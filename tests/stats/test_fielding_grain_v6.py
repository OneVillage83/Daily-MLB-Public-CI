from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.database import Database
from app.migrations import (
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_FINGERPRINT,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_FINGERPRINT,
    MIGRATION_HISTORY,
    MIGRATION_V1_CHECKSUM,
    MIGRATION_V1_NAME,
    MIGRATION_V2_CHECKSUM,
    MIGRATION_V2_NAME,
    MIGRATION_V3_CHECKSUM,
    MIGRATION_V3_NAME,
    MIGRATION_V4_CHECKSUM,
    MIGRATION_V4_NAME,
    MIGRATION_V5_CHECKSUM,
    MIGRATION_V5_NAME,
    MIGRATION_V6_CHECKSUM,
    MIGRATION_V6_NAME,
    ensure_schema,
    schema_fingerprint,
)
from app.stats.repository import StatsRepository
from app.stats.retrosheet_normalization import (
    RetrosheetNormalizationError,
    normalize_retrosheet_player_game_row,
)


TS = "2026-07-21T12:00:00+00:00"
RUN_ID = "run_20260721_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
STATS_RUN_ID = "stats_v5_fielding_migration"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _create_v5_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, name, checksum in (
            (1, FORMAL_SCHEMA_V1_STATEMENTS, MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM),
            (2, FORMAL_SCHEMA_V2_STATEMENTS, MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM),
            (3, FORMAL_SCHEMA_V3_STATEMENTS, MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM),
            (4, FORMAL_SCHEMA_V4_STATEMENTS, MIGRATION_V4_NAME, MIGRATION_V4_CHECKSUM),
            (5, FORMAL_SCHEMA_V5_STATEMENTS, MIGRATION_V5_NAME, MIGRATION_V5_CHECKSUM),
        ):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,?)",
                (version, name, checksum, TS),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V5_FINGERPRINT

        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO collector_runs(run_id,requested_date,status,created_at,"
            "queued_at,started_at,completed_at,updated_at,app_version,schema_version) "
            "VALUES (?, '2026-07-21', 'completed', ?, ?, ?, ?, ?, 'test', 5)",
            (RUN_ID, TS, TS, TS, TS, TS),
        )
        connection.execute(
            "INSERT INTO stats_ingestion_runs(stats_run_id,run_id,provider,"
            "source_version,adapter_version,scope_key,requested_through_date,"
            "source_observed_at,status,created_at,started_at,completed_at,updated_at,"
            "configuration_checksum) VALUES (?,?,?,?,?,'regular-season:2026',"
            "'2026-07-21',?,'completed',?,?,?,?,?)",
            (
                STATS_RUN_ID,
                RUN_ID,
                "retrosheet",
                "retrosheet:through-2025",
                "DSE_STATS_ACQUISITION_V1",
                TS,
                TS,
                TS,
                TS,
                TS,
                HASH_A,
            ),
        )
        connection.execute(
            "INSERT INTO stats_raw_payload_metadata(raw_payload_id,stats_run_id,"
            "provider,endpoint_category,source_capture_id,retrieved_at,content_type,"
            "checksum_sha256,artifact_relpath,metadata_json) "
            "VALUES ('raw:v5',?,'retrosheet','fielding_member','capture-v5',?,"
            "'text/csv',?,'raw/fielding.bin','{}')",
            (STATS_RUN_ID, TS, HASH_A),
        )
        for team_id in ("LAN", "SFN"):
            connection.execute(
                "INSERT INTO stats_team_identities(team_identity_id,provider,"
                "provider_team_id,current_name,active,first_seen_at,last_seen_at,"
                "identity_checksum) VALUES (?,?,?, ?,1,?,?,?)",
                (
                    f"team:retrosheet:{team_id}",
                    "retrosheet",
                    team_id,
                    team_id,
                    TS,
                    TS,
                    HASH_A,
                ),
            )
        connection.execute(
            "INSERT INTO stats_player_identities(player_identity_id,provider,"
            "provider_player_id,full_name,active,first_seen_at,last_seen_at,"
            "identity_checksum) VALUES ('player:retrosheet:known001','retrosheet',"
            "'known001','Known Player',0,?,?,?)",
            (TS, TS, HASH_A),
        )
        connection.execute(
            "INSERT INTO stats_game_identities(game_identity_id,provider,"
            "provider_game_id,season,game_type,official_date,home_team_identity_id,"
            "away_team_identity_id,first_seen_at,last_seen_at,identity_checksum) "
            "VALUES ('game:retrosheet:LAN202607100','retrosheet','LAN202607100',"
            "2026,'regular','2026-07-10','team:retrosheet:LAN',"
            "'team:retrosheet:SFN',?,?,?)",
            (TS, TS, HASH_A),
        )
        rows = (
            (1, "initial", "8", 2, HASH_A, HASH_A),
            (2, "correction", "9", 1, HASH_B, HASH_B),
            (3, "correction", "8", 1, HASH_C, HASH_C),
        )
        for revision, kind, position, putouts, normalized, source in rows:
            stats = json.dumps(
                {
                    "provider_player_id": "known001",
                    "provider_team_id": "LAN",
                    "stats_type": "value",
                    "values": {"d_pos": int(position), "f_po": putouts},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                "INSERT INTO stats_game_player_snapshots(stats_run_id,"
                "game_identity_id,team_identity_id,player_identity_id,raw_payload_id,"
                "role,retrieved_at,revision_number,revision_kind,stats_json,"
                "normalized_checksum,source_checksum) VALUES (?,"
                "'game:retrosheet:LAN202607100','team:retrosheet:LAN',"
                "'player:retrosheet:known001','raw:v5','fielding',?,?,?,?,?,?)",
                (STATS_RUN_ID, TS, revision, kind, stats, normalized, source),
            )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_v5_fielding_chains_migrate_to_independent_source_rows(tmp_path: Path) -> None:
    path = tmp_path / "fielding-v5.db"
    _create_v5_database(path)

    result = ensure_schema(path)

    assert result.version == 6
    assert result.schema_fingerprint == FORMAL_SCHEMA_V6_FINGERPRINT
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT position_code,source_stint_key,source_row_key,revision_number,"
            "revision_kind,source_checksum FROM stats_game_player_snapshots "
            "ORDER BY player_snapshot_id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("8", "000001", "fielding:8:000001", 1, "initial", HASH_A),
            ("9", "000001", "fielding:9:000001", 1, "initial", HASH_B),
            ("8", "000002", "fielding:8:000002", 1, "initial", HASH_C),
        ]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        ] == list(MIGRATION_HISTORY)
    finally:
        connection.close()


def test_same_source_row_revisions_but_new_stint_is_initial(tmp_path: Path) -> None:
    path = tmp_path / "fielding-revisions.db"
    _create_v5_database(path)
    ensure_schema(path)
    repository = StatsRepository(Database(path))

    corrected = repository.record_game_player_snapshot(
        {
            "stats_run_id": STATS_RUN_ID,
            "game_identity_id": "game:retrosheet:LAN202607100",
            "team_identity_id": "team:retrosheet:LAN",
            "player_identity_id": "player:retrosheet:known001",
            "raw_payload_id": "raw:v5",
            "role": "fielding",
            "source_row_key": "fielding:8:000001",
            "position_code": "8",
            "source_stint_key": "000001",
            "retrieved_at": TS,
            "source_checksum": HASH_D,
            "stats": {"values": {"d_pos": 8, "f_po": 99}},
        }
    )
    new_stint = repository.record_game_player_snapshot(
        {
            "stats_run_id": STATS_RUN_ID,
            "game_identity_id": "game:retrosheet:LAN202607100",
            "team_identity_id": "team:retrosheet:LAN",
            "player_identity_id": "player:retrosheet:known001",
            "raw_payload_id": "raw:v5",
            "role": "fielding",
            "source_row_key": "fielding:8:000003",
            "position_code": "8",
            "source_stint_key": "000003",
            "retrieved_at": TS,
            "source_checksum": "e" * 64,
            "stats": {"values": {"d_pos": 8, "f_po": 0}},
        }
    )

    assert corrected["revision_number"] == 2
    assert corrected["revision_kind"] == "correction"
    assert new_stint["revision_number"] == 1
    assert new_stint["revision_kind"] == "initial"


def test_fielding_normalization_requires_and_exposes_position() -> None:
    normalized = normalize_retrosheet_player_game_row(
        "fielding.csv",
        {
            "gid": "LAN202607100",
            "id": "known001",
            "team": "LAN",
            "stattype": "value",
            "gametype": "regular",
            "d_pos": "8",
        },
        regular_game_ids={"LAN202607100"},
    )
    assert normalized.position_code == "8"  # type: ignore[union-attr]

    with pytest.raises(RetrosheetNormalizationError, match="d_pos"):
        normalize_retrosheet_player_game_row(
            "fielding.csv",
            {
                "gid": "LAN202607100",
                "id": "known001",
                "team": "LAN",
                "stattype": "value",
                "gametype": "regular",
                "d_pos": "",
            },
            regular_game_ids={"LAN202607100"},
        )


def test_v5_checksum_is_still_pinned_and_v6_is_appended() -> None:
    assert MIGRATION_HISTORY[4] == (5, MIGRATION_V5_NAME, MIGRATION_V5_CHECKSUM)
    assert MIGRATION_HISTORY[5] == (6, MIGRATION_V6_NAME, MIGRATION_V6_CHECKSUM)
