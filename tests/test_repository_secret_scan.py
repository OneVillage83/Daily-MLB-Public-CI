from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.scan_repository_secrets import (
    EXIT_CLEAN,
    EXIT_FINDINGS,
    EXIT_SCAN_ERROR,
    SCHEMA_VERSION,
    main,
    scan_repository,
)


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    return repo


def _track(repo: Path, relative_path: str, content: str | bytes) -> Path:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", relative_path)
    return path


def _patterns(report: object) -> set[str]:
    findings = getattr(report, "findings")
    return {item.pattern for item in findings}


def test_clean_scan_is_deterministic_and_reads_only_untracked_dotenv(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _track(repo, "README.md", "no credentials here\n")
    (repo / ".env").write_text(
        "ODDS_API_KEY='configured-but-not-tracked'\nREPORT_TIMEZONE=UTC\n",
        encoding="utf-8",
    )

    first = scan_repository(repo, env_file=repo / ".env")
    second = scan_repository(repo, env_file=repo / ".env")

    assert first.exit_code == EXIT_CLEAN
    assert first.to_json() == second.to_json()
    document = json.loads(first.to_json())
    assert document == {
        "configured_secret_count": 1,
        "env_file_checked": True,
        "errors": [],
        "findings": [],
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "tracked_file_count": 1,
    }


def test_tracked_sensitive_locations_are_unconditional_findings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _track(repo, ".env", "ODDS_API_KEY=not-a-real-value\n")
    _track(repo, "local-data/raw/provider.json", "{}\n")
    _track(repo, ".validation/run.db", b"sqlite bytes")

    report = scan_repository(repo, env_file=repo / ".env")

    assert report.exit_code == EXIT_FINDINGS
    assert _patterns(report) == {
        "tracked_dotenv",
        "tracked_local_data",
        "tracked_validation_data",
    }
    assert report.env_file_checked is False


def test_exact_configured_secret_is_detected_without_emitting_the_value(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    secret = "configured-" + "credential-" + "9081726354"
    _track(repo, "settings.txt", f"upstream={secret}\n")
    (repo / ".env").write_text(f'ODDS_API_KEY="{secret}"\n', encoding="utf-8")

    report = scan_repository(repo, env_file=repo / ".env")
    serialized = report.to_json()

    assert report.exit_code == EXIT_FINDINGS
    assert _patterns(report) == {"configured_secret_value"}
    assert secret not in serialized
    assert "ODDS_API_KEY" not in serialized


@pytest.mark.parametrize(
    ("pattern_name", "credential"),
    (
        ("private_key_pem", "-----BEGIN " + "PRIVATE KEY-----"),
        ("github_classic_token", "ghp_" + ("A" * 36)),
        ("github_fine_grained_token", "github_pat_" + ("B" * 74)),
        ("aws_access_key_id", "AKIA" + ("C" * 16)),
        ("google_api_key", "AIza" + ("D" * 35)),
        ("slack_token", "xoxb-" + ("E" * 24)),
        ("stripe_live_secret", "sk_live_" + ("F" * 24)),
        ("openai_api_key", "sk-proj-" + ("G" * 40)),
    ),
)
def test_high_confidence_credential_signatures_are_detected_without_values(
    tmp_path: Path,
    pattern_name: str,
    credential: str,
) -> None:
    repo = _repo(tmp_path)
    _track(repo, "credential.txt", credential)

    report = scan_repository(repo, env_file=None)

    assert report.exit_code == EXIT_FINDINGS
    assert pattern_name in _patterns(report)
    assert credential not in report.to_json()


def test_untracked_sensitive_directories_are_not_scanned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _track(repo, "README.md", "tracked and clean\n")
    credential = "ghp_" + ("Z" * 36)
    untracked = repo / "local-data" / "private.txt"
    untracked.parent.mkdir()
    untracked.write_text(credential, encoding="utf-8")

    report = scan_repository(repo, env_file=None)

    assert report.exit_code == EXIT_CLEAN
    assert credential not in report.to_json()


def test_non_git_directory_returns_safe_error(tmp_path: Path) -> None:
    report = scan_repository(tmp_path, env_file=None)

    assert report.exit_code == EXIT_SCAN_ERROR
    assert report.findings == ()
    assert len(report.errors) == 1
    assert report.errors[0].pattern == "git_command_failed"


def test_cli_outputs_only_deterministic_json_and_nonzero_on_finding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path)
    credential = "AKIA" + ("Q" * 16)
    _track(repo, "credential.txt", credential)

    exit_code = main(["--repo-root", str(repo), "--skip-env"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_FINDINGS
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    document = json.loads(captured.out)
    assert document["status"] == "fail"
    assert credential not in captured.out
