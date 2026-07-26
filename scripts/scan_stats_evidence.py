from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from scripts.scan_repository_secrets import configured_secret_values


SCHEMA_VERSION = "DSE_STATS_EVIDENCE_SECRET_SCAN_V1"
EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_SCAN_ERROR = 2
_CHUNK_SIZE = 1024 * 1024
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private_key_pem",
        re.compile(
            rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    ("github_classic_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,255}")),
    (
        "github_fine_grained_token",
        re.compile(rb"github_pat_[A-Za-z0-9_]{70,255}"),
    ),
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
class EvidenceScanItem:
    path: str
    pattern: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "pattern": self.pattern}


@dataclass(frozen=True)
class EvidenceScanReport:
    roots: tuple[str, ...]
    scanned_file_count: int
    scanned_byte_count: int
    configured_secret_count: int
    findings: tuple[EvidenceScanItem, ...]
    errors: tuple[EvidenceScanItem, ...]

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

    def to_json(self) -> str:
        return json.dumps(
            {
                "configured_secret_count": self.configured_secret_count,
                "errors": [item.as_dict() for item in sorted(self.errors)],
                "findings": [item.as_dict() for item in sorted(self.findings)],
                "roots": list(self.roots),
                "scanned_byte_count": self.scanned_byte_count,
                "scanned_file_count": self.scanned_file_count,
                "schema_version": SCHEMA_VERSION,
                "status": self.status,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _files_for_root(root: Path) -> tuple[tuple[Path, str], ...]:
    if root.is_symlink():
        raise OSError("evidence root must not be a symlink")
    resolved_root = root.resolve(strict=True)
    if resolved_root.is_file():
        return ((resolved_root, resolved_root.name),)
    if not resolved_root.is_dir():
        raise OSError("evidence root is not a regular file or directory")
    files: list[tuple[Path, str]] = []
    for candidate in sorted(resolved_root.rglob("*"), key=lambda value: value.as_posix()):
        relative = candidate.relative_to(resolved_root).as_posix()
        mode = candidate.lstat().st_mode
        if stat.S_ISLNK(mode):
            files.append((candidate, f"!symlink!/{relative}"))
        elif stat.S_ISREG(mode):
            files.append((candidate, relative))
    return tuple(files)


def _scan_file(
    path: Path,
    configured_values: Sequence[bytes],
) -> tuple[int, set[str]]:
    signatures: tuple[tuple[str, re.Pattern[bytes] | bytes], ...] = (
        *_CREDENTIAL_PATTERNS,
        *(("configured_secret_value", value) for value in configured_values),
    )
    overlap = max(
        512,
        max((len(value) - 1 for value in configured_values), default=0),
    )
    matched: set[str] = set()
    byte_count = 0
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_SIZE):
            byte_count += len(chunk)
            candidate = tail + chunk
            for name, signature in signatures:
                if name in matched:
                    continue
                if isinstance(signature, bytes):
                    found = signature in candidate
                else:
                    found = signature.search(candidate) is not None
                if found:
                    matched.add(name)
            tail = candidate[-overlap:]
    return byte_count, matched


def scan_evidence(
    roots: Sequence[Path],
    *,
    configured_values: Sequence[bytes] = (),
) -> EvidenceScanReport:
    findings: set[EvidenceScanItem] = set()
    errors: set[EvidenceScanItem] = set()
    root_labels: list[str] = []
    file_count = 0
    byte_count = 0
    for index, root in enumerate(roots, start=1):
        label = f"root_{index}"
        root_labels.append(label)
        try:
            files = _files_for_root(root)
        except (OSError, ValueError):
            errors.add(EvidenceScanItem(label, "root_unavailable"))
            continue
        for path, relative in files:
            evidence_path = f"{label}/{relative}"
            if relative.startswith("!symlink!/"):
                errors.add(EvidenceScanItem(evidence_path, "symlink_not_allowed"))
                continue
            try:
                size, matches = _scan_file(path, configured_values)
            except OSError:
                errors.add(EvidenceScanItem(evidence_path, "file_read_failed"))
                continue
            file_count += 1
            byte_count += size
            findings.update(
                EvidenceScanItem(evidence_path, pattern) for pattern in matches
            )
    return EvidenceScanReport(
        roots=tuple(root_labels),
        scanned_file_count=file_count,
        scanned_byte_count=byte_count,
        configured_secret_count=len(set(configured_values)),
        findings=tuple(sorted(findings)),
        errors=tuple(sorted(errors)),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan explicit ignored statistics evidence roots for credentials"
    )
    parser.add_argument("--root", action="append", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    configured: tuple[bytes, ...] = ()
    if arguments.env_file is not None and arguments.env_file.exists():
        try:
            configured = configured_secret_values(arguments.env_file)
        except (OSError, ValueError):
            report = EvidenceScanReport(
                roots=(),
                scanned_file_count=0,
                scanned_byte_count=0,
                configured_secret_count=0,
                findings=(),
                errors=(EvidenceScanItem("env_file", "env_read_failed"),),
            )
            print(report.to_json())
            return report.exit_code
    report = scan_evidence(arguments.root, configured_values=configured)
    print(report.to_json())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
