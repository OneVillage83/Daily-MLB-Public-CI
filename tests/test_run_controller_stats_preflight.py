from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.stats.auto_daily import AutoDailyStatsGapError
from scripts import run_controller


def _settings(database_path: Path) -> Settings:
    return Settings(
        app_env="development",
        database_path=database_path,
        artifact_dir=database_path.parent / "artifacts",
        report_timezone="America/Los_Angeles",
        service_auth_token="fixture-service-secret",
        odds_api_key="fixture-odds-secret",
        openweather_enabled=False,
        openweather_api_key="",
    )


def test_start_runs_stats_preflight_before_pipeline_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    configured = _settings(tmp_path / "daily.db")
    observed: list[dict[str, object]] = []

    def fake_preflight(**kwargs):
        observed.append(dict(kwargs))
        return object()

    monkeypatch.setattr(run_controller, "run_pipeline_stats_preflight", fake_preflight)

    exit_code = run_controller.main(
        [
            "start",
            "--database",
            str(configured.database_path),
            "--date",
            "2026-08-09",
            "--json",
        ],
        configured_settings=configured,
    )

    assert exit_code == run_controller.EXIT_SUCCESS
    assert len(observed) == 1
    assert observed[0]["database_path"] == configured.database_path
    assert observed[0]["target_date"].isoformat() == "2026-08-09"
    assert observed[0]["raw_root"] == configured.artifact_dir / "stats_raw"
    assert observed[0]["report_path"] == (
        configured.artifact_dir / "stats_acquisition_report.json"
    )
    assert '"status":"pending"' in capsys.readouterr().out


def test_stats_preflight_failure_prevents_pipeline_run_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database = tmp_path / "blocked.db"
    configured = _settings(database)

    def fail_preflight(**kwargs):
        raise AutoDailyStatsGapError("gap repair did not verify")

    monkeypatch.setattr(run_controller, "run_pipeline_stats_preflight", fail_preflight)

    exit_code = run_controller.main(
        [
            "start",
            "--database",
            str(database),
            "--date",
            "2026-08-09",
        ],
        configured_settings=configured,
    )

    output = capsys.readouterr()
    assert exit_code == run_controller.EXIT_EXECUTION_FAILED
    assert "STATS PREFLIGHT FAILED:" in output.err
    assert not database.exists()


def test_explicit_skip_bypasses_stats_preflight(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    configured = _settings(tmp_path / "skip.db")

    def unexpected_preflight(**kwargs):
        raise AssertionError("preflight should have been skipped")

    monkeypatch.setattr(
        run_controller,
        "run_pipeline_stats_preflight",
        unexpected_preflight,
    )

    exit_code = run_controller.main(
        [
            "start",
            "--database",
            str(configured.database_path),
            "--date",
            "2026-08-09",
            "--skip-stats-preflight",
            "--json",
        ],
        configured_settings=configured,
    )

    assert exit_code == run_controller.EXIT_SUCCESS
    assert '"status":"pending"' in capsys.readouterr().out
