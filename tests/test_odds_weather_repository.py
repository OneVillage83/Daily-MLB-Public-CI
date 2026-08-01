from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pytest import MonkeyPatch

from app.artifacts import ArtifactPaths
from app.daily_slate.contracts import canonical_json_bytes
from app.identifiers import parse_requested_date
from app.odds_weather import (
    OddsProviderEventV1,
    OddsWeatherAssemblyResultV1,
    OddsWeatherAttemptOutcome,
    OddsWeatherRawCaptureV1,
    OddsWeatherRetainedEvidenceInventoryV1,
    OddsWeatherRepository,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
)
from app.raw_payloads import RawPayloadCapture, sanitized_json_bytes
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.repository import PipelineRunRepository
from tests.test_baseball_intelligence_repository import (
    _six_category_repository,
    _zero_game_repository,
)
from tests import test_game_state_repository as game_state_repository_tests
from tests.test_game_state_repository import RUN_ID
from tests.test_odds_weather_assembly import _odds_event, _weather


def _zero_game_odds_weather_repository(tmp_path: Path) -> OddsWeatherRepository:
    bia_repository = _zero_game_repository(tmp_path)
    result, inventory = bia_repository.assemble_for_run(run_id=RUN_ID)
    persisted = bia_repository.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    pipeline = PipelineRunRepository(bia_repository.database)
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseStatus.SUCCEEDED,
        input_checksum=inventory.checksum,
        output_checksum=persisted.assembly.checksum,
        artifact_relpath=persisted.artifact.relpath,
        transitioned_at=persisted.sealed_at.isoformat(),
    )
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.RUNNING,
        transitioned_at=persisted.sealed_at.isoformat(),
    )
    return OddsWeatherRepository(
        bia_repository.database,
        artifact_root=bia_repository.artifact_root,
        clock=lambda: persisted.sealed_at + timedelta(seconds=1),
    )


def test_repository_persists_and_reconstructs_zero_game_snapshot_offline(
    tmp_path: Path,
) -> None:
    repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = repository.baseball_intelligence.get_latest_for_run(RUN_ID)
    assert upstream is not None
    observed_at = upstream.sealed_at
    inventory = repository.build_inventory(
        run_id=RUN_ID,
        phase_attempt=1,
        observed_at=observed_at,
        phase_input_checksum=hashlib.sha256(b"phase-4-zero-game").hexdigest(),
        raw_captures=(),
    )
    result, completed = repository.assemble_inventory(inventory)
    persisted = repository.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=completed,
    )

    reopened = OddsWeatherRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
        clock=lambda: persisted.sealed_at,
    )
    reconstructed = reopened.get_by_snapshot_id(persisted.snapshot_id)

    assert result.snapshot.games == ()
    assert result.snapshot.source_raw_capture_checksums == ()
    assert persisted.snapshot.canonical_json_bytes() == result.snapshot.canonical_json_bytes()
    assert reconstructed.snapshot.canonical_json_bytes() == result.snapshot.canonical_json_bytes()
    assert reopened.load_retained_inventory(RUN_ID, 1) == completed
    assert reopened.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=completed,
    ) == persisted
    assert reopened.get_attempt_evidence(
        RUN_ID,
        1,
    ).outcome is OddsWeatherAttemptOutcome.ASSEMBLED


def _raw_capture(
    *,
    artifact_root: Path,
    run_id: str,
    requested_date: str,
    ordinal: int,
    provider: str,
    endpoint_category: str,
    payload: dict[str, object] | list[object],
    retrieved_at: datetime,
    source_game_id: str | None = None,
    provider_event_id: str | None = None,
) -> OddsWeatherRawCaptureV1:
    capture = RawPayloadCapture(
        provider=provider,  # type: ignore[arg-type]
        endpoint_category=endpoint_category,  # type: ignore[arg-type]
        payload=payload,
        retrieved_at=retrieved_at.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
        event_id=provider_event_id or source_game_id,
    )
    content = sanitized_json_bytes(capture)
    checksum = hashlib.sha256(content).hexdigest()
    path = ArtifactPaths(
        artifact_root,
        parse_requested_date(requested_date),
        run_id,
    ).raw_json_path(provider, endpoint_category, checksum)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return OddsWeatherRawCaptureV1(
        ordinal=ordinal,
        provider=provider,
        endpoint_category=endpoint_category,
        source_game_id=source_game_id,
        provider_event_id=provider_event_id,
        retrieved_at=retrieved_at,
        provider_timestamp=None,
        raw_relpath=path.relative_to(artifact_root).as_posix(),
        checksum=checksum,
        byte_count=len(content),
    )


def _one_game_repository(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> tuple[
    OddsWeatherRepository,
    OddsWeatherAssemblyResultV1,
    OddsWeatherRetainedEvidenceInventoryV1,
    str,
]:
    monkeypatch.setattr(
        game_state_repository_tests,
        "NOW",
        datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc),
    )
    bia_repository, bia_result, _, bia_persisted, fixture, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
    )
    assert bia_persisted is not None
    pipeline = PipelineRunRepository(bia_repository.database)
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        input_checksum=hashlib.sha256(b"phase-3-input").hexdigest(),
        output_checksum=bia_persisted.assembly.checksum,
        artifact_relpath=bia_persisted.artifact.relpath,
        warnings=bia_result.warning_payload(),
        transitioned_at=bia_persisted.sealed_at.isoformat(),
    )
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.RUNNING,
        transitioned_at=bia_persisted.sealed_at.isoformat(),
    )
    observed_at = datetime(2026, 7, 30, 22, 30, tzinfo=timezone.utc)
    repository = OddsWeatherRepository(
        bia_repository.database,
        artifact_root=bia_repository.artifact_root,
        clock=lambda: observed_at + timedelta(seconds=1),
    )
    game = fixture.slate.games[0]
    event_source = _odds_event(
        "odds-event-900001",
        commence=game.scheduled_start_time,
        retrieved_at=datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc),
    )
    event_capture = _raw_capture(
        artifact_root=repository.artifact_root,
        run_id=run_id,
        requested_date=fixture.slate.requested_date,
        ordinal=1,
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[event_source.mutable_event()],
        retrieved_at=event_source.retrieved_at,
        provider_event_id=event_source.provider_event_id,
    )
    event = OddsProviderEventV1(
        provider_event_id=event_source.provider_event_id,
        retrieved_at=event_source.retrieved_at,
        raw_capture_checksum=event_capture.checksum,
        event=event_source.mutable_event(),
        history_rows=(
            {
                "event_id": event_source.provider_event_id,
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": -110,
                "point": None,
                "retrieved_at": "2026-07-30T21:30:00+00:00",
            },
            {
                "event_id": event_source.provider_event_id,
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": -125,
                "point": None,
                "retrieved_at": "2026-07-30T22:15:00+00:00",
            },
            {
                "event_id": event_source.provider_event_id,
                "bookmaker_key": "book-a",
                "market_key": "spreads",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": -105,
                "point": -1.5,
                "retrieved_at": "2026-07-30T22:15:00+00:00",
            },
            {
                "event_id": event_source.provider_event_id,
                "bookmaker_key": "book-a",
                "market_key": "spreads",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": 105,
                "point": -2.5,
                "retrieved_at": "2026-07-30T22:15:00+00:00",
            },
            {
                "event_id": event_source.provider_event_id,
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": -140,
                "point": None,
                "retrieved_at": "2026-07-30T22:45:00+00:00",
            },
        ),
    )
    future_source = _odds_event(
        event_source.provider_event_id,
        commence=game.scheduled_start_time,
        retrieved_at=datetime(2026, 7, 30, 23, 0, tzinfo=timezone.utc),
        variant=9,
    )
    future_capture = _raw_capture(
        artifact_root=repository.artifact_root,
        run_id=run_id,
        requested_date=fixture.slate.requested_date,
        ordinal=2,
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[future_source.mutable_event()],
        retrieved_at=future_source.retrieved_at,
        provider_event_id=future_source.provider_event_id,
    )
    future_event = OddsProviderEventV1(
        provider_event_id=future_source.provider_event_id,
        retrieved_at=future_source.retrieved_at,
        raw_capture_checksum=future_capture.checksum,
        event=future_source.mutable_event(),
    )
    weather_sources: list[WeatherForecastEvidenceV1] = []
    captures = [event_capture, future_capture]
    ordinal = 3
    for provider, retrieved_at in (
        (WeatherProvider.NWS, datetime(2026, 7, 30, 22, 5, tzinfo=timezone.utc)),
        (WeatherProvider.NWS, datetime(2026, 7, 30, 23, 5, tzinfo=timezone.utc)),
        (WeatherProvider.OPENWEATHER, datetime(2026, 7, 30, 22, 6, tzinfo=timezone.utc)),
    ):
        source = _weather(
            game,
            provider,
            retrieved_at=retrieved_at,
        )
        source_captures: list[OddsWeatherRawCaptureV1] = []
        if provider is WeatherProvider.NWS:
            point_capture = _raw_capture(
                artifact_root=repository.artifact_root,
                run_id=run_id,
                requested_date=fixture.slate.requested_date,
                ordinal=ordinal,
                provider=provider.value,
                endpoint_category="point_lookup",
                payload={"grid_id": "fixture-grid", "revision": retrieved_at.isoformat()},
                retrieved_at=source.retrieved_at,
                source_game_id=source.source_game_id,
            )
            captures.append(point_capture)
            source_captures.append(point_capture)
            ordinal += 1
        forecast_capture = _raw_capture(
            artifact_root=repository.artifact_root,
            run_id=run_id,
            requested_date=fixture.slate.requested_date,
            ordinal=ordinal,
            provider=provider.value,
            endpoint_category=(
                "hourly_forecast"
                if provider is WeatherProvider.NWS
                else "one_call"
            ),
            payload=json.loads(json.dumps(dict(source.forecast))),
            retrieved_at=source.retrieved_at,
            source_game_id=source.source_game_id,
        )
        captures.append(forecast_capture)
        source_captures.append(forecast_capture)
        ordinal += 1
        weather_sources.append(
            replace(
                source,
                raw_capture_checksums=tuple(
                    capture.checksum for capture in source_captures
                ),
            )
        )
    inventory = repository.build_inventory(
        run_id=run_id,
        phase_attempt=1,
        observed_at=observed_at,
        phase_input_checksum=hashlib.sha256(b"phase-4-one-game").hexdigest(),
        raw_captures=captures,
        odds_events=(event, future_event),
        weather_evidence=weather_sources,
    )
    result, completed = repository.assemble_inventory(inventory)
    return repository, result, completed, run_id


def test_repository_persists_one_game_and_reconstructs_every_evidence_layer(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    persisted = repository.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    reopened = OddsWeatherRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
        clock=lambda: persisted.sealed_at,
    )
    reconstructed = reopened.get_by_snapshot_id(persisted.snapshot_id)

    assert len(reconstructed.snapshot.games) == 1
    assert reconstructed.snapshot.games[0].odds.provider_event_id == "odds-event-900001"
    assert reconstructed.snapshot.games[0].weather.nws is not None
    assert reconstructed.snapshot.games[0].weather.openweather is not None
    assert reconstructed.snapshot.canonical_json_bytes() == result.snapshot.canonical_json_bytes()
    assert reopened.load_retained_inventory(run_id, 1) == inventory
    assert len(inventory.provider_events) == 2
    assert len(inventory.future_provider_event_revisions) == 1
    assert len(inventory.odds_revision_inventory) == 5
    assert len(inventory.weather_revisions) == 3
    assert len(inventory.future_weather_revisions) == 1
    assert len(inventory.raw_captures) == 7
    assert len(inventory.selected_raw_capture_checksums) == 4
    assert not {
        inventory.future_provider_event_revisions[0].event.raw_capture_checksum,
        *inventory.future_weather_revisions[0].evidence.raw_capture_checksums,
    }.intersection(inventory.selected_raw_capture_checksums)
    assert reopened.get_for_run_attempt(run_id, 1) == reconstructed
    assert reopened.get_latest_for_run(run_id) == reconstructed
    assert reopened.list_attempt_evidence(run_id) == (
        reopened.get_attempt_evidence(run_id, 1),
    )
    assert reopened.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    ) == persisted


def test_configured_secret_inventory_does_not_change_clean_semantic_identity(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, _ = _one_game_repository(tmp_path, monkeypatch)
    validating_repository = OddsWeatherRepository(
        repository.database,
        artifact_root=repository.artifact_root,
        secret_values=("configured-secret-not-present-in-clean-evidence",),
        clock=lambda: inventory.observed_at + timedelta(seconds=1),
    )

    reassembled, rebound_inventory = validating_repository.assemble_inventory(inventory)

    assert reassembled.snapshot.canonical_json_bytes() == result.snapshot.canonical_json_bytes()
    assert reassembled.snapshot.checksum == result.snapshot.checksum
    assert canonical_json_bytes(rebound_inventory.identity_dict()) == canonical_json_bytes(
        inventory.identity_dict()
    )
    assert rebound_inventory.checksum == inventory.checksum
    persisted = validating_repository.persist_assembly(
        run_id=inventory.run_id,
        phase_attempt=inventory.phase_attempt,
        result=reassembled,
        inventory=rebound_inventory,
    )
    manifest = validating_repository.get_attempt_manifest(
        inventory.run_id,
        inventory.phase_attempt,
    )
    manifest_bytes = (
        repository.artifact_root
        / validating_repository.get_attempt_evidence(
            inventory.run_id,
            inventory.phase_attempt,
        ).manifest.relpath
    ).read_bytes()
    assert b"configured-secret-not-present-in-clean-evidence" not in manifest_bytes
    assert manifest.retained_inventory_checksum == inventory.checksum
    assert repository.persist_assembly(
        run_id=inventory.run_id,
        phase_attempt=inventory.phase_attempt,
        result=result,
        inventory=inventory,
    ) == persisted


def test_retained_inventory_checksum_binds_complete_identity_projection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _, _, inventory, _ = _one_game_repository(tmp_path, monkeypatch)
    assert set(inventory.identity_dict()) == {
        "as_of_time",
        "final_warnings",
        "observed_at",
        "odds_revision_inventory",
        "phase_attempt",
        "phase_input_checksum",
        "provider_event_inventory",
        "raw_capture_inventory",
        "requested_date",
        "run_id",
        "selected_raw_capture_checksums",
        "source_warnings",
        "upstream_baseball_intelligence_checksum",
        "upstream_baseball_intelligence_snapshot_id",
        "upstream_daily_slate_checksum",
        "upstream_daily_slate_snapshot_id",
        "upstream_game_state_checksum",
        "upstream_game_state_snapshot_id",
        "weather_revision_inventory",
    }

    raw = list(inventory.raw_captures)
    raw[0] = replace(raw[0], provider_timestamp=inventory.observed_at)

    provider_events = list(inventory.provider_events)
    provider_events[0] = replace(
        provider_events[0],
        event=replace(
            provider_events[0].event,
            retrieved_at=provider_events[0].event.retrieved_at + timedelta(seconds=1),
        ),
    )

    odds_events = list(inventory.provider_events)
    history = odds_events[0].event.mutable_history_rows()
    first_history = dict(history[0])
    first_history["price_american"] = float(first_history["price_american"]) + 1
    history[0] = first_history
    odds_events[0] = replace(
        odds_events[0],
        event=replace(odds_events[0].event, history_rows=tuple(history)),
    )

    weather = list(inventory.weather_revisions)
    weather[0] = replace(
        weather[0],
        evidence=replace(
            weather[0].evidence,
            retrieved_at=weather[0].evidence.retrieved_at + timedelta(seconds=1),
        ),
    )

    source_warning = OddsWeatherWarningV1(
        code="checksum_audit_source_warning",
        domain=OddsWeatherWarningDomain.ODDS,
        message="Deterministic checksum-audit source warning",
    )
    assert inventory.final_warnings
    final_warnings = list(inventory.final_warnings)
    final_warnings[0] = replace(
        final_warnings[0],
        message=f"{final_warnings[0].message} (checksum audit)",
    )

    mutations = {
        "run_id": replace(
            inventory,
            run_id="run_20260731_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        ),
        "phase_attempt": replace(inventory, phase_attempt=2),
        "requested_date": replace(inventory, requested_date="2026-07-29"),
        "as_of_time": replace(
            inventory,
            as_of_time=inventory.as_of_time + timedelta(seconds=1),
        ),
        "observed_at": replace(
            inventory,
            observed_at=inventory.observed_at + timedelta(seconds=1),
        ),
        "phase_input_checksum": replace(
            inventory,
            phase_input_checksum=hashlib.sha256(b"other phase input").hexdigest(),
        ),
        "daily_slate_snapshot_id": replace(
            inventory,
            upstream_daily_slate_snapshot_id="daily-slate:other",
        ),
        "daily_slate_checksum": replace(
            inventory,
            upstream_daily_slate_checksum=hashlib.sha256(b"other slate").hexdigest(),
        ),
        "game_state_snapshot_id": replace(
            inventory,
            upstream_game_state_snapshot_id="game-state:other",
        ),
        "game_state_checksum": replace(
            inventory,
            upstream_game_state_checksum=hashlib.sha256(b"other state").hexdigest(),
        ),
        "bia_snapshot_id": replace(
            inventory,
            upstream_baseball_intelligence_snapshot_id="bia:other",
        ),
        "bia_checksum": replace(
            inventory,
            upstream_baseball_intelligence_checksum=hashlib.sha256(b"other bia").hexdigest(),
        ),
        "raw_capture_identity": replace(inventory, raw_captures=tuple(raw)),
        "provider_event_revision_identity": replace(
            inventory,
            provider_events=tuple(provider_events),
        ),
        "odds_revision_identity": replace(
            inventory,
            provider_events=tuple(odds_events),
        ),
        "weather_revision_identity": replace(
            inventory,
            weather_revisions=tuple(weather),
        ),
        "source_warnings": replace(
            inventory,
            source_warnings=(*inventory.source_warnings, source_warning),
        ),
        "final_warnings": replace(inventory, final_warnings=tuple(final_warnings)),
        "selected_raw_capture_inventory": replace(
            inventory,
            selected_raw_capture_checksums=inventory.selected_raw_capture_checksums[:-1],
        ),
    }

    for field, changed in mutations.items():
        assert changed != inventory, field
        assert changed.identity_dict() != inventory.identity_dict(), field
        assert changed.checksum != inventory.checksum, field


def test_repository_preserves_source_warnings_and_final_canonical_order(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, _, completed, run_id = _one_game_repository(tmp_path, monkeypatch)
    source_warning = OddsWeatherWarningV1(
        code="collector_excluded_row",
        domain=OddsWeatherWarningDomain.ODDS,
        message="Collector excluded one malformed retained row",
        provider="the_odds_api",
        provider_event_id="excluded-event",
    )
    supplied = replace(
        completed,
        source_warnings=(source_warning,),
        final_warnings=(),
        selected_raw_capture_checksums=(),
    )
    result, inventory = repository.assemble_inventory(supplied)
    persisted = repository.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    manifest = repository.get_attempt_manifest(run_id, 1)

    assert manifest.source_warnings == (source_warning,)
    assert source_warning in manifest.final_warnings
    assert persisted.snapshot.warnings == manifest.final_warnings
    assert repository.get_by_snapshot_id(persisted.snapshot_id).snapshot.warnings == (
        manifest.final_warnings
    )
