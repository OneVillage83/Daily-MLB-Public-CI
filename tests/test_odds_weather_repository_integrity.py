from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from app.odds_weather import (
    OddsWeatherAttemptManifestV1,
    OddsWeatherAttemptOutcome,
    OddsWeatherArtifactIntegrityError,
    OddsWeatherIntegrityError,
    OddsWeatherPersistenceConflict,
    OddsWeatherRepository,
    odds_weather_artifact_relpath,
    publish_odds_weather_artifact,
    publish_odds_weather_attempt_manifest,
)
from tests.test_game_state_repository import RUN_ID
from tests.test_odds_weather_repository import (
    _one_game_repository,
    _zero_game_odds_weather_repository,
)


def _persist_one_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    persisted = repository.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    return repository, result, inventory, persisted, run_id


@pytest.mark.parametrize(
    "outcome",
    (
        OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
        OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
        OddsWeatherAttemptOutcome.ASSEMBLY_FAILED,
    ),
)
def test_failed_attempts_are_immutable_idempotent_and_create_no_snapshot(
    tmp_path: Path,
    outcome: OddsWeatherAttemptOutcome,
) -> None:
    repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = repository.baseball_intelligence.get_latest_for_run(RUN_ID)
    assert upstream is not None
    inventory = repository.build_inventory(
        run_id=RUN_ID,
        phase_attempt=1,
        observed_at=upstream.sealed_at,
        phase_input_checksum=hashlib.sha256(outcome.value.encode()).hexdigest(),
        raw_captures=(),
    )

    first = repository.persist_failed_attempt(
        run_id=RUN_ID,
        phase_attempt=1,
        outcome=outcome,
        inventory=inventory,
    )
    replay = repository.persist_failed_attempt(
        run_id=RUN_ID,
        phase_attempt=1,
        outcome=outcome,
        inventory=inventory,
    )

    assert replay == first
    assert first.outcome is outcome
    assert first.snapshot_checksum is None
    assert repository.get_latest_for_run(RUN_ID) is None
    with repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_weather_snapshots WHERE run_id=?",
            (RUN_ID,),
        ).fetchone()[0] == 0


def test_failed_attempt_conflicting_replay_is_rejected(tmp_path: Path) -> None:
    repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = repository.baseball_intelligence.get_latest_for_run(RUN_ID)
    assert upstream is not None
    inventory = repository.build_inventory(
        run_id=RUN_ID,
        phase_attempt=1,
        observed_at=upstream.sealed_at,
        phase_input_checksum="1" * 64,
        raw_captures=(),
    )
    repository.persist_failed_attempt(
        run_id=RUN_ID,
        phase_attempt=1,
        outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
        inventory=inventory,
    )

    with pytest.raises(OddsWeatherPersistenceConflict, match="conflicts"):
        repository.persist_failed_attempt(
            run_id=RUN_ID,
            phase_attempt=1,
            outcome=OddsWeatherAttemptOutcome.NORMALIZATION_FAILED,
            inventory=inventory,
        )
    with pytest.raises(OddsWeatherPersistenceConflict, match="conflicts"):
        repository.persist_failed_attempt(
            run_id=RUN_ID,
            phase_attempt=1,
            outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
            inventory=replace(inventory, phase_input_checksum="2" * 64),
        )


def test_repository_rejects_configured_secret_hidden_as_semantic_bookmaker_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    guarded = OddsWeatherRepository(
        repository.database,
        artifact_root=repository.artifact_root,
        secret_values=("book-a",),
    )

    with pytest.raises(OddsWeatherIntegrityError, match="credential"):
        guarded.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )


@pytest.mark.parametrize(
    "read_method",
    ("attempt_evidence", "attempt_manifest", "snapshot_id", "run_attempt", "latest"),
)
def test_repository_read_paths_reject_configured_secret_in_retained_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_method: str,
) -> None:
    repository, _, _, persisted, run_id = _persist_one_game(tmp_path, monkeypatch)
    guarded = OddsWeatherRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
        secret_values=("book-a",),
    )

    with pytest.raises(OddsWeatherIntegrityError, match="manifest"):
        if read_method == "attempt_evidence":
            guarded.get_attempt_evidence(run_id, 1)
        elif read_method == "attempt_manifest":
            guarded.get_attempt_manifest(run_id, 1)
        elif read_method == "snapshot_id":
            guarded.get_by_snapshot_id(persisted.snapshot_id)
        elif read_method == "run_attempt":
            guarded.get_for_run_attempt(run_id, 1)
        else:
            guarded.get_latest_for_run(run_id)


@pytest.mark.parametrize("target", ("manifest", "snapshot", "raw"))
def test_repository_retrieval_rejects_missing_or_tampered_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    repository, _, inventory, persisted, _ = _persist_one_game(tmp_path, monkeypatch)
    if target == "manifest":
        evidence = repository.get_attempt_evidence(persisted.run_id, 1)
        path = repository.artifact_root / evidence.manifest.relpath
    elif target == "snapshot":
        path = repository.artifact_root / persisted.artifact.relpath
    else:
        path = repository.artifact_root / inventory.raw_captures[0].raw_relpath
    path.write_bytes(b"{}")

    with pytest.raises(OddsWeatherIntegrityError):
        repository.get_by_snapshot_id(persisted.snapshot_id)


@pytest.mark.parametrize(
    ("table", "trigger", "mutation"),
    (
        (
            "odds_weather_games",
            "odds_weather_games_reject_update",
            "UPDATE odds_weather_games SET canonical_json='{}'",
        ),
        (
            "odds_weather_snapshot_raw_captures",
            "odds_weather_snapshot_raw_captures_reject_update",
            "UPDATE odds_weather_snapshot_raw_captures SET raw_capture_checksum=("
            "SELECT raw_capture_checksum FROM odds_weather_raw_captures "
            "WHERE provider='the_odds_api' ORDER BY ordinal DESC LIMIT 1) WHERE ordinal=1",
        ),
        (
            "odds_weather_game_weather_selections",
            "odds_weather_game_weather_selections_reject_update",
            "UPDATE odds_weather_game_weather_selections SET forecast_checksum='"
            + "0" * 64
            + "' WHERE provider='nws'",
        ),
    ),
)
def test_repository_retrieval_rejects_same_shape_relational_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    trigger: str,
    mutation: str,
) -> None:
    del table
    repository, _, _, persisted, _ = _persist_one_game(tmp_path, monkeypatch)
    connection = sqlite3.connect(repository.database.path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OddsWeatherIntegrityError):
        repository.get_by_snapshot_id(persisted.snapshot_id)


def test_transaction_rollback_removes_only_newly_created_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected snapshot insertion failure")

    monkeypatch.setattr(repository, "_insert_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="injected"):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )

    manifest_path = (
        repository.artifact_root
        / "odds_weather"
        / "attempts"
        / run_id
        / "attempt_0001.json"
    )
    snapshot_path = repository.artifact_root / odds_weather_artifact_relpath(
        result.snapshot
    )
    assert not manifest_path.exists()
    assert not snapshot_path.exists()
    with repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_weather_attempt_evidence WHERE run_id=?",
            (run_id,),
        ).fetchone()[0] == 0


def test_transaction_rollback_preserves_preexisting_verified_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    now = repository._now()
    manifest = OddsWeatherAttemptManifestV1.from_inventory(
        inventory=inventory,
        outcome=OddsWeatherAttemptOutcome.ASSEMBLED,
        snapshot_checksum=result.snapshot.checksum,
        created_at=now,
        completed_at=now,
    )
    manifest_artifact, _ = publish_odds_weather_attempt_manifest(
        manifest,
        repository.artifact_root,
    )
    snapshot_artifact, _ = publish_odds_weather_artifact(
        result.snapshot,
        repository.artifact_root,
    )

    def fail_snapshot(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected snapshot insertion failure")

    monkeypatch.setattr(repository, "_insert_snapshot", fail_snapshot)
    with pytest.raises(RuntimeError, match="injected"):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )

    assert (repository.artifact_root / manifest_artifact.relpath).exists()
    assert (repository.artifact_root / snapshot_artifact.relpath).exists()


def test_conflicting_snapshot_artifact_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    path = repository.artifact_root / odds_weather_artifact_relpath(
        result.snapshot
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"conflicting immutable bytes")
    original = path.read_bytes()

    with pytest.raises(OddsWeatherArtifactIntegrityError):
        repository.persist_assembly(
            run_id=run_id,
            phase_attempt=1,
            result=result,
            inventory=inventory,
        )

    assert path.read_bytes() == original


def test_repository_rejects_hard_linked_snapshot_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, _, persisted, _ = _persist_one_game(tmp_path, monkeypatch)
    path = repository.artifact_root / persisted.artifact.relpath
    retained = path.with_name("retained-snapshot.json")
    path.replace(retained)
    try:
        os.link(retained, path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(OddsWeatherIntegrityError, match="artifact"):
        repository.get_by_snapshot_id(persisted.snapshot_id)
