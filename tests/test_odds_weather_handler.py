from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.collectors.odds_collector import OddsCollectionResult
from app.config import Settings
from app.http import HttpRequestDiagnostics
from app.odds_weather import (
    OddsWeatherAssemblyError,
    OddsWeatherAttemptOutcome,
    OddsWeatherPhaseHandler,
    OddsWeatherPhasePolicyV1,
    OddsWeatherRepository,
    odds_weather_phase_input_checksum,
)
from app.raw_payloads import RawPayloadCapture
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext
from tests.test_odds_weather_repository import (
    _one_game_repository,
    _zero_game_odds_weather_repository,
)
from tests.test_game_state_repository import RUN_ID


def _settings(artifact_root: Path, *, openweather_enabled: bool = True) -> Settings:
    return Settings(
        artifact_dir=artifact_root,
        service_auth_token="fixture-service-secret",
        odds_api_key="fixture-odds-secret",
        nws_user_agent="Daily-MLB/1.0 (ops@onevillage.example)",
        openweather_enabled=openweather_enabled,
        openweather_api_key=("fixture-weather-secret" if openweather_enabled else ""),
        weather_compare_enabled=True,
    )


def _context(
    repository: OddsWeatherRepository,
    run_id: str,
    *,
    phase_key: PipelinePhaseKey = PipelinePhaseKey.ODDS_WEATHER,
    attempt_number: int = 1,
) -> PhaseExecutionContext:
    slate, _, _ = repository._verify_upstream(run_id)
    return PhaseExecutionContext(
        run_id=run_id,
        requested_date=slate.slate.requested_date,
        as_of_time=slate.slate.as_of_time.isoformat(),
        timezone="America/Los_Angeles",
        phase_key=phase_key,
        attempt_number=attempt_number,
        retry=attempt_number > 1,
        force_refresh=False,
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_fingerprint="f" * 64,
        code_revision="phase4-handler-fixture",
    )


def _capture(
    *,
    provider: str,
    endpoint: str,
    payload: dict[str, object] | list[object],
    retrieved_at: datetime,
    event_id: str | None,
) -> RawPayloadCapture:
    return RawPayloadCapture(
        provider=provider,  # type: ignore[arg-type]
        endpoint_category=endpoint,  # type: ignore[arg-type]
        payload=payload,
        retrieved_at=retrieved_at.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
        event_id=event_id,
    )


def _one_game_acquirers(inventory):
    event = inventory.eligible_provider_event_revisions[0].event
    event_payload = event.mutable_event()
    odds_capture = _capture(
        provider="the_odds_api",
        endpoint="mlb_odds",
        payload=[event_payload],
        retrieved_at=event.retrieved_at,
        event_id=None,
    )
    collection = OddsCollectionResult(
        games=[event_payload],
        quota={},
        capture=odds_capture,
        request=HttpRequestDiagnostics(
            request_status="success",
            status_code=200,
            attempts=1,
            retries_performed=0,
            duration_seconds=0.01,
            response_date_utc=None,
        ),
        warnings=(),
    )
    nws = next(
        item.evidence
        for item in inventory.eligible_weather_revisions
        if item.evidence.provider.value == "nws"
    )
    openweather = next(
        item.evidence
        for item in inventory.eligible_weather_revisions
        if item.evidence.provider.value == "openweather"
    )

    def acquire_nws(latitude, longitude, game_time, *, event_id=None):
        del latitude, longitude, game_time
        return (
            dict(nws.forecast),
            _capture(
                provider="nws",
                endpoint="point_lookup",
                payload={"grid": "fixture"},
                retrieved_at=nws.retrieved_at - timedelta(seconds=1),
                event_id=event_id,
            ),
            _capture(
                provider="nws",
                endpoint="hourly_forecast",
                payload=dict(nws.forecast),
                retrieved_at=nws.retrieved_at,
                event_id=event_id,
            ),
        )

    def acquire_openweather(latitude, longitude, game_time, *, event_id=None):
        del latitude, longitude, game_time
        return (
            dict(openweather.forecast),
            _capture(
                provider="openweather",
                endpoint="one_call",
                payload=dict(openweather.forecast),
                retrieved_at=openweather.retrieved_at,
                event_id=event_id,
            ),
        )

    return lambda: collection, acquire_nws, acquire_openweather


def _handler(repository, inventory, *, settings=None, clock=None, **overrides):
    odds, nws, openweather = _one_game_acquirers(inventory)
    return OddsWeatherPhaseHandler(
        repository.database,
        artifact_root=repository.artifact_root,
        configured_settings=settings or _settings(repository.artifact_root),
        repository=repository,
        clock=clock or (lambda: inventory.observed_at),
        odds_acquirer=overrides.pop("odds_acquirer", odds),
        nws_acquirer=overrides.pop("nws_acquirer", nws),
        openweather_acquirer=overrides.pop("openweather_acquirer", openweather),
        **overrides,
    )


def test_phase_input_checksum_binds_every_behavior_changing_field_and_not_secrets() -> None:
    policy = OddsWeatherPhasePolicyV1(
        odds_weather_contract_version="snapshot-v1",
        odds_provider_event_contract_version="event-v1",
        weather_forecast_contract_version="forecast-v1",
        odds_collector_version="odds-v1",
        odds_regions=("us",),
        odds_markets=("h2h", "spreads", "totals"),
        odds_format="american",
        weather_policy_version="weather-v1",
        openweather_enabled=True,
        weather_compare_enabled=True,
        openweather_api_version="3.0",
        stadium_metadata_policy_version="stadium-v1",
        stadium_catalog_version=4,
    )
    as_of = datetime(2026, 7, 30, 14, tzinfo=timezone.utc)
    observed = datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc)

    def checksum(
        *,
        requested_date: str = "2026-07-30",
        as_of_time: datetime = as_of,
        observed_at: datetime = observed,
        slate_checksum: str = "1" * 64,
        state_checksum: str = "2" * 64,
        bia_checksum: str = "3" * 64,
        selected_policy: OddsWeatherPhasePolicyV1 = policy,
    ) -> str:
        return odds_weather_phase_input_checksum(
            requested_date=requested_date,
            as_of_time=as_of_time,
            observed_at=observed_at,
            upstream_daily_slate_checksum=slate_checksum,
            upstream_game_state_checksum=state_checksum,
            upstream_baseball_intelligence_checksum=bia_checksum,
            policy=selected_policy,
        )

    baseline = checksum()
    mutations = (
        checksum(requested_date="2026-07-31"),
        checksum(as_of_time=as_of + timedelta(seconds=1)),
        checksum(observed_at=observed + timedelta(seconds=1)),
        checksum(slate_checksum="4" * 64),
        checksum(state_checksum="5" * 64),
        checksum(bia_checksum="6" * 64),
        checksum(
            selected_policy=replace(policy, odds_weather_contract_version="snapshot-v2")
        ),
        checksum(
            selected_policy=replace(
                policy, odds_provider_event_contract_version="event-v2"
            )
        ),
        checksum(
            selected_policy=replace(
                policy, weather_forecast_contract_version="forecast-v2"
            )
        ),
        checksum(selected_policy=replace(policy, odds_regions=("us", "uk"))),
        checksum(selected_policy=replace(policy, odds_markets=("h2h",))),
        checksum(selected_policy=replace(policy, odds_format="decimal")),
        checksum(selected_policy=replace(policy, openweather_enabled=False)),
        checksum(selected_policy=replace(policy, weather_compare_enabled=False)),
        checksum(selected_policy=replace(policy, openweather_api_version="3.1")),
        checksum(selected_policy=replace(policy, stadium_catalog_version=5)),
    )
    assert all(mutation != baseline for mutation in mutations)
    assert "secret" not in str(policy.as_dict()).casefold()


@pytest.mark.parametrize(
    ("phase_key", "attempt", "message"),
    (
        (PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY, 1, "ODDS_WEATHER"),
        (PipelinePhaseKey.ODDS_WEATHER, 0, "positive"),
    ),
)
def test_handler_rejects_wrong_phase_or_attempt(
    tmp_path,
    monkeypatch,
    phase_key,
    attempt,
    message,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match=message):
        _handler(repository, inventory)(
            _context(repository, run_id, phase_key=phase_key, attempt_number=attempt)
        )


def test_handler_rejects_naive_or_early_observation_clock(tmp_path, monkeypatch) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    context = _context(repository, run_id)
    with pytest.raises(ValueError, match="timezone-aware"):
        _handler(
            repository,
            inventory,
            clock=lambda: datetime(2026, 7, 30, 22, 30),
        )(context)
    with pytest.raises(ValueError, match="precedes"):
        _handler(
            repository,
            inventory,
            clock=lambda: datetime(2026, 7, 30, 14, tzinfo=timezone.utc),
        )(context)


def test_one_game_handler_persists_and_reconstructs_exact_success(tmp_path, monkeypatch) -> None:
    repository, _, source_inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return source_inventory.observed_at

    result = _handler(repository, source_inventory, clock=clock)(
        _context(repository, run_id)
    )
    persisted = repository.get_for_run_attempt(run_id, 1)
    reopened = OddsWeatherRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
        secret_values=repository.secret_values,
    )

    assert calls == 1
    assert result.status is PipelinePhaseStatus.SUCCEEDED
    assert result.input_checksum is not None
    assert result.output_checksum == persisted.snapshot.checksum
    assert result.artifact_relpath == persisted.artifact.relpath
    assert reopened.get_for_run_attempt(run_id, 1).snapshot == persisted.snapshot
    assert reopened.get_attempt_manifest(run_id, 1).phase_input_checksum == result.input_checksum
    assert reopened.load_retained_inventory(run_id, 1).checksum == persisted.retained_inventory_checksum


def test_zero_game_fast_path_calls_no_provider_and_returns_success(tmp_path) -> None:
    repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = repository.baseball_intelligence.get_latest_for_run(RUN_ID)
    assert upstream is not None
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("zero-game handler called a provider")

    configured = _settings(repository.artifact_root, openweather_enabled=False)
    configured = replace(configured, odds_api_key="")
    handler = OddsWeatherPhaseHandler(
        repository.database,
        artifact_root=repository.artifact_root,
        configured_settings=configured,
        repository=repository,
        clock=lambda: upstream.sealed_at + timedelta(seconds=1),
        odds_acquirer=forbidden,
        nws_acquirer=forbidden,
        openweather_acquirer=forbidden,
    )
    run_id = upstream.run_id
    result = handler(_context(repository, run_id))
    persisted = repository.get_for_run_attempt(run_id, 1)

    assert calls == 0
    assert result.status is PipelinePhaseStatus.SUCCEEDED
    assert persisted.snapshot.games == ()
    assert repository.load_retained_inventory(run_id, 1).raw_captures == ()


def test_odds_acquisition_failure_retains_redacted_failed_attempt(tmp_path, monkeypatch) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    configured = _settings(repository.artifact_root)

    def fail_odds():
        raise RuntimeError(f"transport rejected {configured.odds_api_key}")

    with pytest.raises(RuntimeError, match="transport rejected"):
        _handler(
            repository,
            inventory,
            settings=configured,
            odds_acquirer=fail_odds,
        )(_context(repository, run_id))
    evidence = repository.get_attempt_evidence(run_id, 1)
    manifest = repository.get_attempt_manifest(run_id, 1)
    manifest_bytes = (repository.artifact_root / evidence.manifest.relpath).read_bytes()

    assert evidence.outcome is OddsWeatherAttemptOutcome.ACQUISITION_FAILED
    assert evidence.snapshot_checksum is None
    assert configured.odds_api_key.encode() not in manifest_bytes
    assert "[REDACTED]" in manifest.final_warnings[0].message


def test_normalization_failure_retains_raw_capture_without_snapshot(tmp_path, monkeypatch) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    event_time = inventory.eligible_provider_event_revisions[0].event.retrieved_at
    invalid_event = {"id": ""}
    collection = OddsCollectionResult(
        games=[invalid_event],
        quota={},
        capture=_capture(
            provider="the_odds_api",
            endpoint="mlb_odds",
            payload=[invalid_event],
            retrieved_at=event_time,
            event_id=None,
        ),
        request=HttpRequestDiagnostics("success", 200, 1, 0, 0.01, None),
        warnings=(),
    )

    with pytest.raises(Exception, match="provider event id"):
        _handler(
            repository,
            inventory,
            odds_acquirer=lambda: collection,
        )(_context(repository, run_id))
    evidence = repository.get_attempt_evidence(run_id, 1)
    retained = repository.load_retained_inventory(run_id, 1)

    assert evidence.outcome is OddsWeatherAttemptOutcome.NORMALIZATION_FAILED
    assert evidence.snapshot_checksum is None
    assert len(retained.raw_captures) == 1
    assert repository.get_latest_for_run(run_id) is None


def test_required_weather_acquisition_failure_retains_failed_attempt(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("fixture weather provider unavailable")

    with pytest.raises(RuntimeError, match="fixture weather provider unavailable"):
        _handler(
            repository,
            inventory,
            nws_acquirer=fail,
            openweather_acquirer=fail,
        )(_context(repository, run_id))
    evidence = repository.get_attempt_evidence(run_id, 1)

    assert evidence.outcome is OddsWeatherAttemptOutcome.ACQUISITION_FAILED
    assert evidence.snapshot_checksum is None
    assert repository.get_latest_for_run(run_id) is None


def test_nws_failure_uses_openweather_fallback_with_explicit_warning(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)

    def fail_nws(*args, **kwargs):
        raise RuntimeError("fixture NWS unavailable")

    result = _handler(repository, inventory, nws_acquirer=fail_nws)(
        _context(repository, run_id)
    )
    persisted = repository.get_for_run_attempt(run_id, 1)

    assert result.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert persisted.snapshot.games[0].weather.nws is None
    assert persisted.snapshot.games[0].weather.openweather is not None
    assert "nws_acquisition_failed" in {
        warning.code for warning in persisted.snapshot.warnings
    }


def test_optional_openweather_comparison_failure_does_not_discard_nws(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)

    def fail_openweather(*args, **kwargs):
        raise RuntimeError("fixture OpenWeather unavailable")

    result = _handler(
        repository,
        inventory,
        openweather_acquirer=fail_openweather,
    )(_context(repository, run_id))
    persisted = repository.get_for_run_attempt(run_id, 1)

    assert result.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert persisted.snapshot.games[0].weather.nws is not None
    assert persisted.snapshot.games[0].weather.openweather is None
    assert "openweather_acquisition_failed" in {
        warning.code for warning in persisted.snapshot.warnings
    }


def test_assembly_and_persistence_failures_retain_assembly_failed_without_masking(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    original_assemble = repository.assemble_inventory

    def fail_assembly(_inventory):
        raise OddsWeatherAssemblyError("deterministic assembly conflict")

    monkeypatch.setattr(repository, "assemble_inventory", fail_assembly)
    with pytest.raises(OddsWeatherAssemblyError, match="deterministic assembly conflict"):
        _handler(repository, inventory)(_context(repository, run_id))
    assert repository.get_attempt_evidence(
        run_id,
        1,
    ).outcome is OddsWeatherAttemptOutcome.ASSEMBLY_FAILED

    # A fresh retry attempt can succeed without rewriting attempt 1.
    pipeline = repository.database
    del pipeline
    monkeypatch.setattr(repository, "assemble_inventory", original_assemble)


def test_persistence_failure_retains_failure_and_preserves_original_exception(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)

    def fail_persistence(**kwargs):
        del kwargs
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(repository, "persist_assembly", fail_persistence)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        _handler(repository, inventory)(_context(repository, run_id))
    evidence = repository.get_attempt_evidence(run_id, 1)

    assert evidence.outcome is OddsWeatherAttemptOutcome.ASSEMBLY_FAILED
    assert evidence.snapshot_checksum is None


def test_handler_is_exactly_idempotent_for_same_attempt(tmp_path, monkeypatch) -> None:
    repository, _, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    handler = _handler(repository, inventory)
    context = _context(repository, run_id)

    first = handler(context)
    second = handler(context)

    assert second == first
    assert len(repository.list_attempt_evidence(run_id)) == 1
