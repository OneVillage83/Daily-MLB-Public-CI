from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.migrations as migrations
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    FORMAL_SCHEMA_V1_FINGERPRINT,
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_FINGERPRINT,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_FINGERPRINT,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_FINGERPRINT,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_FINGERPRINT,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_FINGERPRINT,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_FINGERPRINT,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_FINGERPRINT,
    FORMAL_SCHEMA_V8_STATEMENTS,
    FORMAL_SCHEMA_V9_FINGERPRINT,
    FORMAL_SCHEMA_V9_STATEMENTS,
    FORMAL_SCHEMA_V10_FINGERPRINT,
    FORMAL_SCHEMA_V10_STATEMENTS,
    FORMAL_SCHEMA_V14_FINGERPRINT,
    MIGRATION_HISTORY,
    MIGRATION_V10_CHECKSUM,
    MIGRATION_V10_NAME,
    ensure_schema,
    schema_fingerprint,
)


def _install_formal_v9(path: Path) -> None:
    statements = (
        FORMAL_SCHEMA_V1_STATEMENTS,
        FORMAL_SCHEMA_V2_STATEMENTS,
        FORMAL_SCHEMA_V3_STATEMENTS,
        FORMAL_SCHEMA_V4_STATEMENTS,
        FORMAL_SCHEMA_V5_STATEMENTS,
        FORMAL_SCHEMA_V6_STATEMENTS,
        FORMAL_SCHEMA_V7_STATEMENTS,
        FORMAL_SCHEMA_V8_STATEMENTS,
        FORMAL_SCHEMA_V9_STATEMENTS,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, migration_statements, history in zip(
            range(1, 10), statements, MIGRATION_HISTORY[:9], strict=True
        ):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration_statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (*history, "2026-07-29T12:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V9_FINGERPRINT
    finally:
        connection.close()


def test_v10_constants_pin_historical_identities() -> None:
    assert CURRENT_SCHEMA_VERSION == 14
    assert MIGRATION_V10_NAME == "baseball_intelligence_assembly_v1_temporal_persistence"
    assert MIGRATION_V10_CHECKSUM == "877bdccccb64814a0844adb57279a87d477c79a0e8659ebc3a3dfc08d3bb071b"
    assert FORMAL_SCHEMA_V10_FINGERPRINT == "13a8ed8e477c23954c94a7b6a697c6dae74efd5d3806f0187b1fa2abeb5933c6"
    assert MIGRATION_HISTORY[9] == (10, MIGRATION_V10_NAME, MIGRATION_V10_CHECKSUM)
    assert (
        FORMAL_SCHEMA_V1_FINGERPRINT,
        FORMAL_SCHEMA_V2_FINGERPRINT,
        FORMAL_SCHEMA_V3_FINGERPRINT,
        FORMAL_SCHEMA_V4_FINGERPRINT,
        FORMAL_SCHEMA_V5_FINGERPRINT,
        FORMAL_SCHEMA_V6_FINGERPRINT,
        FORMAL_SCHEMA_V7_FINGERPRINT,
        FORMAL_SCHEMA_V8_FINGERPRINT,
        FORMAL_SCHEMA_V9_FINGERPRINT,
    ) == (
        "f19a6cf797f6ef532af55ca986512e6ea713068c23a8112af56bb41bc4f28ae2",
        "8c28eb16739fb19b78817a88070835fad84947e06349cf1cd879fbce6bfef91f",
        "699e554d65998c41eb922703b17cd5aa508f9229040130bee8b48fa6a73dd24f",
        "e9080c8543c6c98f29af79ba9e7f837d6a10d9a5beb6f41a6535c3ab6fe9587e",
        "5bd9483d29fdf0fd514b9f39c192fe8fe4095fbb2aa1138da25c252dd0accc87",
        "a777640b19ce8dc1509c7d9b5235330ce9dbac47250ca0d4a018f125afccc116",
        "c568f91e2f69309d59438e5639fc97f1037d6c9a859157fa37a26df11a01a716",
        "0438698c19d3afd6ed2bbfd42393f2bf5e1c3897e7588e754eee94d8c0173fc0",
        "c54cdd8b10591e7247f7c1a4d4f1b2bbe385c1ac7fbcb09be8bccd70228130bd",
    )


def test_v10_contract_guards_pin_identity_availability_and_sealing_rules() -> None:
    statements = "\n".join(FORMAL_SCHEMA_V10_STATEMENTS)

    assert "snapshot_id='bia:' || assembly_checksum" in statements
    assert "CHECK ((player_identity_id IS NULL)=(canonical_player_id IS NULL))" in statements
    assert "availability='unavailable' AND representative_feature_snapshot_id IS NULL" in statements
    assert "json_extract(state.canonical_json,'$.away.source_team_id')" in statements
    assert "json_extract(state.canonical_json,'$.home.source_team_id')" in statements
    assert "NEW.as_of_time=slate.as_of_time AND NEW.as_of_time=state.as_of_time" in statements
    assert "game.player_count!=(SELECT count(*) FROM baseball_intelligence_players" in statements
    assert "game.available_feature_count!=(SELECT count(*) FROM baseball_intelligence_players" in statements
    assert "e.feature_snapshot_id=p.representative_feature_snapshot_id" in statements
    assert "e.stats_run_id=p.representative_stats_run_id" in statements
    assert "e.feature_checksum=p.representative_feature_checksum" in statements


def test_v9_upgrade_creates_verified_v10_backup_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "formal-v9.db"
    _install_formal_v9(path)

    result = ensure_schema(path)

    assert result.version == 14
    assert result.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT
    assert result.backup_path is not None and ".pre-v10-" in result.backup_path.name
    assert result.diagnostic_path is not None and result.diagnostic_path.name.startswith("migration-v10-")
    backup = sqlite3.connect(result.backup_path)
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V9_FINGERPRINT
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        backup.close()
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 14
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {row[0] for row in verification.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "baseball_intelligence_attempt_evidence",
            "baseball_intelligence_snapshots",
            "baseball_intelligence_games",
            "baseball_intelligence_players",
            "baseball_intelligence_feature_equivalents",
        }
    finally:
        verification.close()


def test_v10_statement_failure_rolls_back_to_v9(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback-v9.db"
    _install_formal_v9(path)
    original = migrations._execute_statements

    def fail_v10(connection: sqlite3.Connection, statements: object) -> None:
        if statements is migrations.FORMAL_SCHEMA_V10_STATEMENTS:
            raise sqlite3.OperationalError("injected v10 failure")
        original(connection, statements)  # type: ignore[arg-type]

    monkeypatch.setattr(migrations, "_execute_statements", fail_v10)
    with pytest.raises(sqlite3.OperationalError, match="injected v10"):
        ensure_schema(path)
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 9
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V9_FINGERPRINT
        assert verification.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 9
    finally:
        verification.close()
