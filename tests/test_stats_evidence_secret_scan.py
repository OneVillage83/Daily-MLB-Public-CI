from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.scan_stats_evidence import (
    EXIT_CLEAN,
    EXIT_FINDINGS,
    EXIT_SCAN_ERROR,
    SCHEMA_VERSION,
    main,
    scan_evidence,
)


def test_explicit_evidence_scan_is_deterministic_and_counts_bytes(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "raw"
    evidence.mkdir()
    (evidence / "a.csv").write_bytes(b"game_pk,pitch_number\n1,1\n")
    (evidence / "b.json").write_bytes(b'{"provider":"statcast"}\n')

    first = scan_evidence((evidence,))
    second = scan_evidence((evidence,))

    assert first.exit_code == EXIT_CLEAN
    assert first.to_json() == second.to_json()
    assert json.loads(first.to_json()) == {
        "configured_secret_count": 0,
        "errors": [],
        "findings": [],
        "roots": ["root_1"],
        "scanned_byte_count": 49,
        "scanned_file_count": 2,
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
    }


def test_configured_secret_and_signature_are_reported_without_values(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "raw"
    evidence.mkdir()
    configured = b"configured-private-value-123456"
    github_token = b"ghp_" + (b"A" * 36)
    (evidence / "payload.bin").write_bytes(configured + b"\n" + github_token)

    report = scan_evidence((evidence,), configured_values=(configured,))
    serialized = report.to_json()

    assert report.exit_code == EXIT_FINDINGS
    assert {item.pattern for item in report.findings} == {
        "configured_secret_value",
        "github_classic_token",
    }
    assert configured.decode() not in serialized
    assert github_token.decode() not in serialized


def test_chunk_boundary_match_is_detected(tmp_path: Path) -> None:
    evidence = tmp_path / "raw"
    evidence.mkdir()
    configured = b"cross-chunk-configured-secret"
    prefix = b"x" * ((1024 * 1024) - 10)
    (evidence / "large.raw").write_bytes(prefix + configured)

    report = scan_evidence((evidence,), configured_values=(configured,))

    assert report.exit_code == EXIT_FINDINGS
    assert [item.pattern for item in report.findings] == [
        "configured_secret_value"
    ]


def test_missing_root_fails_closed() -> None:
    report = scan_evidence((Path("missing-stats-evidence-root"),))

    assert report.exit_code == EXIT_SCAN_ERROR
    assert report.errors[0].pattern == "root_unavailable"


def test_symlink_is_not_followed(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symbolic links unavailable: {exc}")

    report = scan_evidence((root,))

    assert report.exit_code == EXIT_SCAN_ERROR
    assert report.errors[0].pattern == "symlink_not_allowed"
    assert report.scanned_file_count == 0


def test_cli_reads_configured_values_without_printing_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    secret = "configured-secret-value-987654"
    (root / "payload.txt").write_text(secret, encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(f"STATS_API_KEY={secret}\n", encoding="utf-8")

    exit_code = main(
        ["--root", str(root), "--env-file", str(env_file)]
    )

    output = capsys.readouterr()
    assert exit_code == EXIT_FINDINGS
    assert output.err == ""
    assert output.out.count("\n") == 1
    assert secret not in output.out
