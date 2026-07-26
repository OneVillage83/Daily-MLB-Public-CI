from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.stats_validation_supervisor import (
    EXIT_OPERATIONAL_FAILURE,
    EXIT_PARTIAL_OR_INCOMPLETE,
    EXIT_SUCCESS,
    EXIT_USAGE,
    StageResult,
    SupervisorUsageError,
    _overall_exit_code,
    build_parser,
    build_plan,
    main,
)


def _arguments(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return build_parser().parse_args(
        [
            "--through",
            "2026-07-16",
            "--database",
            str(tmp_path / "stats.db"),
            "--raw-root",
            str(tmp_path / "raw"),
            "--output-root",
            str(tmp_path / "validation"),
            *extra,
        ]
    )


def _result(name: str, exit_code: int, tmp_path: Path) -> StageResult:
    return StageResult(
        name=name,
        command=("python", name),
        exit_code=exit_code,
        started_at="2026-07-21T00:00:00+00:00",
        completed_at="2026-07-21T00:00:01+00:00",
        stdout_path=tmp_path / f"{name}.stdout.log",
        stderr_path=tmp_path / f"{name}.stderr.log",
    )


def test_plan_only_builds_deterministic_live_pipeline(tmp_path: Path) -> None:
    plan = build_plan(
        _arguments(tmp_path, "--run-label", "july-16"),
        python_executable=tmp_path / "python.exe",
        now=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )

    assert plan.requested_through_date.isoformat() == "2026-07-16"
    assert plan.acquisition_command == "backfill-current"
    assert plan.mode == "live"
    assert plan.run_directory == (tmp_path / "validation" / "july-16").resolve()
    assert [stage.name for stage in plan.stages] == [
        "acquisition",
        "validation",
        "sqlite-check",
        "evidence-scan",
    ]
    acquisition = plan.stages[0].command
    assert "backfill-current" in acquisition
    assert acquisition[acquisition.index("--season") + 1] == "2026"
    assert acquisition[acquisition.index("--through") + 1] == "2026-07-16"
    assert acquisition[acquisition.index("--mode") + 1] == "live"
    validation = plan.stages[1].command
    assert "validate" in validation
    assert validation[validation.index("--mode") + 1] == "offline"


def test_source_run_replay_forces_offline_without_fixture_root(tmp_path: Path) -> None:
    plan = build_plan(
        _arguments(
            tmp_path,
            "--source-stats-run-id",
            "stats_9548d7a2cfef48bfba526f434006369f",
            "--run-label",
            "replay",
        ),
        python_executable=tmp_path / "python.exe",
    )

    assert plan.mode == "offline"
    acquisition = plan.stages[0].command
    assert acquisition[acquisition.index("--mode") + 1] == "offline"
    assert acquisition[acquisition.index("--source-stats-run-id") + 1] == (
        "stats_9548d7a2cfef48bfba526f434006369f"
    )
    assert "--fixture-root" not in acquisition


def test_offline_provider_run_requires_fixture_or_source_replay(tmp_path: Path) -> None:
    with pytest.raises(SupervisorUsageError, match="offline acquisition requires"):
        build_plan(_arguments(tmp_path, "--mode", "offline"))


def test_invalid_run_label_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SupervisorUsageError, match="--run-label"):
        build_plan(_arguments(tmp_path, "--run-label", "../escape"))


def test_overall_exit_contract_preserves_partial_result(tmp_path: Path) -> None:
    assert (
        _overall_exit_code(
            [
                _result("acquisition", EXIT_PARTIAL_OR_INCOMPLETE, tmp_path),
                _result("validation", EXIT_PARTIAL_OR_INCOMPLETE, tmp_path),
                _result("sqlite-check", EXIT_SUCCESS, tmp_path),
                _result("evidence-scan", EXIT_SUCCESS, tmp_path),
            ]
        )
        == EXIT_PARTIAL_OR_INCOMPLETE
    )


def test_overall_exit_contract_fails_closed_on_scan_error(tmp_path: Path) -> None:
    assert (
        _overall_exit_code(
            [
                _result("acquisition", EXIT_SUCCESS, tmp_path),
                _result("validation", EXIT_SUCCESS, tmp_path),
                _result("sqlite-check", EXIT_SUCCESS, tmp_path),
                _result("evidence-scan", EXIT_OPERATIONAL_FAILURE, tmp_path),
            ]
        )
        == EXIT_OPERATIONAL_FAILURE
    )


def test_plan_only_cli_emits_machine_readable_zero_network_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--through",
            "2026-07-16",
            "--database",
            str(tmp_path / "stats.db"),
            "--raw-root",
            str(tmp_path / "raw"),
            "--output-root",
            str(tmp_path / "validation"),
            "--run-label",
            "july-16",
            "--plan-only",
        ]
    )

    assert exit_code == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "plan"
    assert payload["network_requests_executed"] == 0
    assert payload["requested_through_date"] == "2026-07-16"
    assert len(payload["stages"]) == 4


def test_cli_returns_usage_contract_for_invalid_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "--through",
            "not-a-date",
            "--database",
            str(tmp_path / "stats.db"),
            "--raw-root",
            str(tmp_path / "raw"),
            "--output-root",
            str(tmp_path / "validation"),
            "--plan-only",
        ]
    )

    assert exit_code == EXIT_USAGE
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "usage_error"
    assert payload["exit_code"] == EXIT_USAGE
