from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.database import Database
from app.migrations import ensure_schema
from app.stats.repository import StatsInvariantError, StatsRepository
from tests.stats.test_fielding_grain_v6 import (
    HASH_A,
    STATS_RUN_ID,
    TS,
    _create_v5_database,
)


def test_migrated_v5_fielding_payload_replays_without_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "fielding-v5-replay.db"
    _create_v5_database(database_path)
    ensure_schema(database_path)
    repository = StatsRepository(Database(database_path))

    stats_payload = {
        "provider_player_id": "known001",
        "provider_team_id": "LAN",
        "stats_type": "value",
        "source_row_key": "fielding:8:000001",
        "position_code": "8",
        "source_stint_key": "000001",
        "values": {"d_pos": 8, "f_po": 2},
    }
    replayed = repository.record_game_player_snapshot(
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
            "source_checksum": HASH_A,
            "stats": stats_payload,
        }
    )

    assert replayed["revision_number"] == 1
    assert replayed["revision_kind"] == "initial"
    assert stats_payload["source_row_key"] == "fielding:8:000001"
    assert stats_payload["position_code"] == "8"
    assert stats_payload["source_stint_key"] == "000001"
    with pytest.raises(StatsInvariantError, match="conflicting normalized evidence"):
        repository.record_game_player_snapshot(
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
                "source_checksum": HASH_A,
                "stats": {
                    **stats_payload,
                    "values": {"d_pos": 8, "f_po": 3},
                },
            }
        )
    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT source_row_key,revision_number,revision_kind,stats_json "
            "FROM stats_game_player_snapshots ORDER BY player_snapshot_id"
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["source_row_key"] == "fielding:8:000001"
    assert rows[0]["revision_number"] == 1
    assert rows[0]["revision_kind"] == "initial"
    stored_payload = json.loads(str(rows[0]["stats_json"]))
    assert "source_row_key" not in stored_payload
    assert "position_code" not in stored_payload
    assert "source_stint_key" not in stored_payload
