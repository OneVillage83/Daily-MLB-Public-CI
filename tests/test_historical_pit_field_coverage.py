from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path

import pytest

from app.predictions.historical_pit_field_coverage import (
    HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION,
    HistoricalPITFieldCoverageError,
    HistoricalPITFieldCoverageInventoryV1,
    HistoricalPITNestedFieldCoverageV1,
    inventory_historical_pit_field_coverage,
)
from scripts.inspect_historical_pit_source import main


CHECKSUM = "a" * 64
FIELD_COVERAGE_FIXTURE_CHECKSUM = (
    "c8501b9b0deadf81c8d4a9d9f306f8c32181ae3c826a09a0b5aba3fc60299f63"
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


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
            identity_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_game_status_observations(
            status_observation_id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            abstract_state TEXT,
            status_code TEXT,
            retrieved_at TEXT NOT NULL,
            revision_number INTEGER NOT NULL,
            status_json TEXT NOT NULL,
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
            source_stats_run_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            source_checksum TEXT NOT NULL
        );
        CREATE TABLE stats_game_team_snapshots(
            team_snapshot_id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            snapshot_kind TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        CREATE TABLE stats_game_player_snapshots(
            player_snapshot_id INTEGER PRIMARY KEY,
            game_identity_id TEXT NOT NULL,
            player_identity_id TEXT NOT NULL,
            role TEXT NOT NULL,
            stats_json TEXT NOT NULL
        );
        """
    )
    games = (
        ("g1", "retrosheet", 2023, "R", "2023-03-30"),
        ("g2", "retrosheet", 2023, "R", "2023-03-31"),
        ("g3", "retrosheet", 2024, "R", "2024-03-28"),
        ("post", "retrosheet", 2023, "P", "2023-10-01"),
        ("statcast", "statcast", 2023, "R", "2023-04-01"),
    )
    for ordinal, (game_id, provider, season, game_type, official_date) in enumerate(
        games, start=1
    ):
        connection.execute(
            """
            INSERT INTO stats_game_identities(
                game_identity_id,provider,provider_game_id,season,game_type,
                official_date,home_team_identity_id,away_team_identity_id,
                identity_checksum
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                game_id,
                provider,
                game_id,
                season,
                game_type,
                official_date,
                f"home:{game_id}",
                f"away:{game_id}",
                chr(96 + ordinal) * 64,
            ),
        )
        connection.execute(
            """
            INSERT INTO stats_game_status_observations(
                status_observation_id,game_identity_id,abstract_state,status_code,
                retrieved_at,revision_number,status_json,source_checksum
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                ordinal,
                game_id,
                "Final",
                "final",
                "2026-01-01T00:00:00+00:00",
                1,
                "{}",
                chr(102 + ordinal) * 64,
            ),
        )

    player_rows = (
        (
            "g1",
            "p1",
            "batting",
            {
                "provider_player_id": "p1",
                "stats_type": "value",
                "values": {
                    "b_ab": 0,
                    "b_h": 2,
                    "label": "starter",
                    "mixed": 1,
                    "nullable": None,
                },
            },
        ),
        (
            "g1",
            "p2",
            "batting",
            {
                "provider_player_id": "p2",
                "stats_type": "value",
                "values": {"b_ab": 4, "b_h": None, "label": "bench", "mixed": "one"},
            },
        ),
        (
            "g2",
            "p1",
            "batting",
            {
                "provider_player_id": "p1",
                "stats_type": "value",
                "values": {"b_ab": 3, "b_h": 1, "label": "starter", "mixed": 2},
            },
        ),
        (
            "g1",
            "p3",
            "pitching",
            {
                "provider_player_id": "p3",
                "stats_type": "value",
                "values": {"flag": True, "p_er": 0, "p_rate": 1.5},
            },
        ),
        (
            "g2",
            "p4",
            "pitching",
            {
                "provider_player_id": "p4",
                "stats_type": "value",
                "values": {"flag": False, "p_er": None, "p_rate": 2.0},
            },
        ),
        (
            "g1",
            "p1",
            "fielding",
            {
                "provider_player_id": "p1",
                "stats_type": "value",
                "values": {"d_e": 0, "d_pos": "8"},
            },
        ),
        (
            "g2",
            "p2",
            "fielding",
            {
                "provider_player_id": "p2",
                "stats_type": "value",
                "values": {"d_e": None, "d_pos": "6"},
            },
        ),
        (
            "g3",
            "p5",
            "batting",
            {
                "provider_player_id": "p5",
                "stats_type": "value",
                "values": {"b_ab": 5, "b_h": 2},
            },
        ),
        (
            "post",
            "p6",
            "batting",
            {"stats_type": "value", "values": {"excluded_postseason": 99}},
        ),
        (
            "statcast",
            "p7",
            "batting",
            {"stats_type": "value", "values": {"excluded_provider": 99}},
        ),
    )
    connection.executemany(
        """
        INSERT INTO stats_game_player_snapshots(
            game_identity_id,player_identity_id,role,stats_json
        ) VALUES(?,?,?,?)
        """,
        [
            (game_id, player_id, role, _canonical_json(payload))
            for game_id, player_id, role, payload in player_rows
        ],
    )
    team_rows = (
        (
            "g1",
            "retrosheet_game_value",
            {
                "stats_type": "value",
                "values": {"b_pa": 38, "note": "complete", "optional": None},
            },
        ),
        (
            "g2",
            "retrosheet_game_value",
            {
                "stats_type": "value",
                "values": {"b_pa": 35, "note": "complete", "optional": "set"},
            },
        ),
        (
            "g1",
            "retrosheet_game_summary",
            {"stats_type": "summary", "values": {"runs": 5}},
        ),
        (
            "g3",
            "retrosheet_game_value",
            {"stats_type": "value", "values": {"b_pa": 36, "note": "complete"}},
        ),
        (
            "post",
            "retrosheet_game_value",
            {"stats_type": "value", "values": {"excluded_postseason": 99}},
        ),
        (
            "statcast",
            "statcast_boxscore",
            {"stats_type": "boxscore", "values": {"excluded_provider": 99}},
        ),
    )
    connection.executemany(
        """
        INSERT INTO stats_game_team_snapshots(game_identity_id,snapshot_kind,stats_json)
        VALUES(?,?,?)
        """,
        [
            (game_id, snapshot_kind, _canonical_json(payload))
            for game_id, snapshot_kind, payload in team_rows
        ],
    )
    connection.commit()
    connection.close()


def _by_identity(
    inventory: HistoricalPITFieldCoverageInventoryV1,
) -> dict[tuple[int, str, str, str], HistoricalPITNestedFieldCoverageV1]:
    return {
        (item.season, item.table_name, item.stats_type, item.field_name): item
        for item in inventory.field_coverage
    }


def test_field_coverage_inventory_counts_nested_player_and_team_fields(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical.db"
    _database(database)

    inventory = inventory_historical_pit_field_coverage(
        database,
        start_date="2023-01-01",
        end_date="2024-12-31",
    )
    coverage = _by_identity(inventory)

    assert inventory.contract_version == HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION
    batting_hits = coverage[(2023, "stats_game_player_snapshots", "batting", "b_h")]
    assert batting_hits.total_applicable_row_count == 3
    assert batting_hits.non_null_row_count == 2
    assert batting_hits.distinct_game_count == 2
    assert batting_hits.distinct_player_count == 1
    assert batting_hits.observed_type == "integer"
    assert batting_hits.observed_types == ("integer", "null")

    mixed = coverage[(2023, "stats_game_player_snapshots", "batting", "mixed")]
    assert mixed.observed_type == "mixed"
    assert mixed.observed_types == ("integer", "string")

    nullable = coverage[
        (2023, "stats_game_player_snapshots", "batting", "nullable")
    ]
    assert nullable.non_null_row_count == 0
    assert nullable.distinct_game_count == 0
    assert nullable.distinct_player_count == 0
    assert nullable.observed_type == "null"

    flag = coverage[(2023, "stats_game_player_snapshots", "pitching", "flag")]
    assert flag.observed_type == "boolean"
    assert flag.observed_types == ("boolean",)

    team_pa = coverage[(2023, "stats_game_team_snapshots", "value", "b_pa")]
    assert team_pa.total_applicable_row_count == 2
    assert team_pa.non_null_row_count == 2
    assert team_pa.distinct_game_count == 2
    assert team_pa.distinct_player_count is None
    assert team_pa.observed_type == "integer"
    team_summary = coverage[
        (2023, "stats_game_team_snapshots", "summary", "runs")
    ]
    assert team_summary.total_applicable_row_count == 1
    assert team_summary.non_null_row_count == 1

    assert (2024, "stats_game_player_snapshots", "batting", "b_ab") in coverage
    assert not any("excluded_" in identity[3] for identity in coverage)


def test_field_coverage_is_deterministic_and_binds_every_coverage_row(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical.db"
    _database(database)

    first = inventory_historical_pit_field_coverage(
        database,
        start_date="2023-01-01",
        end_date="2024-12-31",
    )
    second = inventory_historical_pit_field_coverage(
        database,
        start_date="2023-01-01",
        end_date="2024-12-31",
    )

    assert first == second
    assert first.field_coverage_checksum == second.field_coverage_checksum
    assert first.field_coverage_checksum == FIELD_COVERAGE_FIXTURE_CHECKSUM
    identities = tuple(
        (item.season, item.table_name, item.stats_type, item.field_name)
        for item in first.field_coverage
    )
    assert identities == tuple(sorted(identities))

    with sqlite3.connect(database) as connection:
        payload = _canonical_json(
            {"stats_type": "value", "values": {"b_pa": 35, "new_field": 1}}
        )
        connection.execute(
            "UPDATE stats_game_team_snapshots SET stats_json=? WHERE game_identity_id='g2'",
            (payload,),
        )
    changed = inventory_historical_pit_field_coverage(
        database,
        start_date="2023-01-01",
        end_date="2024-12-31",
    )
    assert changed.field_coverage_checksum != first.field_coverage_checksum


def test_cli_field_coverage_emits_compact_counts_without_values(tmp_path: Path) -> None:
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
            "2024-12-31",
            "--field-coverage",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 0
    assert stderr.getvalue() == ""
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == "inventoried"
    assert payload["mode"] == "field_coverage"
    assert payload["contract_version"] == HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION
    assert len(payload["field_coverage_checksum"]) == 64
    assert payload["provider"] == "retrosheet"
    serialized = stdout.getvalue()
    assert "starter" not in serialized
    assert "bench" not in serialized
    assert "complete" not in serialized
    assert "provider_player_id" not in serialized


def test_field_coverage_rejects_invalid_nested_values_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "historical.db"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE stats_game_player_snapshots SET stats_json='{}' "
            "WHERE player_snapshot_id=1"
        )
        before = connection.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()

    with pytest.raises(HistoricalPITFieldCoverageError, match="nested values object"):
        inventory_historical_pit_field_coverage(
            database,
            start_date="2023-01-01",
            end_date="2024-12-31",
        )

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert after == before
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone() is None


def test_field_coverage_reuses_safe_source_path_validation(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(HistoricalPITFieldCoverageError, match="not found"):
        inventory_historical_pit_field_coverage(
            missing,
            start_date="2023-01-01",
            end_date="2024-12-31",
        )


def test_field_coverage_rejects_symlink_source_consistently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = tmp_path / "historical-link.db"
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == link or original_is_symlink(candidate),
    )

    with pytest.raises(HistoricalPITFieldCoverageError, match="must not be a symlink"):
        inventory_historical_pit_field_coverage(
            link,
            start_date="2023-01-01",
            end_date="2024-12-31",
        )


def test_cli_modes_are_explicitly_bounded_and_mutually_exclusive(tmp_path: Path) -> None:
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
            "2024-12-31",
            "--capabilities",
            "--field-coverage",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert result == 64
    assert stdout.getvalue() == ""
    assert json.loads(stderr.getvalue())["status"] == "usage_error"
