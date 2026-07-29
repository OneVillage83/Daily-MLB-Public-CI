from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.daily_slate import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateRepository,
    DailySlateV1,
    VenueMappingStatus,
    daily_mlb_game_id,
    edge_event_id,
    write_daily_slate_artifact,
)
from app.database import Database
from app.game_state import (
    GamedayPersonnelV1,
    GameStateArtifactIntegrityError,
    GameStateGameV1,
    GameStateIntegrityError,
    GameStatePersistenceConflict,
    GameStateProvenanceV1,
    GameStateRawLinkOutcome,
    GameStateRepository,
    GameStateV1,
    LineupAvailability,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
    write_game_state_artifact,
)
from app.artifacts import resolve_contained_path
from app.run_controller.contracts import (
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import PipelineRunRepository


RUN_ID = "run_20260729_11111111111111111111111111111111"
NOW = datetime(2026, 7, 29, 15, tzinfo=timezone.utc)
START = datetime(2026, 7, 29, 23, 10, tzinfo=timezone.utc)


def _slate_game(source_game_id: str = "900001") -> DailySlateGameV1:
    provenance = DailySlateProvenanceV1(
        source_provider="mlb", source_record_id=source_game_id, observed_at=NOW,
        source_version="schedule-v1", raw_status="Scheduled", upstream_checksum="a" * 64,
    )
    return DailySlateGameV1(
        edge_event_id=edge_event_id(source_game_id), daily_mlb_game_id=daily_mlb_game_id(source_game_id),
        official_date="2026-07-29", scheduled_start_time=START, away_team_id="SF", home_team_id="LAD",
        venue_id="dodger-stadium-los-angeles", venue_mapping_status=VenueMappingStatus.RESOLVED,
        game_number=1, doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED, source_game_id=source_game_id,
        source_provider="mlb", observed_at=NOW, provenance=provenance,
        source_home_team_id="119", source_away_team_id="137", source_venue_id="22", source_venue_name="Dodger Stadium",
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    return DailySlateV1(
        requested_date="2026-07-29", as_of_time=NOW, observed_at=NOW,
        source_authority="mlb", source_version="schedule-v1", games=games,
        provenance=DailySlateProvenanceV1(source_provider="mlb", source_record_id=None, observed_at=NOW,
            source_version="schedule-v1", raw_status="schedule", upstream_checksum="a" * 64),
    )


def _team(team_id: str, source_team_id: str) -> TeamGameStateV1:
    return TeamGameStateV1(team_id=team_id, source_team_id=source_team_id,
        starter=StarterStateV1(certainty=StarterCertainty.UNAVAILABLE),
        lineup=LineupStateV1(availability=LineupAvailability.UNAVAILABLE),
        personnel=GamedayPersonnelV1(available=False))


def _state(slate: DailySlateV1) -> GameStateV1:
    games = tuple(
        GameStateGameV1(
            edge_event_id=game.edge_event_id, daily_mlb_game_id=game.daily_mlb_game_id,
            source_game_id=game.source_game_id, away_team_id=game.away_team_id, home_team_id=game.home_team_id,
            game_status=DailySlateGameStatus.PREGAME, away=_team("SF", "137"), home=_team("LAD", "119"),
            observed_at=NOW, provenance=GameStateProvenanceV1(source_provider="mlb", source_record_id=game.source_game_id,
                observed_at=NOW, source_version="statsapi-game-feed-v1", raw_status="Pre-Game", upstream_checksum=slate.checksum),
        ) for game in slate.games
    )
    return GameStateV1(requested_date=slate.requested_date, as_of_time=slate.as_of_time,
        observed_at=NOW, source_authority="mlb", source_version="statsapi-game-feed-v1",
        upstream_daily_slate_checksum=slate.checksum, games=games,
        provenance=GameStateProvenanceV1(source_provider="mlb", source_record_id=None, observed_at=NOW,
            source_version="statsapi-game-feed-v1", raw_status="snapshot", upstream_checksum=slate.checksum))


def _setup(tmp_path: Path, slate: DailySlateV1) -> tuple[GameStateRepository, PipelineRunRepository, str]:
    database = Database(tmp_path / "state.db")
    pipeline = PipelineRunRepository(database, repository_root=tmp_path / "not-a-git-repository")
    pipeline.create_pipeline_run(requested_date=slate.requested_date, as_of_time=NOW,
        timezone_name="America/Los_Angeles", pipeline_version="test", configuration_version="test",
        configuration_metadata={"network_enabled": False}, code_revision="f" * 40, run_id=RUN_ID, created_at=NOW.isoformat())
    pipeline.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING, transitioned_at=NOW.isoformat())
    pipeline.transition_pipeline_phase(RUN_ID, PipelinePhaseKey.DAILY_SLATE, PipelinePhaseStatus.RUNNING, transitioned_at=NOW.isoformat())
    root = tmp_path / "artifacts"
    slate_repo = DailySlateRepository(database, clock=lambda: NOW)
    slate_artifact = write_daily_slate_artifact(slate, root)
    persisted = slate_repo.persist_daily_slate(run_id=RUN_ID, phase_attempt=1, slate=slate, artifact=slate_artifact)
    pipeline.transition_pipeline_phase(RUN_ID, PipelinePhaseKey.DAILY_SLATE, PipelinePhaseStatus.SUCCEEDED, transitioned_at=NOW.isoformat())
    pipeline.transition_pipeline_phase(RUN_ID, PipelinePhaseKey.GAME_STATE, PipelinePhaseStatus.RUNNING, transitioned_at=NOW.isoformat())
    return GameStateRepository(database, artifact_root=root, clock=lambda: NOW), pipeline, persisted.snapshot_id


def _raw_link(repo: GameStateRepository, state: GameStateV1, outcome: GameStateRawLinkOutcome, *, attempt: int = 1, normalized_checksum: str | None = None) -> str:
    relpath = f"game_state/raw_links/{RUN_ID}/attempt_{attempt:04d}.json"
    payload = {
        "contract_version": "DSE_GAME_STATE_RAW_LINK_V1", "evidence_game_count": len(state.games),
        "failure": None, "game_state_checksum": normalized_checksum, "games": [], "outcome": outcome.value,
        "phase_attempt": attempt, "requested_date": state.requested_date, "run_id": RUN_ID,
        "upstream_daily_slate_checksum": state.upstream_daily_slate_checksum,
    }
    destination = resolve_contained_path(repo.artifact_root, relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return relpath


def test_persists_and_reconstructs_normalized_snapshot_offline(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    raw_link = _raw_link(repo, state, GameStateRawLinkOutcome.NORMALIZED, normalized_checksum=state.checksum)
    attempt = repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome="normalized", normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=raw_link, warnings=("starter unavailable", "starter unavailable"))
    artifact = write_game_state_artifact(state, repo.artifact_root)
    persisted = repo.persist_game_state(run_id=RUN_ID, phase_attempt=1, state=state, artifact=artifact)
    reopened = GameStateRepository(Database(repo.database.path), artifact_root=repo.artifact_root, clock=lambda: NOW)
    reconstructed = reopened.get_game_state_snapshot(persisted.snapshot_id)
    assert attempt.warnings == ("starter unavailable",)
    assert reconstructed.state.canonical_json_bytes() == state.canonical_json_bytes()
    assert reconstructed.artifact == artifact
    assert reconstructed.upstream_daily_slate_snapshot_id


@pytest.mark.parametrize("outcome", [GameStateRawLinkOutcome.ACQUISITION_FAILED, GameStateRawLinkOutcome.NORMALIZATION_FAILED])
def test_failed_attempt_evidence_is_retained_without_snapshot(tmp_path: Path, outcome: GameStateRawLinkOutcome) -> None:
    slate = _slate(_slate_game())
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    link = _raw_link(repo, state, outcome)
    evidence = repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome=outcome, raw_link_relpath=link, warnings=("safe warning",))
    assert evidence.outcome is outcome
    assert repo.get_latest_game_state_for_run(RUN_ID) is None


def test_duplicate_attempt_cannot_replace_retained_evidence(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    link = _raw_link(repo, state, GameStateRawLinkOutcome.ACQUISITION_FAILED)
    first = repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome="acquisition_failed", raw_link_relpath=link)
    with pytest.raises(GameStatePersistenceConflict):
        repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
            upstream_daily_slate_checksum=slate.checksum, outcome="acquisition_failed", raw_link_relpath=link)
    assert repo.get_attempt_evidence(RUN_ID, 1) == first


def test_normalized_snapshot_rejects_reordered_or_unmatched_daily_slate_games(tmp_path: Path) -> None:
    slate = _slate(_slate_game("900001"), _slate_game("900002"))
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    reordered = GameStateV1(
        requested_date=state.requested_date, as_of_time=state.as_of_time, observed_at=state.observed_at,
        source_authority=state.source_authority, source_version=state.source_version,
        upstream_daily_slate_checksum=state.upstream_daily_slate_checksum, games=tuple(reversed(state.games)),
        provenance=state.provenance,
    )
    link = _raw_link(repo, reordered, GameStateRawLinkOutcome.NORMALIZED, normalized_checksum=reordered.checksum)
    repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome="normalized", normalized_snapshot_checksum=reordered.checksum,
        raw_link_relpath=link)
    with pytest.raises(GameStatePersistenceConflict, match="identities"):
        repo.persist_game_state(run_id=RUN_ID, phase_attempt=1, state=reordered,
            artifact=write_game_state_artifact(reordered, repo.artifact_root))


def test_attempt_evidence_rejects_unsafe_path_and_inactive_phase(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    repo, pipeline, _ = _setup(tmp_path, slate)
    state = _state(slate)
    with pytest.raises(ValueError):
        repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
            upstream_daily_slate_checksum=slate.checksum, outcome="acquisition_failed", raw_link_relpath="../escape.json")
    pipeline.transition_pipeline_phase(RUN_ID, PipelinePhaseKey.GAME_STATE, PipelinePhaseStatus.FAILED,
        error={"code": "fixture_failure"}, transitioned_at=NOW.isoformat())
    link = _raw_link(repo, state, GameStateRawLinkOutcome.ACQUISITION_FAILED)
    with pytest.raises(GameStatePersistenceConflict, match="active phase"):
        repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
            upstream_daily_slate_checksum=slate.checksum, outcome="acquisition_failed", raw_link_relpath=link)


def test_tampered_artifact_and_raw_link_fail_closed(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    link = _raw_link(repo, state, GameStateRawLinkOutcome.NORMALIZED, normalized_checksum=state.checksum)
    raw_path = resolve_contained_path(repo.artifact_root, link)
    raw_path.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception, match="raw-link"):
        repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
            upstream_daily_slate_checksum=slate.checksum, outcome="normalized", normalized_snapshot_checksum=state.checksum,
            raw_link_relpath=link)
    link = _raw_link(repo, state, GameStateRawLinkOutcome.NORMALIZED, normalized_checksum=state.checksum)
    repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome="normalized", normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=link)
    artifact = write_game_state_artifact(state, repo.artifact_root)
    resolve_contained_path(repo.artifact_root, artifact.relpath).write_bytes(b"tampered")
    with pytest.raises(GameStateArtifactIntegrityError):
        repo.persist_game_state(run_id=RUN_ID, phase_attempt=1, state=state, artifact=artifact)


def test_zero_game_state_seals_and_database_immutability_holds(tmp_path: Path) -> None:
    slate = _slate()
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    link = _raw_link(repo, state, GameStateRawLinkOutcome.NORMALIZED, normalized_checksum=state.checksum)
    repo.persist_attempt_evidence(run_id=RUN_ID, phase_attempt=1, requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum, outcome="normalized", normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=link)
    persisted = repo.persist_game_state(run_id=RUN_ID, phase_attempt=1, state=state,
        artifact=write_game_state_artifact(state, repo.artifact_root))
    with repo.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM game_state_snapshots WHERE snapshot_id=?", (persisted.snapshot_id,))
    assert repo.database.integrity_check()["ok"] is True


def test_retrieval_rejects_tampered_child_identity_columns(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    repo, _, _ = _setup(tmp_path, slate)
    state = _state(slate)
    link = _raw_link(
        repo,
        state,
        GameStateRawLinkOutcome.NORMALIZED,
        normalized_checksum=state.checksum,
    )
    repo.persist_attempt_evidence(
        run_id=RUN_ID,
        phase_attempt=1,
        requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum,
        outcome="normalized",
        normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=link,
    )
    persisted = repo.persist_game_state(
        run_id=RUN_ID,
        phase_attempt=1,
        state=state,
        artifact=write_game_state_artifact(state, repo.artifact_root),
    )
    with repo.database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER game_state_games_reject_update")
        connection.execute(
            "UPDATE game_state_games SET away_team_id='TAMPERED' WHERE snapshot_id=?",
            (persisted.snapshot_id,),
        )
    with pytest.raises(GameStateIntegrityError, match="child columns"):
        repo.get_game_state_snapshot(persisted.snapshot_id)
