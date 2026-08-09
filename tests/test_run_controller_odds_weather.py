from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.odds_collector import OddsCollectionResult
from app.config import Settings
from app.http import HttpRequestDiagnostics
from app.odds_weather import (
    OddsWeatherAttemptOutcome,
    OddsWeatherPhaseHandler,
    OddsWeatherRepository,
    WeatherProvider,
)
from app.raw_payloads import RawPayloadCapture
from app.run_controller.contracts import (
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import PipelineRunRepository
from app.run_controller.service import ManualRunController, ManualRunExecutionBlocked, ManualRunExecutionError
from scripts import run_controller as run_controller_cli
from scripts.run_controller import EXIT_BLOCKED, _safe_configuration_metadata, build_controller
from tests.test_baseball_intelligence_repository import _six_category_repository
from tests.test_baseball_intelligence_migration_v10_roundtrip import (
    _fixture as _six_category_fixture,
)
from tests import test_game_state_repository as game_state_repository_tests
from tests.test_odds_weather_assembly import _odds_event, _weather


def _settings(database_path: Path, artifact_root: Path) -> Settings:
    return Settings(
        database_path=database_path,
        artifact_dir=artifact_root,
        report_timezone="America/Los_Angeles",
        service_auth_token="phase4-service-secret",
        odds_api_key="phase4-odds-secret",
        nws_user_agent="Daily-MLB/1.0 (ops@onevillage.example)",
        openweather_enabled=True,
        openweather_api_key="phase4-weather-secret",
        weather_compare_enabled=True,
    )


def _capture(provider, endpoint, payload, retrieved_at, event_id=None):
    return RawPayloadCapture(
        provider=provider,
        endpoint_category=endpoint,
        payload=payload,
        retrieved_at=retrieved_at.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
        event_id=event_id,
    )


def _phase4_pending_controller(
    tmp_path, monkeypatch, *, fail_first=False, include_decision_phases=False, include_final_output=False
):
    fixture_preview = _six_category_fixture()
    monkeypatch.setattr(
        game_state_repository_tests,
        "NOW",
        fixture_preview.slate.as_of_time,
    )
    bia_repository, bia_result, bia_inventory, persisted, fixture, run_id = _six_category_repository(
        tmp_path, monkeypatch
    )
    assert persisted is not None
    pipeline = PipelineRunRepository(
        bia_repository.database,
        secret_values=("phase4-service-secret", "phase4-odds-secret"),
    )
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        input_checksum=bia_inventory.checksum,
        output_checksum=persisted.assembly.checksum,
        artifact_relpath=persisted.artifact.relpath,
        warnings=bia_result.warning_payload(),
        transitioned_at=persisted.sealed_at.isoformat(),
    )
    observed_at = max(
        persisted.sealed_at + timedelta(seconds=1),
        datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc),
    )
    game = fixture.slate.games[0]
    event = _odds_event(
        "odds-event-controller",
        commence=game.scheduled_start_time,
        retrieved_at=observed_at - timedelta(minutes=10),
    )
    odds_capture = _capture(
        "the_odds_api",
        "mlb_odds",
        [event.mutable_event()],
        event.retrieved_at,
    )
    collection = OddsCollectionResult(
        games=[event.mutable_event()],
        quota={},
        capture=odds_capture,
        request=HttpRequestDiagnostics("success", 200, 1, 0, 0.01, None),
        warnings=(),
    )
    nws = _weather(
        game,
        WeatherProvider.NWS,
        retrieved_at=observed_at - timedelta(minutes=8),
    )
    openweather = _weather(
        game,
        WeatherProvider.OPENWEATHER,
        retrieved_at=observed_at - timedelta(minutes=7),
    )
    calls = {"odds": 0, "nws": 0, "openweather": 0}

    def odds_acquirer():
        calls["odds"] += 1
        if fail_first and calls["odds"] == 1:
            raise RuntimeError("fixture acquisition failed phase4-odds-secret")
        return collection

    def nws_acquirer(latitude, longitude, game_time, *, event_id=None):
        del latitude, longitude, game_time
        calls["nws"] += 1
        return (
            dict(nws.forecast),
            _capture(
                "nws",
                "point_lookup",
                {"grid": "controller"},
                nws.retrieved_at - timedelta(seconds=1),
                event_id,
            ),
            _capture(
                "nws",
                "hourly_forecast",
                dict(nws.forecast),
                nws.retrieved_at,
                event_id,
            ),
        )

    def openweather_acquirer(latitude, longitude, game_time, *, event_id=None):
        del latitude, longitude, game_time
        calls["openweather"] += 1
        return (
            dict(openweather.forecast),
            _capture(
                "openweather",
                "one_call",
                dict(openweather.forecast),
                openweather.retrieved_at,
                event_id,
            ),
        )

    configured = _settings(
        bia_repository.database.path,
        bia_repository.artifact_root,
    )
    repository = OddsWeatherRepository(
        bia_repository.database,
        artifact_root=bia_repository.artifact_root,
        secret_values=configured.credential_values(),
        clock=lambda: observed_at + timedelta(seconds=1),
    )
    handler = OddsWeatherPhaseHandler(
        repository.database,
        artifact_root=repository.artifact_root,
        configured_settings=configured,
        repository=repository,
        clock=lambda: observed_at,
        odds_acquirer=odds_acquirer,
        nws_acquirer=nws_acquirer,
        openweather_acquirer=openweather_acquirer,
    )
    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: observed_at,
        odds_weather_handler=handler,
    )
    if not include_final_output:
        registered_count = 11 if include_decision_phases else 7
        controller = ManualRunController(
            controller.repository,
            timezone_name=configured.report_timezone,
            configuration_metadata=_safe_configuration_metadata(configured),
            handlers={
                key: value
                for key, value in controller.handlers.items()
                if key in tuple(PipelinePhaseKey)[:registered_count]
            },
            clock=lambda: observed_at,
        )
    return controller, configured, handler, run_id, calls


def test_production_controller_registers_all_fifteen_phases(tmp_path) -> None:
    configured = _settings(tmp_path / "controller.db", tmp_path / "artifacts")
    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
    )

    assert tuple(controller.handlers) == tuple(PipelinePhaseKey)
    metadata = _safe_configuration_metadata(configured)["odds_weather"]
    assert metadata == {
        "attempt_manifest_version": "DSE_ODDS_WEATHER_ATTEMPT_MANIFEST_V1",
        "contract_version": "DSE_ODDS_WEATHER_V1",
        "input_contract_version": "DSE_ODDS_WEATHER_PHASE_INPUT_V1",
        "network_enabled": True,
        "nws_mode": "primary",
        "odds_mode": "the_odds_api_mlb",
        "openweather_enabled": True,
        "openweather_mode": "comparison",
        "source_mode": "provider_acquisition_and_retained_sqlite",
        "weather_contract_version": "DSE_WEATHER_FORECAST_V1",
    }


def test_controller_executes_pre_model_chain_and_blocks_safely_at_predictions(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    controller, configured, handler, run_id, calls = _phase4_pending_controller(
        tmp_path,
        monkeypatch,
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS

    summary = controller.show(run_id)
    phase4 = summary.phases[3]
    data_quality, matchup_packet, model_feature_set = summary.phases[4:7]
    persisted = handler.repository.get_for_run_attempt(run_id, 1)

    assert summary.run.status is PipelineRunStatus.RUNNING
    assert phase4.status in {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
    }
    assert phase4.attempt_count == 1
    assert phase4.input_checksum is not None
    assert phase4.output_checksum == persisted.snapshot.checksum
    assert phase4.artifact_relpath == persisted.artifact.relpath
    assert all(
        phase.status in {
            PipelinePhaseStatus.SUCCEEDED,
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
            PipelinePhaseStatus.DEGRADED,
        }
        for phase in (data_quality, matchup_packet, model_feature_set)
    )
    assert all(phase.attempt_count == 1 for phase in summary.phases[4:7])
    assert all(item.status is PipelinePhaseStatus.PENDING for item in summary.phases[7:])
    first_calls = dict(calls)

    with pytest.raises(ManualRunExecutionBlocked) as second:
        controller.resume(run_id)
    assert second.value.phase_key is PipelinePhaseKey.PREDICTIONS
    assert calls == first_calls
    assert controller.show(run_id).phases[3].attempt_count == 1
    assert len(handler.repository.list_attempt_evidence(run_id)) == 1

    monkeypatch.setattr(run_controller_cli, "build_controller", lambda *args, **kwargs: controller)
    exit_code = run_controller_cli.main(
        ["resume", "--database", str(configured.database_path), "--run-id", run_id],
        configured_settings=configured,
    )
    error = capsys.readouterr().err
    assert exit_code == EXIT_BLOCKED
    assert "predictions" in error
    assert configured.odds_api_key not in error
    assert configured.service_auth_token not in error


def test_phase4_failure_retry_preserves_attempt_one_and_then_blocks_predictions(
    tmp_path,
    monkeypatch,
) -> None:
    controller, configured, handler, run_id, calls = _phase4_pending_controller(
        tmp_path,
        monkeypatch,
        fail_first=True,
    )
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is PipelinePhaseKey.ODDS_WEATHER
    assert configured.odds_api_key not in failed.value.safe_message
    failed_summary = controller.show(run_id)
    assert failed_summary.run.status is PipelineRunStatus.FAILED
    assert failed_summary.run.failure_phase is PipelinePhaseKey.ODDS_WEATHER
    assert failed_summary.phases[3].status is PipelinePhaseStatus.FAILED
    assert failed_summary.phases[4].status is PipelinePhaseStatus.PENDING
    attempt1 = handler.repository.get_attempt_evidence(run_id, 1)
    assert attempt1.outcome is OddsWeatherAttemptOutcome.ACQUISITION_FAILED

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS
    succeeded = controller.show(run_id)
    attempt2 = handler.repository.get_attempt_evidence(run_id, 2)
    latest = handler.repository.get_latest_for_run(run_id)

    assert succeeded.run.status is PipelineRunStatus.RUNNING
    assert succeeded.phases[3].attempt_count == 2
    assert succeeded.phases[3].status in {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
    }
    assert handler.repository.get_attempt_evidence(run_id, 1) == attempt1
    assert attempt2.outcome is OddsWeatherAttemptOutcome.ASSEMBLED
    assert latest is not None and latest.phase_attempt == 2
    assert calls == {"odds": 2, "nws": 1, "openweather": 1}
    assert all(
        phase.status
        in {
            PipelinePhaseStatus.SUCCEEDED,
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
            PipelinePhaseStatus.DEGRADED,
        }
        for phase in succeeded.phases[4:7]
    )
    assert succeeded.phases[7].status is PipelinePhaseStatus.PENDING


def test_controller_summary_json_contains_phase4_evidence(tmp_path, monkeypatch) -> None:
    controller, _, handler, run_id, _ = _phase4_pending_controller(tmp_path, monkeypatch)
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(run_id)
    payload = json.loads(json.dumps(controller.show(run_id).as_dict()))
    phase = payload["phases"][3]
    persisted = handler.repository.get_for_run_attempt(run_id, 1)

    assert phase["phase_key"] == "odds_weather"
    assert phase["attempt_count"] == 1
    assert phase["input_checksum"]
    assert phase["output_checksum"] == persisted.snapshot.checksum
    assert phase["artifact_relpath"] == persisted.artifact.relpath
