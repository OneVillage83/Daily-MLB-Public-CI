from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


SCHEMA_VERSION = "DSE_REPOSITORY_SECRET_SCAN_V1"
EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_SCAN_ERROR = 2

_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:"
    r"API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_?KEY|"
    r"CREDENTIALS?|CLIENT_SECRET|AUTH(?:ORIZATION)?"
    r")(?:_|$)",
    re.IGNORECASE,
)
_ENV_ASSIGNMENT_PATTERN = re.compile(
    r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$"
)

# These signatures identify provider-issued credential formats with low false-positive
# rates. Generic query-string names are intentionally excluded because this repository
# contains redaction fixtures such as ``apiKey=not-a-real-secret``.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private_key_pem",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    ("github_classic_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,255}")),
    ("github_fine_grained_token", re.compile(rb"github_pat_[A-Za-z0-9_]{70,255}")),
    ("aws_access_key_id", re.compile(rb"(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("google_api_key", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}")),
    ("stripe_live_secret", re.compile(rb"(?:sk|rk)_live_[A-Za-z0-9]{16,}")),
    (
        "openai_api_key",
        re.compile(rb"sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}"),
    ),
)


@dataclass(frozen=True, order=True)
class ScanItem:
    path: str
    pattern: str
    source: str

    def as_dict(self) -> dict[str, str]:
        identifier_input = f"{self.path}\0{self.pattern}\0{self.source}".encode("utf-8")
        identifier = hashlib.sha256(identifier_input).hexdigest()[:16]
        return {
            "id": f"secret-scan-{identifier}",
            "path": self.path,
            "pattern": self.pattern,
            "source": self.source,
        }


@dataclass(frozen=True)
class SecretScanReport:
    tracked_file_count: int
    env_file_checked: bool
    configured_secret_count: int
    findings: tuple[ScanItem, ...]
    errors: tuple[ScanItem, ...]

    @property
    def status(self) -> str:
        return "pass" if not self.findings and not self.errors else "fail"

    @property
    def exit_code(self) -> int:
        if self.errors:
            return EXIT_SCAN_ERROR
        if self.findings:
            return EXIT_FINDINGS
        return EXIT_CLEAN

    def as_dict(self) -> dict[str, object]:
        return {
            "configured_secret_count": self.configured_secret_count,
            "env_file_checked": self.env_file_checked,
            "errors": [item.as_dict() for item in sorted(self.errors)],
            "findings": [item.as_dict() for item in sorted(self.findings)],
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "tracked_file_count": self.tracked_file_count,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


class SecretScanError(RuntimeError):
    def __init__(self, pattern: str, *, path: str = ".") -> None:
        super().__init__(pattern)
        self.pattern = pattern
        self.path = path


def _run_git(repo_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(repo_root), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise SecretScanError("git_execution_failed") from exc
    if completed.returncode != 0:
        raise SecretScanError("git_command_failed")
    return completed.stdout


def tracked_files(repo_root: Path) -> tuple[str, ...]:
    output = _run_git(repo_root, ("ls-files", "-z", "--"))
    encoded_paths = [part for part in output.split(b"\0") if part]
    paths: list[str] = []
    for encoded_path in encoded_paths:
        try:
            path = encoded_path.decode("utf-8", errors="strict").replace("\\", "/")
        except UnicodeDecodeError as exc:
            raise SecretScanError("tracked_path_not_utf8") from exc
        pure_path = PurePosixPath(path)
        if pure_path.is_absolute() or ".." in pure_path.parts or path in {"", "."}:
            raise SecretScanError("unsafe_tracked_path")
        paths.append(pure_path.as_posix())
    return tuple(sorted(set(paths)))


def _forbidden_path_pattern(path: str) -> str | None:
    pure_path = PurePosixPath(path)
    if pure_path.name == ".env":
        return "tracked_dotenv"
    if path == "local-data" or path.startswith("local-data/"):
        return "tracked_local_data"
    if path == ".validation" or path.startswith(".validation/"):
        return "tracked_validation_data"
    return None


def _read_tracked_content(repo_root: Path, relative_path: str) -> tuple[bytes, str]:
    absolute_path = repo_root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        mode = absolute_path.lstat().st_mode
    except FileNotFoundError:
        return _run_git(repo_root, ("show", f":{relative_path}")), "git_index"
    except OSError as exc:
        raise SecretScanError("tracked_file_stat_failed", path=relative_path) from exc

    if stat.S_ISLNK(mode):
        return _run_git(repo_root, ("show", f":{relative_path}")), "git_index"
    if not stat.S_ISREG(mode):
        raise SecretScanError("tracked_path_not_regular", path=relative_path)

    try:
        return absolute_path.read_bytes(), "worktree"
    except OSError as exc:
        raise SecretScanError("tracked_file_read_failed", path=relative_path) from exc


def _unquote_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise SecretScanError("dotenv_malformed_quoted_value", path=".env")
        return value[1:-1]
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise SecretScanError("dotenv_malformed_quoted_value", path=".env")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SecretScanError("dotenv_malformed_quoted_value", path=".env") from exc
        if not isinstance(parsed, str):
            raise SecretScanError("dotenv_malformed_quoted_value", path=".env")
        return parsed

    # python-dotenv treats a whitespace-delimited hash as an inline comment.
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return value


def _configured_secrets(env_file: Path) -> tuple[bytes, ...]:
    try:
        text = env_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise SecretScanError("dotenv_read_failed", path=".env") from exc

    values: list[bytes] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT_PATTERN.fullmatch(stripped)
        if match is None:
            continue
        key, raw_value = match.groups()
        if _SECRET_KEY_PATTERN.search(key) is None:
            continue
        value = _unquote_env_value(raw_value)
        if value:
            values.append(value.encode("utf-8"))
    return tuple(sorted(set(values)))


def configured_secret_values(env_file: Path) -> tuple[bytes, ...]:
    """Return configured credential values for value-only downstream scans."""

    return _configured_secrets(env_file)


def _env_path_is_tracked(repo_root: Path, env_file: Path, paths: Sequence[str]) -> bool:
    try:
        relative = env_file.resolve(strict=False).relative_to(repo_root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return relative.as_posix() in paths


def scan_repository(
    repo_root: Path,
    *,
    env_file: Path | None = None,
) -> SecretScanReport:
    root = repo_root.resolve(strict=False)
    findings: set[ScanItem] = set()
    errors: set[ScanItem] = set()

    try:
        paths = tracked_files(root)
    except SecretScanError as exc:
        error = ScanItem(path=exc.path, pattern=exc.pattern, source="scanner")
        return SecretScanReport(0, False, 0, (), (error,))

    for path in paths:
        forbidden_pattern = _forbidden_path_pattern(path)
        if forbidden_pattern is not None:
            findings.add(
                ScanItem(path=path, pattern=forbidden_pattern, source="git_index")
            )

    configured_values: tuple[bytes, ...] = ()
    env_checked = False
    if env_file is not None and env_file.exists():
        if _env_path_is_tracked(root, env_file, paths):
            findings.add(
                ScanItem(
                    path=env_file.name,
                    pattern="tracked_dotenv",
                    source="git_index",
                )
            )
        else:
            try:
                configured_values = _configured_secrets(env_file)
                env_checked = True
            except SecretScanError as exc:
                errors.add(
                    ScanItem(path=exc.path, pattern=exc.pattern, source="scanner")
                )

    for path in paths:
        try:
            content, source = _read_tracked_content(root, path)
        except SecretScanError as exc:
            errors.add(
                ScanItem(path=exc.path, pattern=exc.pattern, source="scanner")
            )
            continue
        for pattern_name, pattern in _CREDENTIAL_PATTERNS:
            if pattern.search(content) is not None:
                findings.add(ScanItem(path=path, pattern=pattern_name, source=source))
        if any(secret in content for secret in configured_values):
            findings.add(
                ScanItem(path=path, pattern="configured_secret_value", source=source)
            )

    return SecretScanReport(
        tracked_file_count=len(paths),
        env_file_checked=env_checked,
        configured_secret_count=len(configured_values),
        findings=tuple(sorted(findings)),
        errors=tuple(sorted(errors)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan Git-tracked content without emitting matched secret values."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Optional untracked dotenv file whose configured secret values are checked.",
    )
    parser.add_argument(
        "--skip-env",
        action="store_true",
        help="Do not read a local dotenv file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.repo_root.resolve(strict=False)
    env_file: Path | None
    if args.skip_env:
        env_file = None
    elif args.env_file.is_absolute():
        env_file = args.env_file
    else:
        env_file = root / args.env_file
    report = scan_repository(root, env_file=env_file)
    print(report.to_json())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
