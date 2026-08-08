from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import Database
from app.final_output_migration import (
    FINAL_OUTPUT_SCHEMA_V14_IMMUTABILITY_TRIGGER_STATEMENTS,
    FINAL_OUTPUT_SCHEMA_V14_INDEX_STATEMENTS,
    FINAL_OUTPUT_SCHEMA_V14_TABLE_STATEMENTS,
    FINAL_OUTPUT_SCHEMA_V14_VALIDATION_TRIGGER_STATEMENTS,
)
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
    FORMAL_SCHEMA_V12_STATEMENTS,
    FORMAL_SCHEMA_V13_FINGERPRINT,
    FORMAL_SCHEMA_V13_STATEMENTS,
    FORMAL_SCHEMA_V14_FINGERPRINT,
    FORMAL_SCHEMA_V14_STATEMENTS,
    MIGRATION_HISTORY,
    MIGRATION_V13_CHECKSUM,
    MIGRATION_V14_CHECKSUM,
    MIGRATION_V14_NAME,
    ensure_schema,
    schema_fingerprint,
)


FROZEN_V13_CHECKSUM = "9606657f9497cd54444ecb35680f05d7003a0db535d2e9a1fcc594c9bbf63089"
FROZEN_V13_FINGERPRINT = "d33d27ba07d21aa35584e0afe3c39334deba30170d761897acb13e9e04a65fee"


def _install_v13(path: Path) -> None:
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
        FORMAL_SCHEMA_V13_STATEMENTS,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, history in zip(range(1, 14), groups, MIGRATION_HISTORY[:13], strict=True):
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (*history, "2026-08-07T00:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FROZEN_V13_FINGERPRINT
    finally:
        connection.close()


def test_v14_identity_and_frozen_v13_identity() -> None:
    assert CURRENT_SCHEMA_VERSION == 14
    assert MIGRATION_V14_NAME == "final_output_pipeline_v1_temporal_persistence"
    assert MIGRATION_V13_CHECKSUM == FROZEN_V13_CHECKSUM
    assert FORMAL_SCHEMA_V13_FINGERPRINT == FROZEN_V13_FINGERPRINT
    assert len(FORMAL_SCHEMA_V13_STATEMENTS) == 155
    assert MIGRATION_HISTORY[13] == (14, MIGRATION_V14_NAME, MIGRATION_V14_CHECKSUM)
    assert len(FORMAL_SCHEMA_V14_STATEMENTS) > 0


def test_fresh_and_v13_upgrade_are_equivalent(tmp_path: Path) -> None:
    fresh = Database(tmp_path / "fresh-v14.db")
    assert fresh.schema_info()["fingerprint"] == FORMAL_SCHEMA_V14_FINGERPRINT
    upgrade_path = tmp_path / "upgrade-v13.db"
    _install_v13(upgrade_path)
    upgraded = ensure_schema(upgrade_path)
    assert upgraded.version == 14
    assert upgraded.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT
    with sqlite3.connect(upgrade_path) as connection:
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V14_FINGERPRINT
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_v14_object_inventory_and_artifact_paths(tmp_path: Path) -> None:
    assert len(FINAL_OUTPUT_SCHEMA_V14_TABLE_STATEMENTS) == 11
    assert len(FINAL_OUTPUT_SCHEMA_V14_INDEX_STATEMENTS) == 9
    assert len(FINAL_OUTPUT_SCHEMA_V14_VALIDATION_TRIGGER_STATEMENTS) == 17
    assert len(FINAL_OUTPUT_SCHEMA_V14_IMMUTABILITY_TRIGGER_STATEMENTS) == 22
    with Database(tmp_path / "objects-v14.db").connect() as connection:
        sql = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND "
                "name IN ('pdf_report_snapshots','infographic_snapshots','final_qc_snapshots')"
            )
        )
    assert "pdf_report/snapshots/" in sql
    assert "infographic/snapshots/" in sql
    assert "final_qc/snapshots/" in sql
