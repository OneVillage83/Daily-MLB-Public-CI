from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.baseball_intelligence import (
    BASEBALL_INTELLIGENCE_PHASE_INPUT_CONTRACT,
    BaseballFeatureSnapshotV1,
    BaseballIntelligenceAssemblyError,
    BaseballIntelligenceAttemptOutcome,
    BaseballIntelligenceNotFoundError,
    BaseballIntelligencePersistenceConflict,
    BaseballIntelligencePhaseHandler,
    BaseballIntelligenceRepository,
    BaseballIntelligenceSelectorError,
    baseball_intelligence_phase_input_checksum,
)
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext
from app.stats.features import (
    FEATURE_VERSION_V3,
    BattingAggregateLine,
    build_aggregate_player_feature_payloads_v3,
)
from tests.test_baseball_intelligence_migration_v10_roundtrip import (
    FEATURE_CUTOFF,
    SELECTION_OBSERVED,
)
from tests.test_baseball_intelligence_repository import (
    RUN_ID,
    _six_category_repository,
    _zero_game_repository,
)


def _context(
    *,
    run_id: str,
    requested_date: str,
    as_of_time: datetime,
    phase_key: PipelinePhaseKey = PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
    attempt_number: int = 1,
) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        run_id=run_id,
        requested_date=requested_date,
        as_of_time=as_of_time.isoformat(),
        timezone="America/Los_Angeles",
        phase_key=phase_key,
        attempt_number=attempt_number,
        retry=attempt_number > 1,
        force_refresh=False,
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_fingerprint="f" * 64,
        code_revision="fixture-handler",
    )


def _handler(
    repository: BaseballIntelligenceRepository,
    *,
    clock=lambda: SELECTION_OBSERVED,
) -> BaseballIntelligencePhaseHandler:
    return BaseballIntelligencePhaseHandler(
        repository.database,
        artifact_root=repository.artifact_root,
        repository=repository,
        clock=clock,
    )


def _six_category(
    tmp_path,
    monkeypatch,
):
    repository, _, _, _, fixture, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    return repository, fixture, run_id, _context(
        run_id=run_id,
        requested_date=fixture.slate.requested_date,
        as_of_time=fixture.slate.as_of_time,
    )


@pytest.mark.parametrize(
    ("phase_key", "attempt_number", "message"),
    (
        (PipelinePhaseKey.GAME_STATE, 1, "BASEBALL_INTELLIGENCE_ASSEMBLY"),
        (PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY, 0, "positive"),
    ),
)
def test_handler_rejects_wrong_phase_and_invalid_attempt(
    tmp_path,
    monkeypatch,
    phase_key,
    attempt_number,
    message,
) -> None:
    repository, fixture, run_id, _ = _six_category(tmp_path, monkeypatch)
    context = _context(
        run_id=run_id,
        requested_date=fixture.slate.requested_date,
        as_of_time=fixture.slate.as_of_time,
        phase_key=phase_key,
        attempt_number=attempt_number,
    )
    with pytest.raises(ValueError, match=message):
        _handler(repository)(context)


def test_handler_rejects_naive_or_early_fixed_selection_clock(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, _, context = _six_category(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="timezone-aware"):
        _handler(repository, clock=lambda: datetime(2026, 7, 30, 14, 15))(context)
    with pytest.raises(ValueError, match="cannot precede"):
        _handler(
            repository,
            clock=lambda: datetime(2026, 7, 30, 14, 9, tzinfo=timezone.utc),
        )(context)


def test_six_category_handler_success_verifies_all_persisted_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    repository, fixture, run_id, context = _six_category(tmp_path, monkeypatch)
    calls = 0

    def clock() -> datetime:
        nonlocal calls
        calls += 1
        return SELECTION_OBSERVED

    result = _handler(repository, clock=clock)(context)
    persisted = repository.get_for_run_attempt(run_id, 1)
    reopened = BaseballIntelligenceRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
    )
    reconstructed = reopened.get_for_run_attempt(run_id, 1)
    evidence = reopened.get_attempt_evidence(run_id, 1)
    manifest = reopened.get_attempt_manifest(run_id, 1)
    inventory = reopened.load_candidate_inventory(run_id=run_id)
    players = tuple(
        player
        for game in reconstructed.assembly.games
        for team in (game.away, game.home)
        for player in team.players
    )

    assert calls == 1
    assert result.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert result.output_checksum == persisted.assembly.checksum
    assert result.artifact_relpath == persisted.artifact.relpath
    assert result.warnings == [
        warning.as_dict()
        for warning in repository.assemble_for_run(
            run_id=run_id,
            observed_at=SELECTION_OBSERVED,
        )[0].warnings
    ]
    assert result.input_checksum == baseball_intelligence_phase_input_checksum(
        requested_date=fixture.slate.requested_date,
        as_of_time=fixture.slate.as_of_time,
        upstream_daily_slate_checksum=fixture.slate.checksum,
        upstream_game_state_checksum=fixture.state.checksum,
        candidate_inventory_checksum=inventory.checksum,
        selection_observed_at=SELECTION_OBSERVED,
    )
    assert BASEBALL_INTELLIGENCE_PHASE_INPUT_CONTRACT.startswith("DSE_")
    assert reconstructed.assembly.canonical_json_bytes() == (
        persisted.assembly.canonical_json_bytes()
    )
    assert evidence.outcome is BaseballIntelligenceAttemptOutcome.ASSEMBLED
    assert evidence.assembly_checksum == persisted.assembly.checksum
    assert evidence.selection_observed_at == SELECTION_OBSERVED
    assert manifest.candidate_inventory_checksum == inventory.checksum
    assert manifest.candidate_feature_snapshot_ids == (
        "feature:blocked",
        "feature:late",
        "feature:complete:a",
        "feature:complete:b",
        "feature:degraded",
    )
    assert len(reconstructed.assembly.games) == 1
    assert len(players) == 6
    assert sum(player.feature is not None for player in players) == 2
    assert sum(len(player.equivalent_feature_snapshot_ids) for player in players) == 3


def test_zero_game_handler_succeeds_without_warnings_or_feature_query(
    tmp_path,
    monkeypatch,
) -> None:
    repository = _zero_game_repository(tmp_path)
    slate = repository.daily_slate.get_latest_daily_slate_for_run(RUN_ID)
    assert slate is not None
    queried = False
    original = repository.selector.load_candidates

    def selector(**kwargs):
        nonlocal queried
        if tuple(kwargs["canonical_player_ids"]):
            queried = True
        return original(**kwargs)

    monkeypatch.setattr(repository.selector, "load_candidates", selector)
    context = _context(
        run_id=RUN_ID,
        requested_date=slate.slate.requested_date,
        as_of_time=slate.slate.as_of_time,
    )
    state = repository.game_state.get_latest_game_state_for_run(RUN_ID)
    assert state is not None
    result = _handler(
        repository,
        clock=lambda: state.state.observed_at,
    )(context)

    assert result.status is PipelinePhaseStatus.SUCCEEDED
    assert result.warnings is None
    assert not queried
    persisted = repository.get_for_run_attempt(RUN_ID, 1)
    assert persisted.assembly.games == ()
    assert repository.get_attempt_manifest(RUN_ID, 1).candidate_inventory_checksum


def _insert_conflicting_complete_feature(
    repository: BaseballIntelligenceRepository,
) -> None:
    player_id = "player:canonical:1005"
    feature_date = date(2026, 7, 30)
    payload = build_aggregate_player_feature_payloads_v3(
        feature_as_of=feature_date,
        knowledge_cutoff=FEATURE_CUTOFF,
        batting_aggregates=(
            BattingAggregateLine(
                feature_date - timedelta(days=1),
                player_id,
                "season_to_date",
                pa=20,
                ab=18,
                hits=8,
                home_runs=2,
                walks=2,
                strikeouts=3,
                available_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            ),
        ),
        pitching_aggregates=(),
    )[player_id]
    feature = BaseballFeatureSnapshotV1(
        feature_snapshot_id="feature:complete:conflict",
        stats_run_id="stats-run-complete-conflict",
        feature_version=FEATURE_VERSION_V3,
        entity_kind="player",
        entity_id=player_id,
        feature_as_of=feature_date.isoformat(),
        completeness_state="complete",
        input_checksum=hashlib.sha256(b"conflicting-input").hexdigest(),
        feature_checksum=str(payload["feature_checksum"]),
        features=payload,
        created_at=datetime(2026, 7, 30, 13, 35, tzinfo=timezone.utc),
    )
    with repository.database.connect(write=True) as connection:
        connection.execute(
            """INSERT INTO stats_ingestion_runs(
                   stats_run_id,run_id,provider,source_version,adapter_version,
                   scope_key,requested_through_date,source_observed_at,status,
                   created_at,started_at,completed_at,updated_at,
                   configuration_checksum
               ) VALUES (?,
                   'collector_fixture_v10','fixture','v3','fixture-v10',
                   'regular-season:2026',?,?, 'completed',?,?,?,?,?)""",
            (
                feature.stats_run_id,
                feature.feature_as_of,
                feature.created_at.isoformat(),
                feature.created_at.isoformat(),
                feature.created_at.isoformat(),
                feature.created_at.isoformat(),
                feature.created_at.isoformat(),
                hashlib.sha256(feature.stats_run_id.encode()).hexdigest(),
            ),
        )
        connection.execute(
            """INSERT INTO stats_feature_snapshots(
                   feature_snapshot_id,stats_run_id,feature_version,entity_kind,
                   game_identity_id,team_identity_id,player_identity_id,
                   canonical_player_id,feature_as_of,completeness_state,
                   observed_through,complete_through,input_checksum,
                   feature_checksum,features_json,created_at
               ) VALUES (?, ?, ?, 'player', NULL, NULL, NULL, ?, ?, 'complete',
                   NULL, NULL, ?, ?, ?, ?)""",
            (
                feature.feature_snapshot_id,
                feature.stats_run_id,
                feature.feature_version,
                feature.entity_id,
                feature.feature_as_of,
                feature.input_checksum,
                feature.feature_checksum,
                json.dumps(
                    feature.as_dict()["features"],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                feature.created_at.isoformat(),
            ),
        )


def test_conflicting_retained_features_persist_selection_failed_only(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, run_id, context = _six_category(tmp_path, monkeypatch)
    _insert_conflicting_complete_feature(repository)

    with pytest.raises(BaseballIntelligenceAssemblyError, match="conflicting"):
        _handler(repository)(context)

    evidence = repository.get_attempt_evidence(run_id, 1)
    assert evidence.outcome is BaseballIntelligenceAttemptOutcome.SELECTION_FAILED
    assert evidence.assembly_checksum is None
    assert evidence.selection_observed_at == SELECTION_OBSERVED
    assert repository.get_latest_for_run(run_id) is None
    assert evidence.warnings[0]["code"] == "baseball_intelligence_selection_failed"
    with pytest.raises(BaseballIntelligenceAssemblyError, match="conflicting"):
        _handler(repository)(context)
    assert repository.get_attempt_evidence(run_id, 1) == evidence


def test_persistence_failure_retains_assembly_failed_without_masking_original(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, run_id, context = _six_category(tmp_path, monkeypatch)
    original = RuntimeError("safe persistence sentinel")

    def fail(**_kwargs):
        raise original

    monkeypatch.setattr(repository, "persist_assembly", fail)
    with pytest.raises(RuntimeError, match="safe persistence sentinel") as captured:
        _handler(repository)(context)
    assert captured.value is original
    evidence = repository.get_attempt_evidence(run_id, 1)
    assert evidence.outcome is BaseballIntelligenceAttemptOutcome.ASSEMBLY_FAILED
    assert repository.get_latest_for_run(run_id) is None


def test_failed_evidence_error_does_not_mask_original(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, _, context = _six_category(tmp_path, monkeypatch)
    original = RuntimeError("original persistence failure")

    def fail_assembly(**_kwargs):
        raise original

    def fail_evidence(**_kwargs):
        raise RuntimeError("secondary evidence failure")

    monkeypatch.setattr(repository, "persist_assembly", fail_assembly)
    monkeypatch.setattr(repository, "persist_failed_attempt", fail_evidence)
    with pytest.raises(RuntimeError, match="original persistence failure") as captured:
        _handler(repository)(context)
    assert captured.value is original
    assert captured.value.__notes__ == [
        "failed-attempt evidence could not be retained (RuntimeError)"
    ]


def test_inactive_or_wrong_attempt_creates_no_misleading_failure_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    repository, fixture, run_id, _ = _six_category(tmp_path, monkeypatch)
    context = _context(
        run_id=run_id,
        requested_date=fixture.slate.requested_date,
        as_of_time=fixture.slate.as_of_time,
        attempt_number=2,
    )
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        _handler(repository)(context)
    with pytest.raises(BaseballIntelligenceNotFoundError):
        repository.get_attempt_evidence(run_id, 2)


def test_malformed_selector_failure_creates_no_attempt_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, run_id, context = _six_category(tmp_path, monkeypatch)

    def fail_selector(**_kwargs):
        raise BaseballIntelligenceSelectorError("malformed retained V3 fixture")

    monkeypatch.setattr(repository, "load_candidate_inventory", fail_selector)
    with pytest.raises(
        BaseballIntelligenceSelectorError,
        match="malformed retained V3 fixture",
    ):
        _handler(repository)(context)
    with pytest.raises(BaseballIntelligenceNotFoundError):
        repository.get_attempt_evidence(run_id, 1)


def test_exact_handler_replay_is_idempotent_and_conflicting_boundary_fails(
    tmp_path,
    monkeypatch,
) -> None:
    repository, _, run_id, context = _six_category(tmp_path, monkeypatch)
    first = _handler(repository)(context)
    second = _handler(repository)(context)
    original_attempt = repository.get_attempt_evidence(run_id, 1)
    assert first == second

    later = SELECTION_OBSERVED + timedelta(seconds=1)
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        _handler(repository, clock=lambda: later)(context)
    assert repository.get_attempt_evidence(run_id, 1) == original_attempt
