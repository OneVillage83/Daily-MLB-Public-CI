from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.daily_slate.acquisition import MLB_SCHEDULE_FIXTURE_KEY
from app.daily_slate.handler import (
    DAILY_SLATE_RAW_LINK_CONTRACT,
    DAILY_SLATE_RAW_PROVIDER_DIRECTORY,
    DailySlatePhaseHandler,
    daily_slate_raw_link_relpath,
)
from app.daily_slate.repository import DailySlateRepository
from app.database import Database
from app.migrations import CURRENT_SCHEMA_VERSION
from app.run_controller.contracts import (
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import PipelineRunRepository
from app.run_controller.service import (
    ManualRunController,
    ManualRunExecutionBlocked,
    ManualRunExecutionError,
)
from app.stats.contracts import (
    FixtureResponse,
    StatsRequest,
    StatsResponse,
    StatsTransportError,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _payload(*, venue_name: str = "Dodger Stadium") -> dict[str, object]:
    return {
        "totalGames": 1,
        "dates": [
            {
                "date": "2026-07-27",
                "totalGames": 1,
                "games": [
                    {
                        "gamePk": 900001,
                        "gameDate": "2026-07-27T23:10:00Z",
                        "officialDate": "2026-07-27",
                        "status": {
                            "abstractGameState": "Preview",
                            "codedGameState": "S",
                            "detailedState": "Scheduled",
                            "statusCode": "S",
                            "startTimeTBD": False,
                        },
                        "teams": {
                            "away": {
                                "team": {
                                    "id": 137,
                                    "name": "San Francisco Giants",
                                }
                            },
                            "home": {
                                "team": {
                                    "id": 119,
                                    "name": "Los Angeles Dodgers",
                                }
                            },
                        },
                        "venue": {"id": 22, "name": venue_name},
                        "gameNumber": 1,
                        "doubleHeader": "N",
                    }
                ],
            }
        ],
    }


def _body(*, venue_name: str = "Dodger Stadium") -> bytes:
    return json.dumps(
        _payload(venue_name=venue_name),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _fixture_transport(raw_store: RawArtifactStore, body: bytes) -> FixtureStatsTransport:
    return FixtureStatsTransport(
        raw_store,
        {
            MLB_SCHEDULE_FIXTURE_KEY: FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
        clock=_clock,
    )


def _build(
    tmp_path: Path,
    *,
    venue_name: str = "Dodger Stadium",
) -> tuple[
    ManualRunController,
    DailySlateRepository,
    Path,
    bytes,
    DailySlatePhaseHandler,
]:
    database = Database(tmp_path / "pipeline.db")
    artifact_root = tmp_path / "artifacts"
    raw_store = RawArtifactStore(
        artifact_root / DAILY_SLATE_RAW_PROVIDER_DIRECTORY
    )
    body = _body(venue_name=venue_name)
    handler = DailySlatePhaseHandler(
        database,
        artifact_root=artifact_root,
        request_timeout_seconds=30,
        user_agent="Daily-MLB-DS1B-Test/1.0",
        transport=_fixture_transport(raw_store, body),
        raw_store=raw_store,
        clock=_clock,
    )
    pipeline_repository = PipelineRunRepository(
        database,
        repository_root=tmp_path / "not-a-git-repository",
    )
    controller = ManualRunController(
        pipeline_repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={
            "daily_slate": {
                "network_enabled": False,
                "provider": "mlb",
                "source_version": "statsapi-v1",
            },
            "execution": {"mode": "manual"},
        },
        handlers={PipelinePhaseKey.DAILY_SLATE: handler},
        clock=_clock,
    )
    return (
        controller,
        DailySlateRepository(database, clock=_clock),
        artifact_root,
        body,
        handler,
    )


def _raw_manifest(artifact_root: Path, run_id: str, attempt: int) -> dict[str, object]:
    path = artifact_root / daily_slate_raw_link_relpath(run_id, attempt)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_daily_slate_executes_then_controller_blocks_safely_at_game_state(
    tmp_path: Path,
) -> None:
    controller, slate_repository, artifact_root, raw_body, _ = _build(tmp_path)
    created = controller.start("2026-07-27")

    assert created.run.database_schema_version == CURRENT_SCHEMA_VERSION == 14
    assert created.run.status is PipelineRunStatus.PENDING

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.execute(created.run.run_id)

    assert blocked.value.phase_key is PipelinePhaseKey.GAME_STATE
    persisted = controller.show(created.run.run_id)
    assert persisted.run.status is PipelineRunStatus.RUNNING

    daily_slate = persisted.phases[0]
    game_state = persisted.phases[1]
    assert daily_slate.phase_key is PipelinePhaseKey.DAILY_SLATE
    assert daily_slate.status is PipelinePhaseStatus.SUCCEEDED
    assert daily_slate.attempt_count == 1
    assert daily_slate.input_checksum == hashlib.sha256(raw_body).hexdigest()
    assert daily_slate.output_checksum is not None
    assert daily_slate.artifact_relpath is not None
    assert daily_slate.warnings is None
    assert game_state.phase_key is PipelinePhaseKey.GAME_STATE
    assert game_state.status is PipelinePhaseStatus.PENDING
    assert game_state.attempt_count == 0

    snapshot = slate_repository.get_latest_daily_slate_for_run(created.run.run_id)
    assert snapshot is not None
    assert snapshot.phase_attempt == 1
    assert snapshot.slate.checksum == daily_slate.output_checksum
    assert snapshot.artifact_relpath == daily_slate.artifact_relpath
    assert [game.source_game_id for game in snapshot.slate.games] == ["900001"]

    canonical_path = artifact_root / daily_slate.artifact_relpath
    assert canonical_path.read_bytes() == snapshot.slate.canonical_json_bytes()

    raw_manifest = _raw_manifest(artifact_root, created.run.run_id, 1)
    assert raw_manifest["contract_version"] == DAILY_SLATE_RAW_LINK_CONTRACT
    assert raw_manifest["run_id"] == created.run.run_id
    assert raw_manifest["phase_attempt"] == 1
    assert raw_manifest["requested_date"] == "2026-07-27"
    assert raw_manifest["evidence_state"] == "normalized"
    assert raw_manifest["error"] is None
    assert raw_manifest["final_raw_checksum_sha256"] == daily_slate.input_checksum
    assert raw_manifest["provider"] == "mlb"
    request = raw_manifest["request"]
    assert isinstance(request, dict)
    params = request["params"]
    assert isinstance(params, dict)
    assert params["date"] == "2026-07-27"
    captures = raw_manifest["captures"]
    assert isinstance(captures, list)
    assert len(captures) == 1
    capture = captures[0]
    assert isinstance(capture, dict)
    retained_raw = artifact_root / str(capture["raw_artifact_relpath"])
    assert retained_raw.read_bytes() == raw_body


def test_noncritical_venue_gap_returns_succeeded_with_warnings_and_still_blocks_next(
    tmp_path: Path,
) -> None:
    controller, slate_repository, _, _, _ = _build(
        tmp_path,
        venue_name="Unmapped Neutral Site",
    )
    created = controller.start("2026-07-27")

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.execute(created.run.run_id)

    assert blocked.value.phase_key is PipelinePhaseKey.GAME_STATE
    persisted = controller.show(created.run.run_id)
    daily_slate = persisted.phases[0]
    assert daily_slate.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert daily_slate.warnings == [
        {
            "code": "unresolved_venue",
            "message": (
                "MLB venue evidence could not be resolved to a canonical physical venue"
            ),
            "source_game_id": "900001",
        }
    ]
    assert all(
        phase.status is not PipelinePhaseStatus.DEGRADED for phase in persisted.phases
    )

    snapshot = slate_repository.get_latest_daily_slate_for_run(created.run.run_id)
    assert snapshot is not None
    assert snapshot.slate.games[0].venue_id is None


def test_invalid_json_failure_is_linked_to_phase_attempt_and_retry_preserves_lineage(
    tmp_path: Path,
) -> None:
    controller, slate_repository, artifact_root, good_body, handler = _build(tmp_path)
    bad_body = b"{not-json"
    handler.transport = _fixture_transport(handler.raw_store, bad_body)
    created = controller.start("2026-07-27")

    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)

    failed = controller.show(created.run.run_id)
    assert failed.run.status is PipelineRunStatus.FAILED
    assert failed.phases[0].status is PipelinePhaseStatus.FAILED
    assert failed.phases[0].attempt_count == 1
    assert slate_repository.get_latest_daily_slate_for_run(created.run.run_id) is None

    failed_manifest = _raw_manifest(artifact_root, created.run.run_id, 1)
    assert failed_manifest["evidence_state"] == "acquisition_failed"
    assert failed_manifest["phase_attempt"] == 1
    assert failed_manifest["final_raw_checksum_sha256"] == hashlib.sha256(bad_body).hexdigest()
    failed_error = failed_manifest["error"]
    assert isinstance(failed_error, dict)
    assert failed_error["error_type"] == "DailySlateAcquisitionError"
    failed_captures = failed_manifest["captures"]
    assert isinstance(failed_captures, list)
    assert len(failed_captures) == 1

    handler.transport = _fixture_transport(handler.raw_store, good_body)
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(created.run.run_id)

    assert blocked.value.phase_key is PipelinePhaseKey.GAME_STATE
    recovered = controller.show(created.run.run_id)
    assert recovered.run.status is PipelineRunStatus.RUNNING
    assert recovered.phases[0].status is PipelinePhaseStatus.SUCCEEDED
    assert recovered.phases[0].attempt_count == 2
    assert recovered.phases[0].input_checksum == hashlib.sha256(good_body).hexdigest()

    attempt_one_again = _raw_manifest(artifact_root, created.run.run_id, 1)
    assert attempt_one_again == failed_manifest
    attempt_two = _raw_manifest(artifact_root, created.run.run_id, 2)
    assert attempt_two["evidence_state"] == "normalized"
    assert attempt_two["phase_attempt"] == 2
    assert attempt_two["error"] is None
    snapshot = slate_repository.get_latest_daily_slate_for_run(created.run.run_id)
    assert snapshot is not None
    assert snapshot.phase_attempt == 2


def test_transport_failure_manifest_links_every_retained_http_attempt(tmp_path: Path) -> None:
    controller, _, artifact_root, _, handler = _build(tmp_path)

    class MultiAttemptFailureTransport:
        def fetch(self, request: StatsRequest) -> StatsResponse:
            first = handler.raw_store.retain_bytes(
                provider=request.provider,
                endpoint_category=request.endpoint_category,
                payload=b'{"error":"temporary-1"}',
                retrieved_at=NOW,
                content_type="application/json",
            )
            second = handler.raw_store.retain_bytes(
                provider=request.provider,
                endpoint_category=request.endpoint_category,
                payload=b'{"error":"temporary-2"}',
                retrieved_at=NOW,
                content_type="application/json",
            )
            raise StatsTransportError(
                "authoritative MLB schedule unavailable after retries",
                attempts=2,
                status_code=503,
                captures=(first, second),
            )

    handler.transport = MultiAttemptFailureTransport()
    created = controller.start("2026-07-27")

    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)

    manifest = _raw_manifest(artifact_root, created.run.run_id, 1)
    assert manifest["evidence_state"] == "acquisition_failed"
    assert manifest["http_attempts"] == 2
    assert manifest["http_status"] == 503
    captures = manifest["captures"]
    assert isinstance(captures, list)
    assert len(captures) == 2
    assert [capture["ordinal"] for capture in captures if isinstance(capture, dict)] == [1, 2]
    assert manifest["final_raw_checksum_sha256"] == hashlib.sha256(
        b'{"error":"temporary-2"}'
    ).hexdigest()
