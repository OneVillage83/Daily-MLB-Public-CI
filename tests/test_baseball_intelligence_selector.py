from __future__ import annotations

from pathlib import Path

from app.baseball_intelligence import BaseballIntelligenceFeatureSelector
from app.database import Database
from tests.test_baseball_intelligence_migration_v10_roundtrip import _build_database


def test_selector_loads_all_relevant_retained_player_candidates_in_stable_order(
    tmp_path: Path,
) -> None:
    connection, fixture = _build_database(tmp_path)
    try:
        connection.commit()
    finally:
        connection.close()
    selector = BaseballIntelligenceFeatureSelector(Database(tmp_path / "bia-roundtrip-none.sqlite3"))
    inventory = selector.load_candidates(
        requested_date=fixture.assembly.requested_date,
        canonical_player_ids=(
            "player:canonical:1006",
            "player:canonical:1003",
            "player:canonical:1005",
            "player:canonical:1004",
            "player:canonical:1005",
        ),
    )
    assert inventory.canonical_player_ids == (
        "player:canonical:1003",
        "player:canonical:1004",
        "player:canonical:1005",
        "player:canonical:1006",
    )
    assert inventory.candidate_feature_snapshot_ids == (
        "feature:blocked",
        "feature:late",
        "feature:complete:a",
        "feature:complete:b",
        "feature:degraded",
    )
    assert inventory.candidate_stats_run_ids == (
        "stats-run-blocked",
        "stats-run-complete-a",
        "stats-run-complete-b",
        "stats-run-degraded",
        "stats-run-late",
    )
    assert inventory.checksum == selector.load_candidates(
        requested_date=fixture.assembly.requested_date,
        canonical_player_ids=reversed(inventory.canonical_player_ids),
    ).checksum


def test_selector_empty_player_set_returns_without_feature_rows(tmp_path: Path) -> None:
    database = Database(tmp_path / "selector-empty.sqlite3")
    selector = BaseballIntelligenceFeatureSelector(database)
    inventory = selector.load_candidates(
        requested_date="2026-07-30",
        canonical_player_ids=(),
    )
    assert inventory.candidates == ()
    assert inventory.candidate_feature_snapshot_ids == ()
