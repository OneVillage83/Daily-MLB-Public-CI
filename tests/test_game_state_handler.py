from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.daily_slate.handler import (
    DAILY_SLATE_RAW_PROVIDER_DIRECTORY,
    DailySlatePhaseHandler,
)
from app.daily_slate.acquisition import MLB_SCHEDULE_FIXTURE_KEY
from app.database import Database
from app.game_state import (
    GAME_STATE_RAW_PROVIDER_DIRECTORY,
    GameStatePhaseHandler,
    GameStateRawLinkOutcome,
    game_state_raw_link_relpath,
)
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.repository import PipelineRunRepository
from app.run_controller.service import (
    ManualRunController,
    ManualRunExecutionBlocked,
    PhaseExecutionContext,
)
from app.stats.contracts import FixtureResponse
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport


NOW = datetime(2026, 7, 29, 15, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _schedule_body(*, games: int = 1) -> bytes:
    game_rows = [
        {
            "gamePk": 900000 + ordinal,
            "gameDate": f"2026-07-29T{21 + ordinal:02d}:10:00Z",
            "officialDate": "2026-07-29",
            "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
            "teams": {
                "away": {"team": {"id": 137, "name": "San Francisco Giants"}},
                "home": {"team": {"id": 119, "name": "Los Angeles Dodgers"}},
            },
            "venue": {"id": 22, "name": "Dodger Stadium"},
            "gameNumber": ordinal,
            "doubleHeader": "N",
        }
        for ordinal in range(1, games + 1)
    ]
    return json.dumps(
        {"totalGames": games, "dates": [{"date": "2026-07-29", "totalGames": games, "games": game_rows}]},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _feed_body(game_pk: int) -> bytes:
    return json.dumps(
        {
            "gamePk": game_pk,
            "gameData": {
                "datetime": {"dateTime": f"2026-07-29T{21 + game_pk - 900000:02d}:10:00Z"},
                "status": {"abstractGameState": "Preview", "detailedState": "Pre-Game"},
                "teams": {
                    "away": {"id": 137, "name": "San Francisco Giants"},
                    "home": {"id": 119, "name": "Los Angeles Dodgers"},
                },
                "players": {},
                "probablePitchers": {},
            },
            "liveData": {},
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _controller(tmp_path: Path, *, games: int = 1, include_feeds: bool = True) -> tuple[ManualRunController, GameStatePhaseHandler, Path]:
    database = Database(tmp_path / "pipeline.db")
    root = tmp_path / "artifacts"
    schedule_store = RawArtifactStore(root / DAILY_SLATE_RAW_PROVIDER_DIRECTORY)
    schedule_transport = FixtureStatsTransport(
        schedule_store,
        {MLB_SCHEDULE_FIXTURE_KEY: FixtureResponse(body=_schedule_body(games=games), content_type="application/json")},
        clock=_clock,
    )
    game_store = RawArtifactStore(root / GAME_STATE_RAW_PROVIDER_DIRECTORY)
    game_fixtures = {
        f"mlb_game_state_feed:{900000 + ordinal}": FixtureResponse(body=_feed_body(900000 + ordinal), content_type="application/json")
        for ordinal in range(1, games + 1)
    } if include_feeds else {}
    game_transport = FixtureStatsTransport(game_store, game_fixtures, clock=_clock)
    daily = DailySlatePhaseHandler(database, artifact_root=root, request_timeout_seconds=30, user_agent="Daily-MLB-Test/1.0", transport=schedule_transport, raw_store=schedule_store, clock=_clock)
    state = GameStatePhaseHandler(database, artifact_root=root, request_timeout_seconds=30, user_agent="Daily-MLB-Test/1.0", transport=game_transport, raw_store=game_store, clock=_clock)
    controller = ManualRunController(
        PipelineRunRepository(database, repository_root=tmp_path / "not-a-git"),
        timezone_name="America/Los_Angeles",
        configuration_metadata={"execution": {"network_enabled": False}},
        handlers={PipelinePhaseKey.DAILY_SLATE: daily, PipelinePhaseKey.GAME_STATE: state},
        clock=_clock,
    )
    return controller, state, root


def test_handler_executes_after_daily_slate_then_blocks_at_phase_three(tmp_path: Path) -> None:
    controller, _, root = _controller(tmp_path)
    run = controller.start("2026-07-29").run
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.execute(run.run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY
    summary = controller.show(run.run_id)
    daily, state, assembly = summary.phases[:3]
    assert daily.status is PipelinePhaseStatus.SUCCEEDED
    assert state.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert state.attempt_count == 1
    assert state.input_checksum == daily.output_checksum
    assert state.output_checksum is not None
    assert state.artifact_relpath == f"game_state/snapshots/{state.output_checksum}/game_state_v1.json"
    assert assembly.status is PipelinePhaseStatus.PENDING
    assert (root / state.artifact_relpath).is_file()


def test_zero_game_handler_makes_no_game_feed_requests_and_persists_snapshot(tmp_path: Path) -> None:
    controller, handler, _ = _controller(tmp_path, games=0)
    run = controller.start("2026-07-29").run
    with pytest.raises(ManualRunExecutionBlocked):
        controller.execute(run.run_id)
    summary = controller.show(run.run_id)
    state = summary.phases[1]
    assert state.status is PipelinePhaseStatus.SUCCEEDED
    assert isinstance(handler.transport, FixtureStatsTransport)
    assert handler.transport.requests == []
    persisted = handler.repository.get_latest_game_state_for_run(run.run_id)
    assert persisted is not None
    assert persisted.state.games == ()


def test_acquisition_failure_retains_immutable_attempt_evidence(tmp_path: Path) -> None:
    controller, handler, root = _controller(tmp_path, include_feeds=False)
    run = controller.start("2026-07-29").run
    with pytest.raises(Exception):
        controller.execute(run.run_id)
    evidence = handler.repository.get_attempt_evidence(run.run_id, 1)
    assert evidence.outcome is GameStateRawLinkOutcome.ACQUISITION_FAILED
    assert handler.repository.get_latest_game_state_for_run(run.run_id) is None
    assert (root / game_state_raw_link_relpath(run.run_id, 1)).is_file()


def test_controller_retry_preserves_failed_attempt_then_persists_attempt_two(tmp_path: Path) -> None:
    controller, handler, root = _controller(tmp_path, include_feeds=False)
    run = controller.start("2026-07-29").run
    with pytest.raises(Exception):
        controller.execute(run.run_id)
    first = handler.repository.get_attempt_evidence(run.run_id, 1)
    assert first.outcome is GameStateRawLinkOutcome.ACQUISITION_FAILED
    assert isinstance(handler.raw_store, RawArtifactStore)
    handler.transport = FixtureStatsTransport(
        handler.raw_store,
        {
            "mlb_game_state_feed:900001": FixtureResponse(
                body=_feed_body(900001), content_type="application/json"
            )
        },
        clock=_clock,
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run.run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY
    second = handler.repository.get_attempt_evidence(run.run_id, 2)
    assert second.outcome is GameStateRawLinkOutcome.NORMALIZED
    assert handler.repository.get_attempt_evidence(run.run_id, 1) == first
    assert (root / game_state_raw_link_relpath(run.run_id, 1)).is_file()
    assert (root / game_state_raw_link_relpath(run.run_id, 2)).is_file()
    persisted = handler.repository.get_game_state_for_run_attempt(run.run_id, 2)
    assert persisted.state.checksum == second.normalized_snapshot_checksum


def test_normalization_failure_retains_complete_raw_link_without_snapshot(tmp_path: Path) -> None:
    controller, handler, root = _controller(tmp_path)
    malformed = json.loads(_feed_body(900001))
    game_data = malformed["gameData"]
    assert isinstance(game_data, dict)
    teams = game_data["teams"]
    assert isinstance(teams, dict)
    home = teams["home"]
    assert isinstance(home, dict)
    home["id"] = 999
    assert isinstance(handler.raw_store, RawArtifactStore)
    handler.transport = FixtureStatsTransport(
        handler.raw_store,
        {
            "mlb_game_state_feed:900001": FixtureResponse(
                body=json.dumps(malformed, separators=(",", ":")).encode("utf-8"),
                content_type="application/json",
            )
        },
        clock=_clock,
    )
    run = controller.start("2026-07-29").run
    with pytest.raises(Exception):
        controller.execute(run.run_id)
    evidence = handler.repository.get_attempt_evidence(run.run_id, 1)
    assert evidence.outcome is GameStateRawLinkOutcome.NORMALIZATION_FAILED
    assert handler.repository.get_latest_game_state_for_run(run.run_id) is None
    assert (root / game_state_raw_link_relpath(run.run_id, 1)).is_file()


def test_handler_construction_and_context_guards(tmp_path: Path) -> None:
    database = Database(tmp_path / "state.db")
    with pytest.raises(ValueError, match="positive"):
        GameStatePhaseHandler(database, artifact_root=tmp_path, request_timeout_seconds=0, user_agent="agent")
    with pytest.raises(ValueError, match="at least one"):
        GameStatePhaseHandler(
            database,
            artifact_root=tmp_path,
            request_timeout_seconds=1,
            user_agent="agent",
            max_attempts=0,
        )
    with pytest.raises(ValueError, match="blank"):
        GameStatePhaseHandler(database, artifact_root=tmp_path, request_timeout_seconds=1, user_agent=" ")
    partial_store = RawArtifactStore(tmp_path / "partial-raw-store")
    partial_transport = FixtureStatsTransport(partial_store, {}, clock=_clock)
    with pytest.raises(ValueError, match="supplied together"):
        GameStatePhaseHandler(
            database,
            artifact_root=tmp_path,
            request_timeout_seconds=1,
            user_agent="agent",
            transport=partial_transport,
        )
    handler = GameStatePhaseHandler(database, artifact_root=tmp_path, request_timeout_seconds=1, user_agent="agent", clock=_clock)
    context = PhaseExecutionContext("run_20260729_11111111111111111111111111111111", "2026-07-29", NOW.isoformat(), "UTC", PipelinePhaseKey.DAILY_SLATE, 1, False, False, "test", "test", "a" * 64, "b" * 40)
    with pytest.raises(ValueError, match="GAME_STATE"):
        handler(context)
