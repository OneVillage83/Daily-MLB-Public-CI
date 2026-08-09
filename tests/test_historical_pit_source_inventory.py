from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from app.predictions.historical_pit_sources import (
    HistoricalPITSourceError,
    inventory_historical_pit_source,
    open_historical_source_read_only,
)
from scripts.inspect_historical_pit_source import main


def _fixture_database(tmp_path: Path) -> Path:
    path = tmp_path / "historical.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE stats_game_identities(
            game_identity_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_game_id TEXT NOT NULL,
            event_id TEXT,
            season INTEGER NOT NULL,
            game_type TEXT NOT NULL,
            official_date TEXT NOT NULL,
            scheduled_start TEXT,
            home_team_identity_id TEXT NOT NULL,
            away_team_identity_id TEXT NOT NULL,
            venue_provider_id TEXT,
            venue_name TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            identity_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_game_status_observations(
            status_observation_id INTEGER PRIMARY KEY,
            stats_run_id TEXT,
            game_identity_id TEXT NOT NULL,
            raw_payload_id TEXT,
            abstract_state TEXT,
            detailed_state TEXT,
            status_code TEXT,
            scheduled_start TEXT,
            provider_updated_at TEXT,
            retrieved_at TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            revision_kind TEXT,
            status_json TEXT NOT NULL,
            normalized_checksum TEXT,
            source_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_completeness_watermarks(
            provider TEXT NOT NULL,
            dataset_key TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            requested_through_date TEXT NOT NULL,
            source_observed_at TEXT,
            latest_ingested_completed_game_date TEXT,
            contiguous_regular_season_complete_through_date TEXT,
            partial_date TEXT,
            partial_date_reason TEXT,
            cursor_json TEXT,
            source_stats_run_id TEXT NOT NULL,
            reconciliation_id TEXT,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            source_checksum TEXT NOT NULL
        );
        CREATE TABLE statcast_pitch_identities(
            pitch_identity_id TEXT PRIMARY KEY,
            game_identity_id TEXT NOT NULL
        );
        CREATE TABLE stats_feature_snapshots(
            feature_snapshot_id TEXT PRIMARY KEY,
            stats_run_id TEXT,
            feature_version TEXT NOT NULL,
            entity_kind TEXT NOT NULL,
            game_identity_id TEXT,
            team_identity_id TEXT,
            player_identity_id TEXT,
            canonical_player_id TEXT,
            feature_as_of TEXT NOT NULL,
            completeness_state TEXT,
            observed_through TEXT,
            complete_through TEXT,
            input_checksum TEXT,
            feature_checksum TEXT,
            features_json TEXT,
            created_at TEXT
        );
        """
    )
    connection.executemany(
        """
        INSERT INTO stats_game_identities(
            game_identity_id,provider,provider_game_id,season,game_type,
            official_date,scheduled_start,home_team_identity_id,
            away_team_identity_id,identity_checksum
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (
                "game:retrosheet:AAA202504010",
                "retrosheet",
                "AAA202504010",
                2025,
                "R",
                "2025-04-01",
                None,
                "team:retrosheet:AAA",
                "team:retrosheet:BBB",
                "a" * 64,
            ),
            (
                "game:statcast:123",
                "statcast",
                "123",
                2026,
                "R",
                "2026-04-01",
                "2026-04-01T20:10:00+00:00",
                "team:statcast:AAA",
                "team:statcast:BBB",
                "b" * 64,
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO stats_game_status_observations(
            status_observation_id,game_identity_id,abstract_state,status_code,
            retrieved_at,revision_number,status_json,source_checksum
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        [
            (1, "game:retrosheet:AAA202504010", "Final", "final", "2026-01-01T00:00:00+00:00", 1, "{}", "c" * 64),
            (2, "game:statcast:123", "Final", "final", "2026-04-02T00:00:00+00:00", 1, "{}", "d" * 64),
        ],
    )
    connection.execute(
        """
        INSERT INTO stats_completeness_watermarks(
            provider,dataset_key,scope_key,requested_through_date,
            source_observed_at,latest_ingested_completed_game_date,
            contiguous_regular_season_complete_through_date,partial_date,
            partial_date_reason,source_stats_run_id,revision,updated_at,
            source_checksum
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "retrosheet",
            "regular_season_games",
            "season:2025",
            "2025-12-31",
            "2026-01-01T00:00:00+00:00",
            "2025-09-28",
            "2025-09-28",
            None,
            None,
            "stats_" + "1" * 32,
            1,
            "2026-01-01T00:00:00+00:00",
            "e" * 64,
        ),
    )
    connection.executemany(
        "INSERT INTO statcast_pitch_identities VALUES (?,?)",
        [("pitch:1", "game:statcast:123"), ("pitch:2", "game:statcast:123")],
    )
    connection.execute(
        """
        INSERT INTO stats_feature_snapshots(
            feature_snapshot_id,feature_version,entity_kind,feature_as_of
        ) VALUES (?,?,?,?)
        """,
        ("feature:1", "DSE_MLB_STATS_FEATURES_V1", "player", "2026-07-14T00:00:00+00:00"),
    )
    connection.commit()
    connection.close()
    return path


def test_inventory_profiles_provider_final_pitch_feature_and_watermark_coverage(
    tmp_path: Path,
) -> None:
    path = _fixture_database(tmp_path)

    inventory = inventory_historical_pit_source(
        path,
        start_date="2025-01-01",
        end_date="2026-08-08",
    )

    assert inventory.candidate_game_count == 2
    assert inventory.final_game_count == 2
    assert inventory.statcast_pitch_game_count == 1
    assert inventory.feature_snapshot_count == 1
    assert [item.provider for item in inventory.provider_season_coverage] == [
        "retrosheet",
        "statcast",
    ]
    assert inventory.provider_season_coverage[0].scheduled_start_count == 0
    assert inventory.provider_season_coverage[1].scheduled_start_count == 1
    assert inventory.statcast_pitch_coverage[0].pitch_identity_count == 2
    assert (
        inventory.completeness_watermarks[0]
        .contiguous_regular_season_complete_through_date.isoformat()
        == "2025-09-28"
    )
    assert len(inventory.checksum) == 64


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    path = _fixture_database(tmp_path)

    with open_historical_source_read_only(path) as connection:
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden_write(id INTEGER)")

    with sqlite3.connect(path) as connection:
        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "forbidden_write" not in names
    assert "schema_migrations" not in names


def test_inventory_fails_closed_for_missing_required_schema(tmp_path: Path) -> None:
    path = tmp_path / "invalid.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated(id INTEGER)")

    with pytest.raises(HistoricalPITSourceError, match="missing required table"):
        inventory_historical_pit_source(
            path,
            start_date="2025-01-01",
            end_date="2025-12-31",
        )


def test_cli_emits_compact_inventory_without_modifying_source(tmp_path: Path) -> None:
    path = _fixture_database(tmp_path)
    before = path.stat().st_size
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "--database",
            str(path),
            "--start-date",
            "2025-01-01",
            "--end-date",
            "2026-08-08",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "inventoried"
    assert payload["candidate_game_count"] == 2
    assert payload["final_game_count"] == 2
    assert payload["statcast_pitch_game_count"] == 1
    assert payload["source_database"] == str(path.resolve())
    assert path.stat().st_size == before
