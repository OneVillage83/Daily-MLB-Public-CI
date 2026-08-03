from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.run_controller.contracts import PipelinePhaseKey, PipelineRunStatus
from scripts.run_controller import (
    EXIT_CONFLICT,
    EXIT_SUCCESS,
    build_controller,
    main,
)


ROOT = Path(__file__).resolve().parents[1]


def _settings(database_path: Path) -> Settings:
    return Settings(
        database_path=database_path,
        artifact_dir=database_path.parent / "artifacts",
        report_timezone="America/Los_Angeles",
        service_auth_token="fixture-service-secret",
        odds_api_key="fixture-odds-secret",
        openweather_enabled=False,
        openweather_api_key="",
    )


def test_cli_start_show_json_and_duplicate_without_executing_network(
    tmp_path: Path,
    capsys,
) -> None:
    database = tmp_path / "cli.db"
    configured = _settings(database)
    start_code = main(
        ["start", "--database", str(database), "--date", "2026-07-26", "--json"],
        configured_settings=configured,
    )
    start_output = json.loads(capsys.readouterr().out)

    assert start_code == EXIT_SUCCESS
    assert start_output["status"] == PipelineRunStatus.PENDING.value
    assert start_output["phase_status_counts"]["pending"] == 15
    assert start_output["next_actionable_phase"] == "daily_slate"

    show_code = main(
        [
            "show",
            "--database",
            str(database),
            "--run-id",
            start_output["run_id"],
            "--json",
        ],
        configured_settings=configured,
    )
    show_output = json.loads(capsys.readouterr().out)
    assert show_code == EXIT_SUCCESS
    assert show_output == start_output

    duplicate_code = main(
        ["start", "--database", str(database), "--date", "2026-07-26"],
        configured_settings=configured,
    )
    duplicate_output = capsys.readouterr()
    assert duplicate_code == EXIT_CONFLICT
    assert "CONFLICT:" in duplicate_output.err

    final_code = main(
        [
            "show",
            "--database",
            str(database),
            "--run-id",
            start_output["run_id"],
            "--json",
        ],
        configured_settings=configured,
    )
    final_output = json.loads(capsys.readouterr().out)
    assert final_code == EXIT_SUCCESS
    assert final_output["status"] == "pending"
    assert final_output["phase_status_counts"]["pending"] == 15
    serialized = json.dumps(final_output, sort_keys=True)
    assert configured.odds_api_key not in serialized
    assert configured.service_auth_token not in serialized


def test_production_controller_registers_first_seven_phases(tmp_path: Path) -> None:
    configured = _settings(tmp_path / "handlers.db")

    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
    )

    assert tuple(controller.handlers) == (
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseKey.DATA_QUALITY,
        PipelinePhaseKey.MATCHUP_PACKET,
        PipelinePhaseKey.MODEL_FEATURE_SET,
    )


def test_cli_human_show_lists_all_phases(tmp_path: Path, capsys) -> None:
    database = tmp_path / "human.db"
    configured = _settings(database)
    assert (
        main(
            ["start", "--database", str(database), "--date", "2026-07-26", "--json"],
            configured_settings=configured,
        )
        == EXIT_SUCCESS
    )
    run_id = json.loads(capsys.readouterr().out)["run_id"]

    assert (
        main(
            ["show", "--database", str(database), "--run-id", run_id],
            configured_settings=configured,
        )
        == EXIT_SUCCESS
    )
    output = capsys.readouterr().out
    assert "run_id:" in output
    assert "01 daily_slate status=pending attempts=0" in output
    assert "15 human_review status=pending attempts=0" in output
    assert "configuration_metadata" not in output


def test_cli_invalid_arguments_and_input_use_exit_two_without_traceback(
    tmp_path: Path,
    capsys,
) -> None:
    configured = _settings(tmp_path / "invalid.db")
    with pytest.raises(SystemExit) as captured:
        main(["start"], configured_settings=configured)
    argument_output = capsys.readouterr()
    assert captured.value.code == 2
    assert "usage:" in argument_output.err
    assert "Traceback" not in argument_output.err

    exit_code = main(
        ["show", "--database", str(configured.database_path), "--run-id", "invalid"],
        configured_settings=configured,
    )
    input_output = capsys.readouterr()
    assert exit_code == 2
    assert "ERROR:" in input_output.err
    assert "Traceback" not in input_output.err


def test_cli_script_entrypoint_start_and_show_use_temporary_database(
    tmp_path: Path,
) -> None:
    database = tmp_path / "subprocess.db"
    artifact_dir = tmp_path / "artifacts"
    environment = dict(os.environ)
    environment.update(
        {
            "ARTIFACT_DIR": str(artifact_dir),
            "OPENWEATHER_ENABLED": "false",
            "REPORT_TIMEZONE": "America/Los_Angeles",
        }
    )
    start = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_controller.py"),
            "start",
            "--database",
            str(database),
            "--date",
            "2026-07-26",
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert start.returncode == EXIT_SUCCESS
    payload = json.loads(start.stdout)

    show = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_controller.py"),
            "show",
            "--database",
            str(database),
            "--run-id",
            payload["run_id"],
            "--json",
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert show.returncode == EXIT_SUCCESS
    assert json.loads(show.stdout) == payload
