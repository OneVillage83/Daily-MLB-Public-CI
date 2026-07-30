from __future__ import annotations

from pathlib import Path

from app.baseball_intelligence import (
    BaseballIntelligenceAttemptOutcome,
    BaseballIntelligenceRepository,
)
from app.game_state import GameStateRawLinkOutcome, write_game_state_artifact
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from tests.test_game_state_repository import RUN_ID, _raw_link, _setup, _slate, _state


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
