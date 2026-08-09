from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import app.migrations as migrations
from app.migrations import (
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_FINGERPRINT,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V14_FINGERPRINT,
    MIGRATION_HISTORY,
    MIGRATION_V1_CHECKSUM,
    MIGRATION_V1_NAME,
    MIGRATION_V2_CHECKSUM,
    MIGRATION_V2_NAME,
    MIGRATION_V3_CHECKSUM,
    MIGRATION_V3_NAME,
    MIGRATION_V4_CHECKSUM,
    MIGRATION_V4_NAME,
    ensure_schema,
    schema_fingerprint,
)


TIMESTAMP = "2026-07-15T00:00:00+00:00"
RUN_ID = "run_20260714_11111111111111111111111111111111"


def _create_formal_v4_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, name, checksum in (
            (1, FORMAL_SCHEMA_V1_STATEMENTS, MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM),
            (2, FORMAL_SCHEMA_V2_STATEMENTS, MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM),
            (3, FORMAL_SCHEMA_V3_STATEMENTS, MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM),
            (4, FORMAL_SCHEMA_V4_STATEMENTS, MIGRATION_V4_NAME, MIGRATION_V4_CHECKSUM),
        ):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (version, name, checksum, TIMESTAMP),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V4_FINGERPRINT
    finally:
        connection.close()


def test_fresh_schema_installs_current_with_deterministic_fingerprint(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.db"
    second = tmp_path / "second.db"

    first_result = ensure_schema(first)
    second_result = ensure_schema(second)

    assert first_result.version == second_result.version == 14
    assert first_result.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT
    assert second_result.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT
    connection = sqlite3.connect(first)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 14
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() == list(MIGRATION_HISTORY)
    finally:
        connection.close()


def test_exact_v4_database_upgrades_transactionally_and_preserves_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.db"
    _create_formal_v4_database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO collector_runs(
            run_id,requested_date,status,created_at,queued_at,started_at,
            completed_at,updated_at,failure_stage,error_message,artifact_relpath,
            app_version,schema_version
        ) VALUES (?, '2026-07-14', 'completed', ?, ?, ?, ?, ?, NULL, NULL, NULL,
                  'test', 4)
        """,
        (RUN_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
    )
    connection.commit()
    connection.close()

    result = ensure_schema(path)

    assert result.version == 14
    assert result.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT run_id,schema_version FROM collector_runs").fetchone() == (RUN_ID, 4)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v4_to_v5_failure_rolls_back_schema_and_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback.db"
    _create_formal_v4_database(path)
    original = migrations.FORMAL_SCHEMA_V5_STATEMENTS
    monkeypatch.setattr(
        migrations,
        "FORMAL_SCHEMA_V5_STATEMENTS",
        (*original, "CREATE TABLE broken("),
    )

    with pytest.raises(sqlite3.OperationalError):
        ensure_schema(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V4_FINGERPRINT
        assert connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() == list(MIGRATION_HISTORY[:4])
    finally:
        connection.close()


def test_ensure_schema_is_idempotent_at_current(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    first = ensure_schema(path)
    second = ensure_schema(path)

    assert first.version == second.version == 14
    assert second.migrated is False
    assert second.schema_fingerprint == FORMAL_SCHEMA_V14_FINGERPRINT


def test_v1_through_v4_checksums_remain_pinned() -> None:
    assert MIGRATION_V1_CHECKSUM == ("5ade37c867d9dd887e567f75be2044d18b175f6ecd80527ad26c9df8e0725263")
    assert MIGRATION_V2_CHECKSUM == ("c259e90f1d5e8242cda30a8a3a1510fe03234c0d0ccc01b16ead35103b9543ce")
    assert MIGRATION_V3_CHECKSUM == ("6b5a8f618c744e8492177a8dbcb3bc414c8d6040b87bcb9f36ebae56de95af08")
    assert MIGRATION_V4_CHECKSUM == ("c25d35f00e92c055afd5b3168273ea763064ae91148b6997d1aef38a6975afbd")


def test_v5_snapshot_and_statcast_constraints_encode_persistence_policy(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constraints.db"
    ensure_schema(path)
    connection = sqlite3.connect(path)
    try:
        run_columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(stats_ingestion_runs)")
        }
        assert run_columns["source_version"] == 1
        assert run_columns["adapter_version"] == 1
        for table in ("stats_game_identities", "stats_season_snapshots"):
            create_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert create_sql is not None
            assert "season BETWEEN 1871 AND 9999" in str(create_sql[0])
        for table in (
            "stats_game_status_observations",
            "stats_game_team_snapshots",
            "stats_game_player_snapshots",
        ):
            columns = {
                str(row[1]): int(row[3])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert columns["revision_number"] == 1
            assert columns["revision_kind"] == 1
            assert columns["normalized_checksum"] == 1
            assert columns["stats_run_id"] == 1
            if table == "stats_game_player_snapshots":
                assert columns["source_row_key"] == 1
                assert columns["source_stint_key"] == 1
                assert columns["position_code"] == 0

        def unique_column_sets(table: str) -> set[tuple[str, ...]]:
            result: set[tuple[str, ...]] = set()
            for index in connection.execute(f"PRAGMA index_list({table})"):
                if int(index[2]) != 1:
                    continue
                result.add(
                    tuple(
                        str(column[2])
                        for column in connection.execute(
                            f"PRAGMA index_info({index[1]})"
                        )
                    )
                )
            return result

        assert {
            ("game_identity_id", "revision_number"),
            ("game_identity_id", "source_checksum"),
            ("game_identity_id", "normalized_checksum"),
        }.issubset(unique_column_sets("stats_game_status_observations"))
        assert (
            "game_identity_id",
            "team_identity_id",
            "side",
            "snapshot_kind",
            "normalized_checksum",
        ) in unique_column_sets("stats_game_team_snapshots")
        assert (
            "game_identity_id",
            "team_identity_id",
            "player_identity_id",
            "role",
            "source_row_key",
            "source_checksum",
        ) in unique_column_sets("stats_game_player_snapshots")
        assert (
            "game_identity_id",
            "team_identity_id",
            "player_identity_id",
            "role",
            "source_row_key",
            "normalized_checksum",
        ) in unique_column_sets("stats_game_player_snapshots")
        assert (
            "raw_payload_id",
            "provider",
            "dataset_key",
            "source_row_id",
            "source_row_checksum",
            "classification",
            "reason_code",
        ) in unique_column_sets("stats_excluded_source_rows")
        assert (
            "provider",
            "game_pk",
            "at_bat_number",
            "pitch_number",
        ) in unique_column_sets("statcast_pitch_identities")
        assert (
            "pitch_identity_id",
            "source_checksum",
        ) in unique_column_sets("statcast_pitch_revisions")
        assert (
            "provider",
            "season",
            "split_key",
            "player_identity_id",
            "source_team_id",
            "source_stint_key",
            "source_checksum",
        ) in unique_column_sets("stats_season_snapshots")
        lineup_identity_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_stats_lineup_entry_identity'"
        ).fetchone()
        assert lineup_identity_sql is not None
        normalized_lineup_identity_sql = " ".join(
            str(lineup_identity_sql[0]).split()
        ).lower()
        assert (
            "on stats_lineup_entries( lineup_snapshot_id, player_identity_id, "
            "lineup_role, coalesce(batting_order, 0) )"
            in normalized_lineup_identity_sql
        )
        feature_indexes = {
            str(row[1]): int(row[4])
            for row in connection.execute("PRAGMA index_list(stats_feature_snapshots)")
        }
        assert feature_indexes["uq_stats_feature_game_input"] == 1
        assert feature_indexes["uq_stats_feature_team_input"] == 1
        assert feature_indexes["uq_stats_feature_player_input"] == 1
        feature_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(stats_feature_snapshots)")
        }
        assert "canonical_player_id" in feature_columns
        assert {
            "stats_run_id",
            "feature_version",
            "feature_as_of",
            "input_checksum",
            "feature_checksum",
            "created_at",
        }.issubset(feature_columns)
        raw_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(stats_raw_payload_metadata)"
            )
        }
        assert {
            "stats_run_id",
            "checkpoint_id",
            "provider",
            "endpoint_category",
            "source_capture_id",
            "provider_updated_at",
            "retrieved_at",
            "content_type",
            "checksum_sha256",
            "artifact_relpath",
            "metadata_json",
        }.issubset(raw_columns)
        mapping_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(stats_player_identifier_mappings)"
            )
        }
        assert {
            "stats_run_id",
            "source_version",
            "adapter_version",
            "observed_at",
            "provenance_json",
            "source_checksum",
        }.issubset(mapping_columns)
        feature_foreign_tables = {
            str(row[2])
            for row in connection.execute(
                "PRAGMA foreign_key_list(stats_feature_snapshots)"
            )
        }
        assert "stats_canonical_players" in feature_foreign_tables

        foreign_tables = {
            str(row[2])
            for row in connection.execute(
                "PRAGMA foreign_key_list(stats_game_player_snapshots)"
            )
        }
        assert {
            "stats_ingestion_runs",
            "stats_game_identities",
            "stats_team_identities",
            "stats_player_identities",
            "stats_raw_payload_metadata",
        } == foreign_tables
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {
            "stats_game_identities_validate_teams",
            "stats_game_team_snapshots_validate_side",
            "stats_game_player_snapshots_validate_team",
            "statcast_pitch_identities_validate_game",
            "stats_excluded_source_rows_validate_raw_run",
            "stats_raw_payload_metadata_validate_checkpoint_run",
            "stats_game_status_observations_validate_raw_run",
            "stats_game_team_snapshots_validate_raw_run",
            "stats_game_player_snapshots_validate_raw_run",
            "stats_lineup_snapshots_validate_raw_run",
            "stats_play_revisions_validate_raw_run",
            "statcast_pitch_revisions_validate_raw_run",
            "stats_season_snapshots_validate_raw_run",
            "stats_canonical_players_reject_update",
            "stats_player_identifier_mappings_reject_update",
            "stats_ingestion_runs_preserve_provenance",
            "stats_team_identities_preserve_facts",
            "stats_player_identities_preserve_facts",
            "stats_game_identities_preserve_facts",
            "stats_play_identities_preserve_facts",
            "statcast_pitch_identities_preserve_facts",
        }.issubset(triggers)
        mapping_foreign_tables = {
            str(row[2])
            for row in connection.execute(
                "PRAGMA foreign_key_list(stats_player_identifier_mappings)"
            )
        }
        assert mapping_foreign_tables == {
            "stats_ingestion_runs",
            "stats_canonical_players",
            "stats_player_identities",
        }
        raw_run_tables = (
            "stats_game_status_observations",
            "stats_game_team_snapshots",
            "stats_game_player_snapshots",
            "stats_lineup_snapshots",
            "stats_play_revisions",
            "statcast_pitch_revisions",
            "stats_season_snapshots",
        )
        tables_with_raw_run_columns: set[str] = set()
        for (table_name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ):
            column_names = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if {"stats_run_id", "raw_payload_id"}.issubset(column_names):
                tables_with_raw_run_columns.add(str(table_name))
        assert tables_with_raw_run_columns == {
            "stats_raw_payload_metadata",
            "stats_excluded_source_rows",
            *raw_run_tables,
        }
        for table in raw_run_tables:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (f"{table}_validate_raw_run",),
            ).fetchone()
            assert trigger_sql is not None
            normalized_trigger_sql = " ".join(str(trigger_sql[0]).split()).lower()
            assert "when new.raw_payload_id is not null" in normalized_trigger_sql
            assert "raw.raw_payload_id = new.raw_payload_id" in normalized_trigger_sql
            assert "raw.stats_run_id = new.stats_run_id" in normalized_trigger_sql

        checkpoint_trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='stats_raw_payload_metadata_validate_checkpoint_run'"
        ).fetchone()
        assert checkpoint_trigger_sql is not None
        normalized_checkpoint_sql = " ".join(
            str(checkpoint_trigger_sql[0]).split()
        ).lower()
        assert "when new.checkpoint_id is not null" in normalized_checkpoint_sql
        assert "checkpoint.checkpoint_id = new.checkpoint_id" in normalized_checkpoint_sql
        assert "checkpoint.stats_run_id = new.stats_run_id" in normalized_checkpoint_sql
    finally:
        connection.close()
