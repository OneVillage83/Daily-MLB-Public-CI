from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

from app.predictions.historical_pit_capabilities import (
    HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION,
    inventory_historical_pit_reconstruction_capabilities,
)
from scripts.inspect_historical_pit_source import main


CHECKSUM = "a" * 64


def _database(path: Path) -> None:
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
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            identity_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_game_status_observations(
            status_observation_id INTEGER PRIMARY KEY,
            stats_run_id TEXT NOT NULL,
            game_identity_id TEXT NOT NULL,
            raw_payload_id TEXT,
            abstract_state TEXT,
            detailed_state TEXT,
            status_code TEXT,
            scheduled_start TEXT,
            provider_updated_at TEXT,
            retrieved_at TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            revision_kind TEXT NOT NULL,
            status_json TEXT NOT NULL,
            normalized_checksum TEXT NOT NULL,
            source_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_completeness_watermarks(
            watermark_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            dataset_key TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            requested_through_date TEXT NOT NULL,
            source_observed_at TEXT,
            latest_ingested_completed_game_date TEXT,
            contiguous_regular_season_complete_through_date TEXT,
            partial_date TEXT,
            partial_date_reason TEXT,
            source_stats_run_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            source_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_game_team_snapshots(
            id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            snapshot_kind TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        CREATE TABLE stats_game_player_snapshots(
            id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            role TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        CREATE TABLE stats_lineup_snapshots(
            lineup_snapshot_id TEXT PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            lineup_state TEXT NOT NULL
        );
        CREATE TABLE stats_lineup_entries(
            id INTEGER PRIMARY KEY,
            lineup_snapshot_id TEXT NOT NULL,
            lineup_role TEXT NOT NULL
        );
        CREATE TABLE stats_play_identities(
            id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL
        );
        """
    )
    games = (
        ("g1", "2023-03-30", "A", "B"),
        ("g2", "2023-03-31", "B", "A"),
    )
    for game_id, official_date, home, away in games:
        connection.execute(
            "INSERT INTO stats_game_identities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                game_id,
                "retrosheet",
                game_id,
                None,
                2023,
                "R",
                official_date,
                None,
                home,
                away,
                None,
                None,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                CHECKSUM,
            ),
        )
        connection.execute(
            """
            INSERT INTO stats_game_status_observations(
                stats_run_id,game_identity_id,abstract_state,status_code,
                retrieved_at,revision_number,revision_kind,status_json,
                normalized_checksum,source_checksum
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "stats_source",
                game_id,
                "Final",
                "final",
                "2026-01-01T00:00:00+00:00",
                1,
                "initial",
                "{}",
                CHECKSUM,
                ("b" if game_id == "g1" else "c") * 64,
            ),
        )
        for snapshot_kind in ("boxscore", "linescore"):
            connection.execute(
                "INSERT INTO stats_game_team_snapshots(game_identity_id,snapshot_kind,stats_json) VALUES(?,?,?)",
                (game_id, snapshot_kind, '{"runs":5,"hits":8}'),
            )
        for role in ("batting", "pitching", "fielding"):
            connection.execute(
                "INSERT INTO stats_game_player_snapshots(game_identity_id,role,stats_json) VALUES(?,?,?)",
                (game_id, role, '{"hits":2,"player_id":"p1"}'),
            )
        for side in ("home", "away"):
            lineup_id = f"{game_id}-{side}"
            connection.execute(
                "INSERT INTO stats_lineup_snapshots VALUES(?,?,?)",
                (lineup_id, game_id, "official"),
            )
            for _ in range(9):
                connection.execute(
                    "INSERT INTO stats_lineup_entries(lineup_snapshot_id,lineup_role) VALUES(?,?)",
                    (lineup_id, "starter"),
                )
        for _ in range(3):
            connection.execute(
                "INSERT INTO stats_play_identities(game_identity_id) VALUES(?)",
                (game_id,),
            )
    connection.execute(
        """
        INSERT INTO stats_completeness_watermarks VALUES(
            'wm1','retrosheet','regular_season_games','season:2023',
            '2023-12-31','2026-01-01T00:00:00+00:00',
            '2023-03-31','2023-03-31',NULL,NULL,'stats_source',1,
            '2026-01-01T00:00:00+00:00',?
        )
        """,
        (CHECKSUM,),
    )
    connection.commit()
    connection.close()


def test_capability_inventory_reports_game_scoped_reconstruction_evidence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical.db"
    _database(database)

    inventory = inventory_historical_pit_reconstruction_capabilities(
        database,
        start_date="2023-01-01",
        end_date="2023-12-31",
    )

    assert (
        inventory.contract_version
        == HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION
    )
    assert inventory.tables_present == (
        "stats_game_player_snapshots",
        "stats_game_team_snapshots",
        "stats_lineup_entries",
        "stats_lineup_snapshots",
        "stats_play_identities",
    )
    by_table = {item.table_name: item for item in inventory.capabilities}
    assert by_table["stats_game_team_snapshots"].game_count == 2
    assert by_table["stats_game_team_snapshots"].row_count == 4
    assert by_table["stats_game_team_snapshots"].detail_values == (
        "boxscore",
        "linescore",
    )
    assert by_table["stats_game_team_snapshots"].sample_json_keys == (
        "hits",
        "runs",
    )
    assert by_table["stats_game_player_snapshots"].game_count == 2
    assert by_table["stats_game_player_snapshots"].detail_values == (
        "batting",
        "fielding",
        "pitching",
    )
    assert by_table["stats_lineup_snapshots"].game_count == 2
    assert by_table["stats_lineup_entries"].row_count == 36
    assert by_table["stats_play_identities"].row_count == 6
    assert len(inventory.source_inventory_checksum) == 64
    assert len(inventory.checksum) == 64


def test_cli_capability_mode_emits_compact_json(tmp_path: Path) -> None:
    database = tmp_path / "historical.db"
    _database(database)
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = main(
        [
            "--database",
            str(database),
            "--start-date",
            "2023-01-01",
            "--end-date",
            "2023-12-31",
            "--capabilities",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "inventoried"
    assert payload["mode"] == "reconstruction_capabilities"
    assert payload["contract_version"] == (
        HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION
    )
    assert {item["table_name"] for item in payload["capabilities"]} == {
        "stats_game_player_snapshots",
        "stats_game_team_snapshots",
        "stats_lineup_entries",
        "stats_lineup_snapshots",
        "stats_play_identities",
    }


def test_capability_inventory_does_not_mutate_source_schema(tmp_path: Path) -> None:
    database = tmp_path / "historical.db"
    _database(database)
    before = sqlite3.connect(database).execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    inventory_historical_pit_reconstruction_capabilities(
        database,
        start_date="2023-01-01",
        end_date="2023-12-31",
    )

    connection = sqlite3.connect(database)
    after = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    assert after == before
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
    ).fetchone() is None
    connection.close()
