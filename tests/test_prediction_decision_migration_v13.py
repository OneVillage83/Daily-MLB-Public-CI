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
    FORMAL_SCHEMA_V11_STATEMENTS,
    FORMAL_SCHEMA_V12_FINGERPRINT,
    FORMAL_SCHEMA_V12_STATEMENTS,
    FORMAL_SCHEMA_V13_FINGERPRINT,
    FORMAL_SCHEMA_V13_STATEMENTS,
    MIGRATION_HISTORY,
    MIGRATION_V12_CHECKSUM,
    MIGRATION_V13_CHECKSUM,
    MIGRATION_V13_NAME,
    ensure_schema,
    schema_fingerprint,
)
from app.prediction_decision_migration import (
    PREDICTION_DECISION_SCHEMA_V13_IMMUTABILITY_TRIGGER_STATEMENTS,
    PREDICTION_DECISION_SCHEMA_V13_INDEX_STATEMENTS,
    PREDICTION_DECISION_SCHEMA_V13_TABLE_STATEMENTS,
    PREDICTION_DECISION_SCHEMA_V13_VALIDATION_TRIGGER_STATEMENTS,
)


V12_CHECKSUM = "9409445202fc362377f112dc546bacf087820b0f1fb608b08b1a542afa4950d0"
V12_FINGERPRINT = "f597210e59f941e3e0bcdd5583dea597ee9cbbcb598a45fc23abe18144f52a22"


def _install_v12(path: Path) -> None:
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
        FORMAL_SCHEMA_V12_STATEMENTS,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, history in zip(range(1, 13), groups, MIGRATION_HISTORY[:12], strict=True):
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (*history, "2026-08-03T00:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == V12_FINGERPRINT
    finally:
        connection.close()


def test_v13_identity_and_frozen_v12_identity() -> None:
    assert CURRENT_SCHEMA_VERSION == 13
    assert MIGRATION_V13_NAME == "prediction_decision_v1_temporal_persistence"
    assert MIGRATION_V12_CHECKSUM == V12_CHECKSUM
    assert FORMAL_SCHEMA_V12_FINGERPRINT == V12_FINGERPRINT
    assert MIGRATION_HISTORY[11][2] == V12_CHECKSUM
    assert MIGRATION_HISTORY[12] == (13, MIGRATION_V13_NAME, MIGRATION_V13_CHECKSUM)
    assert len(FORMAL_SCHEMA_V13_STATEMENTS) == 155


def test_fresh_and_v12_upgrade_are_exactly_equivalent(tmp_path: Path) -> None:
    fresh = Database(tmp_path / "fresh-v13.db")
    with fresh.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V13_FINGERPRINT
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    upgrade_path = tmp_path / "upgrade-v12.db"
    _install_v12(upgrade_path)
    result = ensure_schema(upgrade_path)
    assert result.version == 13
    assert result.schema_fingerprint == FORMAL_SCHEMA_V13_FINGERPRINT
    with sqlite3.connect(upgrade_path) as connection:
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V13_FINGERPRINT
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v13_object_inventory_and_semantic_artifact_paths(tmp_path: Path) -> None:
    assert len(PREDICTION_DECISION_SCHEMA_V13_TABLE_STATEMENTS) == 17
    assert len(PREDICTION_DECISION_SCHEMA_V13_INDEX_STATEMENTS) == 18
    assert len(PREDICTION_DECISION_SCHEMA_V13_VALIDATION_TRIGGER_STATEMENTS) == 16
    assert len(PREDICTION_DECISION_SCHEMA_V13_IMMUTABILITY_TRIGGER_STATEMENTS) == 34
    with Database(tmp_path / "objects.db").connect() as connection:
        sql = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name IN "
                "('prediction_snapshots','value_engine_snapshots','recommendation_gate_snapshots','ranking_snapshots')"
            )
        )
    for relpath in (
        "predictions/snapshots/",
        "value_engine/snapshots/",
        "recommendation_gate/snapshots/",
        "rankings/snapshots/",
    ):
        assert relpath in sql


def test_first_seal_triggers_bind_every_snapshot_semantic_column(tmp_path: Path) -> None:
    with Database(tmp_path / "seal.db").connect() as connection:
        for table in (
            "prediction_snapshots",
            "value_engine_snapshots",
            "recommendation_gate_snapshots",
            "ranking_snapshots",
        ):
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
                if str(row[1]) != "sealed_at"
            }
            trigger = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (f"{table}_seal_only",),
                ).fetchone()[0]
            )
            for column in columns:
                assert f"NEW.{column}=OLD.{column}" in trigger
        value_seal = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='value_engine_snapshots_validate_seal'"
            ).fetchone()[0]
        )
        gate_seal = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='recommendation_gate_snapshots_validate_seal'"
            ).fetchone()[0]
        )
    assert "Value pair ordinals are not contiguous" in value_seal
    assert "Value eligible bookmaker count mismatch" in value_seal
    assert "Gate result ordinals are not contiguous" in gate_seal


def test_injected_v13_ddl_failure_rolls_back_to_exact_frozen_v12(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollback-v12.db"
    _install_v12(path)
    monkeypatch.setattr(
        migrations,
        "FORMAL_SCHEMA_V13_STATEMENTS",
        (*FORMAL_SCHEMA_V13_STATEMENTS[:8], "CREATE TABL invalid_v13"),
    )

    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 12
        assert schema_fingerprint(connection) == V12_FINGERPRINT
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 12
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
