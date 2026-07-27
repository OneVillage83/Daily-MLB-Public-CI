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
from app.run_controller.service import ManualRunController, ManualRunExecutionBlocked
from app.stats.contracts import FixtureResponse
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


def _build(
    tmp_path: Path,
    *,
    venue_name: str = "Dodger Stadium",
) -> tuple[
    ManualRunController,
    DailySlateRepository,
    Path,
    bytes,
]:
    database = Database(tmp_path / "pipeline.db")
    artifact_root = tmp_path / "artifacts"
    raw_store = RawArtifactStore(
        artifact_root / DAILY_SLATE_RAW_PROVIDER_DIRECTORY
    )
    body = json.dumps(
        _payload(venue_name=venue_name),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    transport = FixtureStatsTransport(
        raw_store,
        {
            MLB_SCHEDULE_FIXTURE_KEY: FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
        clock=_clock,
    )
    handler = DailySlatePhaseHandler(
        database,
        artifact_root=artifact_root,
        request_timeout_seconds=30,
        user_agent="Daily-MLB-DS1B-Test/1.0",
        transport=transport,
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
    return controller, DailySlateRepository(database, clock=_clock), artifact_root, body


def test_daily_slate_executes_then_controller_blocks_safely_at_game_state(
    tmp_path: Path,
) -> None:
    controller, slate_repository, artifact_root, raw_body = _build(tmp_path)
    created = controller.start("2026-07-27")

    assert created.run.database_schema_version == CURRENT_SCHEMA_VERSION == 8
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

    raw_link = artifact_root / daily_slate_raw_link_relpath(created.run.run_id, 1)
    raw_manifest = json.loads(raw_link.read_text(encoding="utf-8"))
    assert raw_manifest["contract_version"] == DAILY_SLATE_RAW_LINK_CONTRACT
    assert raw_manifest["run_id"] == created.run.run_id
    assert raw_manifest["phase_attempt"] == 1
    assert raw_manifest["requested_date"] == "2026-07-27"
    assert raw_manifest["raw_checksum_sha256"] == daily_slate.input_checksum
    assert raw_manifest["provider"] == "mlb"
    assert raw_manifest["request"]["params"]["date"] == "2026-07-27"
    retained_raw = artifact_root / raw_manifest["raw_artifact_relpath"]
    assert retained_raw.read_bytes() == raw_body


def test_noncritical_venue_gap_returns_succeeded_with_warnings_and_still_blocks_next(
    tmp_path: Path,
) -> None:
    controller, slate_repository, _, _ = _build(
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
