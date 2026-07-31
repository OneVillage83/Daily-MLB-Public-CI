from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from app.baseball_intelligence import (
    BaseballFeatureCandidateInventoryV1,
    BaseballIntelligenceAttemptManifestError,
    BaseballIntelligenceAttemptOutcome,
    BaseballIntelligenceAssemblyResultV1,
    BaseballIntelligenceIntegrityError,
    BaseballIntelligencePersistenceConflict,
    BaseballIntelligenceRole,
    baseball_intelligence_artifact_relpath,
    baseball_intelligence_attempt_manifest_relpath,
    publish_baseball_intelligence_artifact,
)
from app.baseball_intelligence.attempt_manifest import (
    publish_baseball_intelligence_attempt_manifest,
)
from app.daily_slate.contracts import DailySlateGameStatus
from tests.test_baseball_intelligence_repository import _six_category_repository


def _replace_away_player(
    result: BaseballIntelligenceAssemblyResultV1,
    source_player_id: str,
    **changes: object,
) -> BaseballIntelligenceAssemblyResultV1:
    game = result.assembly.games[0]
    players = tuple(
        cast(Any, replace)(player, **changes)
        if player.source_player_id == source_player_id
        else player
        for player in game.away.players
    )
    away = replace(game.away, players=players)
    return replace(
        result,
        assembly=replace(
            result.assembly,
            games=(replace(game, away=away),),
        ),
    )


def _mutated_upstream_result(
    result: BaseballIntelligenceAssemblyResultV1,
    mutation: str,
) -> BaseballIntelligenceAssemblyResultV1:
    game = result.assembly.games[0]
    away = game.away
    if mutation == "missing_player":
        away = replace(
            away,
            players=tuple(
                player
                for player in away.players
                if player.source_player_id != "1002"
            ),
            lineup_source_player_ids=tuple(
                value for value in away.lineup_source_player_ids if value != "1002"
            ),
            batter_source_player_ids=tuple(
                value for value in away.batter_source_player_ids if value != "1002"
            ),
        )
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(replace(game, away=away),),
            ),
        )
    if mutation == "extra_player":
        extra = replace(
            away.players[0],
            source_player_id="1999",
            full_name="Extra Fixture Player",
            roles=(BaseballIntelligenceRole.BENCH,),
        )
        away = replace(away, players=(*away.players, extra))
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(replace(game, away=away),),
            ),
        )
    if mutation == "changed_role":
        return _replace_away_player(
            result,
            "1002",
            roles=(BaseballIntelligenceRole.BENCH,),
        )
    if mutation == "changed_identity":
        return _replace_away_player(
            result,
            "1002",
            player_identity_id="identity:mlb:9999",
            canonical_player_id="player:canonical:9999",
        )
    if mutation == "changed_name":
        return _replace_away_player(
            result,
            "1002",
            full_name="Changed Fixture Name",
        )
    if mutation == "changed_source_team":
        away = replace(away, source_team_id="999")
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(replace(game, away=away),),
            ),
        )
    if mutation == "changed_player_checksum":
        return _replace_away_player(
            result,
            "1002",
            game_state_player_checksum="0" * 64,
        )
    if mutation == "moved_player":
        player = next(
            value for value in away.players if value.source_player_id == "1002"
        )
        away = replace(
            away,
            players=tuple(
                value for value in away.players if value.source_player_id != "1002"
            ),
            lineup_source_player_ids=tuple(
                value for value in away.lineup_source_player_ids if value != "1002"
            ),
            batter_source_player_ids=tuple(
                value for value in away.batter_source_player_ids if value != "1002"
            ),
        )
        home = replace(game.home, players=(*game.home.players, player))
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(replace(game, away=away, home=home),),
            ),
        )
    if mutation == "changed_status":
        replacement_status = next(
            value
            for value in DailySlateGameStatus
            if value is not game.game_status
        )
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(replace(game, game_status=replacement_status),),
            ),
        )
    if mutation == "changed_upstream_checksum":
        return replace(
            result,
            assembly=replace(
                result.assembly,
                games=(
                    replace(game, upstream_game_state_game_checksum="0" * 64),
                ),
            ),
        )
    raise AssertionError(f"unknown mutation {mutation}")


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_player",
        "extra_player",
        "changed_role",
        "changed_identity",
        "changed_name",
        "changed_source_team",
        "changed_player_checksum",
        "moved_player",
        "changed_status",
        "changed_upstream_checksum",
    ),
)
def test_repository_independently_reconstructs_upstream_gamestate_lineage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    mutation: str,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    slate, state = repository._verify_upstream(run_id)
    mutated = _mutated_upstream_result(result, mutation)
    with pytest.raises(BaseballIntelligenceIntegrityError):
        repository._verify_assembly_lineage(
            mutated.assembly,
            slate,
            state,
            inventory,
        )


def _corrupt_equivalent(
    database_path: Path,
    statements: tuple[tuple[str, tuple[object, ...]], ...],
) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "DROP TRIGGER baseball_intelligence_equivalents_reject_update"
        )
        for statement, parameters in statements:
            connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("case", "statements"),
    (
        (
            "wrong_stats_run",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET stats_run_id='stats-run-complete-b'
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    (),
                ),
            ),
        ),
        (
            "wrong_snapshot",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET feature_snapshot_id='feature:blocked'
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    (),
                ),
            ),
        ),
        (
            "wrong_player",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET canonical_player_id='player:canonical:1006'
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    (),
                ),
            ),
        ),
        (
            "wrong_checksum",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET feature_checksum=?
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    ("0" * 64,),
                ),
            ),
        ),
        (
            "blocked_selected",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET completeness_state='blocked'
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    (),
                ),
            ),
        ),
        (
            "wrong_representative",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET is_representative=0
                       WHERE feature_snapshot_id='feature:complete:a'""",
                    (),
                ),
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET is_representative=1
                       WHERE feature_snapshot_id='feature:complete:b'""",
                    (),
                ),
            ),
        ),
        (
            "missing_and_extra_same_count",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET source_player_id='1006'
                       WHERE feature_snapshot_id='feature:complete:b'""",
                    (),
                ),
            ),
        ),
        (
            "ordinal_gap",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET ordinal=3
                       WHERE feature_snapshot_id='feature:complete:b'""",
                    (),
                ),
            ),
        ),
        (
            "late_selected",
            (
                (
                    """UPDATE baseball_intelligence_feature_equivalents
                       SET feature_snapshot_id='feature:late',
                           stats_run_id='stats-run-late',
                           canonical_player_id='player:canonical:1004',
                           feature_checksum=(
                               SELECT feature_checksum
                               FROM stats_feature_snapshots
                               WHERE feature_snapshot_id='feature:late'
                           )
                       WHERE feature_snapshot_id='feature:complete:b'""",
                    (),
                ),
            ),
        ),
    ),
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_retrieval_rejects_same_count_equivalent_row_corruption(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    case: str,
    statements: tuple[tuple[str, tuple[object, ...]], ...],
) -> None:
    repository, _, _, persisted, _, _ = _six_category_repository(
        tmp_path,
        monkeypatch,
    )
    assert persisted is not None
    _corrupt_equivalent(repository.database.path, statements)
    with pytest.raises(BaseballIntelligenceIntegrityError):
        repository.get_by_snapshot_id(persisted.snapshot_id)


@pytest.mark.parametrize(
    "variant",
    (
        "missing_blocked",
        "missing_late",
        "missing_equivalent",
        "unrelated_player",
        "wrong_date",
    ),
)
def test_success_persistence_binds_inventory_to_exact_selector_result(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    variant: str,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    candidates = inventory.candidates
    canonical_ids = inventory.canonical_player_ids
    requested_date = inventory.requested_date
    if variant == "missing_blocked":
        candidates = tuple(
            value
            for value in candidates
            if value.feature_snapshot_id != "feature:blocked"
        )
    elif variant == "missing_late":
        candidates = tuple(
            value
            for value in candidates
            if value.feature_snapshot_id != "feature:late"
        )
    elif variant == "missing_equivalent":
        candidates = tuple(
            value
            for value in candidates
            if value.feature_snapshot_id != "feature:complete:b"
        )
    elif variant == "unrelated_player":
        canonical_ids = (*canonical_ids, "player:canonical:unrelated")
    elif variant == "wrong_date":
        requested_date = "2026-07-28"
        candidates = ()
    supplied = BaseballFeatureCandidateInventoryV1(
        requested_date=requested_date,
        feature_version=inventory.feature_version,
        canonical_player_ids=canonical_ids,
        candidates=candidates,
    )
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=supplied,
        )
    with repository.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM baseball_intelligence_attempt_evidence"
            ).fetchone()[0]
            == 0
        )
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 1)
    ).exists()
    assert not (
        repository.artifact_root
        / baseball_intelligence_artifact_relpath(result.assembly)
    ).exists()


@pytest.mark.parametrize(
    "excluded_feature",
    ("feature:blocked", "feature:late", "feature:complete:b"),
)
def test_failed_attempt_binds_inventory_before_writing_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    excluded_feature: str,
) -> None:
    repository, _, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    supplied = replace(
        inventory,
        candidates=tuple(
            value
            for value in inventory.candidates
            if value.feature_snapshot_id != excluded_feature
        ),
    )
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        repository.persist_failed_attempt(
            run_id=run_id,
            phase_attempt=1,
            outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
            inventory=supplied,
        )
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 1)
    ).exists()


def test_inactive_or_wrong_attempt_writes_no_final_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=2,
            result=result,
            inventory=inventory,
        )
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 2)
    ).exists()
    assert not (
        repository.artifact_root
        / baseball_intelligence_artifact_relpath(result.assembly)
    ).exists()


def test_manifest_atomic_publication_cleans_interrupted_temporary_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, _, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    slate, state = repository._verify_upstream(run_id)
    manifest = repository._manifest(
        run_id=run_id,
        phase_attempt=1,
        slate=slate,
        state=state,
        outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
        assembly_checksum=None,
        inventory=inventory,
        warnings=(),
    )

    def fail_publish(_source: Path, _destination: Path) -> None:
        raise OSError("injected atomic publication interruption")

    monkeypatch.setattr("app.exporter.os.link", fail_publish)
    with pytest.raises(
        BaseballIntelligenceAttemptManifestError,
        match="atomically published",
    ):
        publish_baseball_intelligence_attempt_manifest(
            manifest,
            repository.artifact_root,
        )
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 1)
    ).exists()
    assert not list(repository.artifact_root.rglob(".tmp-*.part"))


def test_transaction_rollback_removes_only_files_created_by_call(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    artifact_path = (
        repository.artifact_root
        / baseball_intelligence_artifact_relpath(result.assembly)
    )

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected relational rollback")

    monkeypatch.setattr(repository, "_insert_snapshot", fail_insert)
    with pytest.raises(RuntimeError, match="relational rollback"):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )
    assert not artifact_path.exists()
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 1)
    ).exists()
    assert not list(repository.artifact_root.rglob(".tmp-*.part"))
    with repository.database.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM baseball_intelligence_attempt_evidence"
            ).fetchone()[0]
            == 0
        )


def test_transaction_rollback_preserves_preexisting_verified_artifact(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    artifact, created = publish_baseball_intelligence_artifact(
        result.assembly,
        repository.artifact_root,
    )
    assert created is True
    artifact_path = repository.artifact_root / artifact.relpath
    original = artifact_path.read_bytes()

    def fail_insert(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected relational rollback")

    monkeypatch.setattr(repository, "_insert_snapshot", fail_insert)
    with pytest.raises(RuntimeError, match="relational rollback"):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )
    assert artifact_path.read_bytes() == original
    assert not (
        repository.artifact_root
        / baseball_intelligence_attempt_manifest_relpath(run_id, 1)
    ).exists()


def test_conflicting_immutable_attempt_keeps_original_manifest(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, result, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    evidence = repository.persist_failed_attempt(
        run_id=run_id,
        phase_attempt=1,
        outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
        inventory=inventory,
        warnings=({"code": "safe-selection-failure"},),
    )
    manifest_path = repository.artifact_root / evidence.manifest.relpath
    original = manifest_path.read_bytes()
    with pytest.raises(BaseballIntelligencePersistenceConflict):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )
    assert manifest_path.read_bytes() == original
    assert not (
        repository.artifact_root
        / baseball_intelligence_artifact_relpath(result.assembly)
    ).exists()


def test_exact_manifest_replay_is_idempotent_and_conflict_preserves_bytes(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    repository, _, inventory, _, _, run_id = _six_category_repository(
        tmp_path,
        monkeypatch,
        persist=False,
    )
    slate, state = repository._verify_upstream(run_id)
    manifest = repository._manifest(
        run_id=run_id,
        phase_attempt=1,
        slate=slate,
        state=state,
        outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
        assembly_checksum=None,
        inventory=inventory,
        warnings=(),
    )
    first, created = publish_baseball_intelligence_attempt_manifest(
        manifest,
        repository.artifact_root,
    )
    original = (repository.artifact_root / first.relpath).read_bytes()
    replay, replay_created = publish_baseball_intelligence_attempt_manifest(
        manifest,
        repository.artifact_root,
    )
    assert created is True
    assert replay_created is False
    assert replay == first
    conflicting = replace(
        manifest,
        warnings=({"code": "different-safe-warning"},),
    )
    with pytest.raises(BaseballIntelligenceAttemptManifestError):
        publish_baseball_intelligence_attempt_manifest(
            conflicting,
            repository.artifact_root,
        )
    assert (repository.artifact_root / first.relpath).read_bytes() == original
