from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.migrations as migrations

from app.database import Database
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_STATEMENTS,
    FORMAL_SCHEMA_V9_STATEMENTS,
    FORMAL_SCHEMA_V10_STATEMENTS,
    FORMAL_SCHEMA_V11_FINGERPRINT,
    FORMAL_SCHEMA_V11_STATEMENTS,
    FORMAL_SCHEMA_V12_FINGERPRINT,
    FORMAL_SCHEMA_V12_STATEMENTS,
    MIGRATION_HISTORY,
    MIGRATION_V11_CHECKSUM,
    MIGRATION_V12_CHECKSUM,
    MIGRATION_V12_NAME,
    ensure_schema,
    schema_fingerprint,
)
from app.pre_model_migration import (
    PRE_MODEL_SCHEMA_V12_IMMUTABILITY_TRIGGER_STATEMENTS,
    PRE_MODEL_SCHEMA_V12_INDEX_STATEMENTS,
    PRE_MODEL_SCHEMA_V12_TABLE_STATEMENTS,
    PRE_MODEL_SCHEMA_V12_VALIDATION_TRIGGER_STATEMENTS,
)


V11_CHECKSUM = "a54865d8b5623e96c4f571d6c9d7f899e9ced0d1911128b5a874b41e29df75dd"
V11_FINGERPRINT = "5b9635e1aac05d98fd61dadaf2ac5d435e4aaae9214c79501b5dd642673c75b8"
V12_CHECKSUM = "1408c940cea49c84c68c87a9d350a852d038671e9867acec534440435d60d5ce"
V12_FINGERPRINT = "15e30a24bb578ce691ef5b9063ce61fe8d3e2a09708236de55390ccef9ed8581"

TABLES = {
    "data_quality_attempt_evidence",
    "data_quality_snapshots",
    "data_quality_games",
    "data_quality_issues",
    "matchup_packet_attempt_evidence",
    "matchup_packet_snapshots",
    "matchup_packet_games",
    "model_feature_set_attempt_evidence",
    "model_feature_set_snapshots",
    "model_feature_set_games",
    "model_feature_set_market_contexts",
    "model_feature_set_source_features",
}


def _install_formal_v11(path: Path) -> None:
    groups = (
        FORMAL_SCHEMA_V1_STATEMENTS,
        FORMAL_SCHEMA_V2_STATEMENTS,
        FORMAL_SCHEMA_V3_STATEMENTS,
        FORMAL_SCHEMA_V4_STATEMENTS,
        FORMAL_SCHEMA_V5_STATEMENTS,
        FORMAL_SCHEMA_V6_STATEMENTS,
        FORMAL_SCHEMA_V7_STATEMENTS,
        FORMAL_SCHEMA_V8_STATEMENTS,
        FORMAL_SCHEMA_V9_STATEMENTS,
        FORMAL_SCHEMA_V10_STATEMENTS,
        FORMAL_SCHEMA_V11_STATEMENTS,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, history in zip(
            range(1, 12), groups, MIGRATION_HISTORY[:11], strict=True
        ):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,?)",
                (*history, "2026-08-01T00:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V11_FINGERPRINT
    finally:
        connection.close()


def test_v12_identity_and_prior_v11_identity_are_exact() -> None:
    assert CURRENT_SCHEMA_VERSION == 12
    assert MIGRATION_V12_NAME == "pre_model_pipeline_v1_temporal_persistence"
    assert MIGRATION_V12_CHECKSUM == V12_CHECKSUM
    assert FORMAL_SCHEMA_V12_FINGERPRINT == V12_FINGERPRINT
    assert MIGRATION_V11_CHECKSUM == V11_CHECKSUM
    assert FORMAL_SCHEMA_V11_FINGERPRINT == V11_FINGERPRINT
    assert MIGRATION_HISTORY[10][2] == V11_CHECKSUM
    assert MIGRATION_HISTORY[11] == (12, MIGRATION_V12_NAME, V12_CHECKSUM)
    assert len(FORMAL_SCHEMA_V12_STATEMENTS) == 148


def test_fresh_v12_install_has_exact_objects_and_integrity(tmp_path: Path) -> None:
    database = Database(tmp_path / "fresh-v12.db")
    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert schema_fingerprint(connection) == V12_FINGERPRINT
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert TABLES <= names
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_frozen_v11_upgrade_matches_fresh_v12(tmp_path: Path) -> None:
    upgraded_path = tmp_path / "upgrade-v11.db"
    _install_formal_v11(upgraded_path)
    result = ensure_schema(upgraded_path)
    assert result.version == 12
    assert result.schema_fingerprint == V12_FINGERPRINT
    with sqlite3.connect(upgraded_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert schema_fingerprint(connection) == V12_FINGERPRINT
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v12_object_statement_inventory_is_explicit() -> None:
    assert len(PRE_MODEL_SCHEMA_V12_TABLE_STATEMENTS) == 12
    assert len(PRE_MODEL_SCHEMA_V12_INDEX_STATEMENTS) == 11
    assert len(PRE_MODEL_SCHEMA_V12_VALIDATION_TRIGGER_STATEMENTS) == 15
    assert len(PRE_MODEL_SCHEMA_V12_IMMUTABILITY_TRIGGER_STATEMENTS) == 24


def test_injected_v12_ddl_failure_rolls_back_to_exact_v11(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback-v11.db"
    _install_formal_v11(path)
    monkeypatch.setattr(
        migrations,
        "FORMAL_SCHEMA_V12_STATEMENTS",
        (*FORMAL_SCHEMA_V12_STATEMENTS[:4], "CREATE TABL invalid_v12"),
    )
    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert schema_fingerprint(connection) == V11_FINGERPRINT
        assert connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() == list(MIGRATION_HISTORY[:11])
        assert not (
            TABLES
            & {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        )
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
