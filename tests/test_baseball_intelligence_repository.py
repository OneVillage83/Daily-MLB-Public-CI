from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pytest import MonkeyPatch

from app.baseball_intelligence import (
    BaseballIntelligenceAttemptOutcome,
    BaseballIntelligenceRepository,
)
from app.game_state import GameStateRawLinkOutcome, write_game_state_artifact
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from tests import test_game_state_repository as game_state_repository_tests
from tests.test_baseball_intelligence_migration_v10_roundtrip import (
    _fixture as _six_category_fixture,
    _persist_feature_inventory,
)
from tests.test_game_state_repository import RUN_ID, _raw_link, _setup, _slate, _state


def _persist_verified_game_state_mappings(
    repository: object,
    fixture: object,
) -> None:
    """Populate only the frozen verified mappings required by GameState persistence."""
    # The fixture's feature inventory owns canonical-player rows; this helper
    # provides the corresponding authoritative MLB source-identity crosswalk.
    state = fixture.state  # type: ignore[attr-defined]
    observed_at = state.observed_at.isoformat()
    players = {
        player.source_player_id: player
        for game in state.games
        for team in (game.away, game.home)
        for player in (
            *((team.starter.player,) if team.starter.player is not None else ()),
            *(entry.player for entry in team.lineup.entries),
            *team.personnel.batters,
            *team.personnel.pitchers,
            *team.personnel.bench,
            *team.personnel.bullpen,
        )
        if player.resolved
    }
    with repository.database.connect(write=True) as connection:  # type: ignore[attr-defined]
        _persist_feature_inventory(connection, fixture)  # type: ignore[arg-type]
        for source_player_id, player in sorted(players.items()):
            identity_checksum = hashlib.sha256(
                f"identity:{source_player_id}".encode("utf-8")
            ).hexdigest()
            mapping_checksum = hashlib.sha256(
                f"mapping:{source_player_id}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """INSERT INTO stats_player_identities(
                    player_identity_id,provider,provider_player_id,full_name,
                    primary_position,bats,throws,active,first_seen_at,last_seen_at,
                    identity_checksum
                ) VALUES (?, 'mlb', ?, ?, NULL, NULL, NULL, 1, ?, ?, ?)""",
                (
                    player.player_identity_id,
                    source_player_id,
                    player.full_name,
                    observed_at,
                    observed_at,
                    identity_checksum,
                ),
            )
            connection.execute(
                """INSERT INTO stats_player_identifier_mappings(
                    mapping_id,stats_run_id,canonical_player_id,player_identity_id,
                    mapping_method,verification_status,source_version,adapter_version,
                    observed_at,provenance_json,source_checksum
                ) VALUES (?, 'stats-run-blocked', ?, ?, 'source_declared', 'verified',
                    'fixture-v10', 'fixture-v10', ?, ?, ?)""",
                (
                    f"mapping:mlb:{source_player_id}",
                    player.canonical_player_id,
                    player.player_identity_id,
                    observed_at,
                    json.dumps({"fixture": True}, separators=(",", ":"), sort_keys=True),
                    mapping_checksum,
                ),
            )


def _zero_game_repository(tmp_path: Path) -> BaseballIntelligenceRepository:
    slate = _slate()
    game_state_repository, pipeline, _ = _setup(tmp_path, slate)
    state = _state(slate)
    raw_link = _raw_link(
        game_state_repository,
        state,
        GameStateRawLinkOutcome.NORMALIZED,
        normalized_checksum=state.checksum,
    )
    game_state_repository.persist_attempt_evidence(
        run_id=RUN_ID,
        phase_attempt=1,
        requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum,
        outcome=GameStateRawLinkOutcome.NORMALIZED,
        normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=raw_link,
    )
    game_state_repository.persist_game_state(
        run_id=RUN_ID,
        phase_attempt=1,
        state=state,
        artifact=write_game_state_artifact(state, game_state_repository.artifact_root),
    )
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.SUCCEEDED,
        transitioned_at=state.observed_at.isoformat(),
    )
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseStatus.RUNNING,
        transitioned_at=state.observed_at.isoformat(),
    )
    return BaseballIntelligenceRepository(
        game_state_repository.database,
        artifact_root=game_state_repository.artifact_root,
        clock=lambda: state.observed_at,
    )


def test_repository_persists_and_reconstructs_zero_game_assembly_offline(
    tmp_path: Path,
) -> None:
    repository = _zero_game_repository(tmp_path)
    result, inventory = repository.assemble_for_run(run_id=RUN_ID)
    persisted = repository.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    reopened = BaseballIntelligenceRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
        clock=lambda: result.assembly.observed_at,
    )
    reconstructed = reopened.get_by_snapshot_id(persisted.snapshot_id)
    assert inventory.candidates == ()
    assert persisted.assembly.canonical_json_bytes() == result.assembly.canonical_json_bytes()
    assert reconstructed.assembly.canonical_json_bytes() == result.assembly.canonical_json_bytes()
    assert reconstructed.artifact == persisted.artifact
    assert reopened.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    ).snapshot_id == persisted.snapshot_id


def test_repository_retains_failed_attempt_without_creating_snapshot(
    tmp_path: Path,
) -> None:
    repository = _zero_game_repository(tmp_path)
    inventory = repository.load_candidate_inventory(run_id=RUN_ID)
    first = repository.persist_failed_attempt(
        run_id=RUN_ID,
        phase_attempt=1,
        outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
        inventory=inventory,
        warnings=({"code": "fixture_selection_failure", "message": "safe"},),
    )
    replay = repository.persist_failed_attempt(
        run_id=RUN_ID,
        phase_attempt=1,
        outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
        inventory=inventory,
        warnings=({"code": "fixture_selection_failure", "message": "safe"},),
    )
    assert first == replay
    assert repository.get_latest_for_run(RUN_ID) is None
    assert first.manifest.relpath.endswith("attempt_0001.json")


def test_repository_persists_six_category_assembly_from_retained_feature_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Exercise the production serializer from the accepted six-category assembly."""
    fixture = _six_category_fixture()
    run_id = "run_20260730_22222222222222222222222222222222"
    monkeypatch.setattr(game_state_repository_tests, "RUN_ID", run_id)
    game_state_repository, pipeline, _ = _setup(tmp_path, fixture.slate)
    _persist_verified_game_state_mappings(game_state_repository, fixture)
    raw_link = _raw_link(
        game_state_repository,
        fixture.state,
        GameStateRawLinkOutcome.NORMALIZED,
        normalized_checksum=fixture.state.checksum,
    )
    game_state_repository.persist_attempt_evidence(
        run_id=run_id,
        phase_attempt=1,
        requested_date=fixture.slate.requested_date,
        upstream_daily_slate_checksum=fixture.slate.checksum,
        outcome=GameStateRawLinkOutcome.NORMALIZED,
        normalized_snapshot_checksum=fixture.state.checksum,
        raw_link_relpath=raw_link,
    )
    game_state_repository.persist_game_state(
        run_id=run_id,
        phase_attempt=1,
        state=fixture.state,
        artifact=write_game_state_artifact(
            fixture.state,
            game_state_repository.artifact_root,
        ),
    )
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.SUCCEEDED,
        transitioned_at=fixture.state.observed_at.isoformat(),
    )
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseStatus.RUNNING,
        transitioned_at=fixture.state.observed_at.isoformat(),
    )
    repository = BaseballIntelligenceRepository(
        game_state_repository.database,
        artifact_root=game_state_repository.artifact_root,
        clock=lambda: fixture.assembly.observed_at,
    )
    result, inventory = repository.assemble_for_run(run_id=run_id)
    persisted = repository.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    reconstructed = repository.get_by_snapshot_id(persisted.snapshot_id)

    assert persisted.assembly.checksum == result.assembly.checksum
    assert reconstructed.assembly.canonical_json_bytes() == result.assembly.canonical_json_bytes()
    assert result.assembly.upstream_daily_slate_checksum == fixture.slate.checksum
    assert result.assembly.upstream_game_state_checksum == fixture.state.checksum
    assert inventory.candidate_feature_snapshot_ids == (
        "feature:blocked",
        "feature:late",
        "feature:complete:a",
        "feature:complete:b",
        "feature:degraded",
    )
    assert persisted.assembly.source_stats_run_ids == (
        "stats-run-complete-a",
        "stats-run-complete-b",
        "stats-run-degraded",
    )
    players = {
        player.source_player_id: player
        for game in reconstructed.assembly.games
        for team in (game.away, game.home)
        for player in team.players
    }
    assert players["1001"].canonical_player_id is None
    assert players["1002"].canonical_player_id == "player:canonical:1002"
    assert players["1003"].availability.value == "unavailable"
    assert players["1004"].availability.value == "unavailable"
    assert players["1005"].equivalent_feature_snapshot_ids == (
        "feature:complete:a",
        "feature:complete:b",
    )
    assert players["1006"].feature is not None
    assert players["1006"].feature.completeness_state == "degraded"
