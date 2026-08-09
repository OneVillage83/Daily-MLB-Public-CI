from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Iterator, cast

import pytest

from app.database import Database
from app.model_feature_set.repository import ModelFeatureSetRepository
from app.predictions.historical_sources import (
    HistoricalModelFeatureSetInventoryV1,
    HistoricalTrainingSourceError,
    load_final_game_score_inventory,
    load_historical_model_feature_sets,
)
from tests.test_scoring_training_data_materialization import _feature_set


class _FixtureDatabase:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE model_feature_set_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                requested_date TEXT NOT NULL,
                as_of_time TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                sealed_at TEXT,
                phase_attempt INTEGER NOT NULL
            );
            CREATE TABLE daily_slate_snapshots(
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                requested_date TEXT NOT NULL,
                phase_attempt INTEGER NOT NULL,
                sealed_at TEXT
            );
            CREATE TABLE daily_slate_games(
                snapshot_id TEXT NOT NULL,
                source_game_id TEXT NOT NULL,
                official_date TEXT NOT NULL,
                scheduled_start_time TEXT,
                away_team_id TEXT NOT NULL,
                home_team_id TEXT NOT NULL,
                game_number INTEGER,
                row_checksum TEXT NOT NULL
            );
            CREATE TABLE stats_team_identities(
                team_identity_id TEXT PRIMARY KEY,
                canonical_team_key TEXT
            );
            CREATE TABLE stats_game_identities(
                game_identity_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_game_id TEXT NOT NULL,
                official_date TEXT NOT NULL,
                home_team_identity_id TEXT NOT NULL,
                away_team_identity_id TEXT NOT NULL
            );
            CREATE TABLE stats_raw_payload_metadata(
                raw_payload_id TEXT PRIMARY KEY,
                checksum_sha256 TEXT NOT NULL
            );
            CREATE TABLE stats_game_status_observations(
                status_observation_id INTEGER PRIMARY KEY,
                game_identity_id TEXT NOT NULL,
                raw_payload_id TEXT,
                revision_number INTEGER NOT NULL,
                abstract_state TEXT,
                status_code TEXT,
                retrieved_at TEXT NOT NULL,
                status_json TEXT NOT NULL,
                source_checksum TEXT NOT NULL,
                normalized_checksum TEXT NOT NULL
            );
            """
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        yield self.connection


class _FeatureRepository:
    def __init__(self, snapshots: dict[str, object]) -> None:
        self.snapshots = snapshots

    def get_by_snapshot_id(self, snapshot_id: str) -> object:
        return self.snapshots[snapshot_id]


def _snapshot(
    snapshot_id: str,
    feature_set: object,
    *,
    run_id: str = "run_20240601_fixture",
    phase_attempt: int = 1,
    sealed_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_id=snapshot_id,
        run_id=run_id,
        phase_attempt=phase_attempt,
        feature_set=feature_set,
        sealed_at=sealed_at or datetime(2024, 6, 1, 18, 2, tzinfo=timezone.utc),
    )


def _insert_feature_snapshot(db: _FixtureDatabase, snapshot: SimpleNamespace) -> None:
    feature_set = snapshot.feature_set
    db.connection.execute(
        "INSERT INTO model_feature_set_snapshots VALUES (?,?,?,?,?,?)",
        (
            snapshot.snapshot_id,
            feature_set.requested_date,
            feature_set.as_of_time.isoformat(),
            feature_set.observed_at.isoformat(),
            snapshot.sealed_at.isoformat(),
            snapshot.phase_attempt,
        ),
    )


def _insert_daily_slate(
    db: _FixtureDatabase,
    snapshot: SimpleNamespace,
    *,
    scheduled_start: datetime | None = None,
) -> None:
    game = snapshot.feature_set.games[0]
    start = scheduled_start or datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc)
    db.connection.execute(
        "INSERT INTO daily_slate_snapshots VALUES (?,?,?,?,?)",
        (
            f"slate-{snapshot.snapshot_id}",
            snapshot.run_id,
            snapshot.feature_set.requested_date,
            snapshot.phase_attempt,
            snapshot.sealed_at.isoformat(),
        ),
    )
    db.connection.execute(
        "INSERT INTO daily_slate_games VALUES (?,?,?,?,?,?,?,?)",
        (
            f"slate-{snapshot.snapshot_id}",
            game.source_game_id,
            snapshot.feature_set.requested_date,
            None if scheduled_start is None and start is None else start.isoformat(),
            game.away_team_id,
            game.home_team_id,
            1,
            "d" * 64,
        ),
    )


def _insert_statcast_final(
    db: _FixtureDatabase,
    *,
    source_game_id: str = "123456",
    home_runs: int = 5,
    away_runs: int = 3,
    revision_number: int = 1,
    observation_id: int = 1,
    retrieved_at: datetime | None = None,
    completion_contract: str = "DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1",
    evidence_home_runs: int | None = None,
) -> None:
    db.connection.execute(
        "INSERT OR IGNORE INTO stats_team_identities VALUES (?,?)",
        ("team-statcast-home", "LAD"),
    )
    db.connection.execute(
        "INSERT OR IGNORE INTO stats_team_identities VALUES (?,?)",
        ("team-statcast-away", "SF"),
    )
    db.connection.execute(
        "INSERT OR IGNORE INTO stats_game_identities VALUES (?,?,?,?,?,?)",
        (
            f"game:statcast:{source_game_id}",
            "statcast",
            source_game_id,
            "2024-06-01",
            "team-statcast-home",
            "team-statcast-away",
        ),
    )
    raw_id = f"raw-{observation_id}"
    db.connection.execute(
        "INSERT INTO stats_raw_payload_metadata VALUES (?,?)",
        (raw_id, "a" * 64),
    )
    status_payload = {
        "game_pk": int(source_game_id),
        "completion_contract": completion_contract,
        "home_score": home_runs,
        "away_score": away_runs,
        "completion_source_evidence": {
            "home_score": home_runs if evidence_home_runs is None else evidence_home_runs,
            "away_score": away_runs,
        },
    }
    db.connection.execute(
        "INSERT INTO stats_game_status_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            observation_id,
            f"game:statcast:{source_game_id}",
            raw_id,
            revision_number,
            "final",
            "final",
            (
                retrieved_at
                or datetime(2024, 6, 2, 2, 25, tzinfo=timezone.utc)
            ).isoformat(),
            json.dumps(status_payload, sort_keys=True),
            chr(ord("b") + observation_id - 1) * 64,
            chr(ord("e") + observation_id - 1) * 64,
        ),
    )


def _inventory(snapshot: SimpleNamespace) -> HistoricalModelFeatureSetInventoryV1:
    return HistoricalModelFeatureSetInventoryV1(
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 1),
        snapshots=(snapshot,),
    )


def test_historical_feature_inventory_falls_back_to_latest_strictly_pregame_snapshot() -> None:
    db = _FixtureDatabase()
    early_features = _feature_set()
    late_features = replace(
        early_features,
        as_of_time=datetime(2024, 6, 1, 23, 11, tzinfo=timezone.utc),
        observed_at=datetime(2024, 6, 1, 23, 12, tzinfo=timezone.utc),
    )
    early = _snapshot("mfs-early", early_features, phase_attempt=1)
    late = _snapshot(
        "mfs-late",
        late_features,
        phase_attempt=2,
        sealed_at=datetime(2024, 6, 1, 23, 13, tzinfo=timezone.utc),
    )
    for snapshot in (early, late):
        _insert_feature_snapshot(db, snapshot)
        _insert_daily_slate(
            db,
            snapshot,
            scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
        )
    repository = _FeatureRepository({"mfs-early": early, "mfs-late": late})

    result = load_historical_model_feature_sets(
        cast(Database, db),
        cast(ModelFeatureSetRepository, repository),
        start_date="2024-06-01",
        end_date="2024-06-01",
    )

    assert tuple(item.snapshot_id for item in result.snapshots) == ("mfs-early",)
    assert result.exclusions == ()
    assert len(result.checksum) == 64


def test_historical_feature_inventory_explicitly_excludes_date_without_pregame_snapshot() -> None:
    db = _FixtureDatabase()
    features = replace(
        _feature_set(),
        as_of_time=datetime(2024, 6, 1, 23, 11, tzinfo=timezone.utc),
        observed_at=datetime(2024, 6, 1, 23, 12, tzinfo=timezone.utc),
    )
    snapshot = _snapshot("mfs-late", features)
    _insert_feature_snapshot(db, snapshot)
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )

    result = load_historical_model_feature_sets(
        cast(Database, db),
        cast(ModelFeatureSetRepository, _FeatureRepository({"mfs-late": snapshot})),
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 1),
    )

    assert result.snapshots == ()
    assert len(result.exclusions) == 1
    assert result.exclusions[0].reason == "no_fully_pregame_verified_snapshot"
    assert result.exclusions[0].candidate_count == 1


def test_final_score_inventory_uses_exact_statcast_game_and_retains_lineage() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )
    _insert_statcast_final(db)

    result = load_final_game_score_inventory(cast(Database, db), _inventory(snapshot))

    assert result.exclusions == ()
    assert len(result.scores) == 1
    score = result.scores[0]
    assert score.source_game_id == "123456"
    assert score.home_runs == 5
    assert score.away_runs == 3
    assert score.source_provider == "statcast"
    assert score.source_payload_checksum == "a" * 64
    lineage = result.lineages[0]
    assert lineage.result_game_identity_id == "game:statcast:123456"
    assert lineage.result_revision_number == 1
    assert lineage.daily_slate_game_checksum == "d" * 64
    assert lineage.completion_contract == "DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1"


def test_final_score_inventory_uses_latest_valid_correction_revision() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )
    _insert_statcast_final(db, home_runs=5, revision_number=1, observation_id=1)
    _insert_statcast_final(db, home_runs=6, revision_number=2, observation_id=2)

    result = load_final_game_score_inventory(cast(Database, db), _inventory(snapshot))

    assert result.scores[0].home_runs == 6
    assert result.lineages[0].result_revision_number == 2
    assert result.lineages[0].result_status_observation_id == 2


def test_missing_validated_statcast_final_is_explicitly_excluded() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )
    _insert_statcast_final(db, completion_contract="unsupported_completion_contract")

    result = load_final_game_score_inventory(cast(Database, db), _inventory(snapshot))

    assert result.scores == ()
    assert len(result.exclusions) == 1
    assert result.exclusions[0].reason == "missing_validated_statcast_final_score"


def test_final_score_source_fails_closed_on_completion_evidence_score_conflict() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )
    _insert_statcast_final(db, home_runs=5, evidence_home_runs=4)

    with pytest.raises(HistoricalTrainingSourceError, match="completion-source evidence"):
        load_final_game_score_inventory(cast(Database, db), _inventory(snapshot))


def test_final_score_source_rejects_final_evidence_at_or_before_first_pitch() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    start = datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc)
    _insert_daily_slate(db, snapshot, scheduled_start=start)
    _insert_statcast_final(db, retrieved_at=start)

    with pytest.raises(HistoricalTrainingSourceError, match="cannot predate or equal"):
        load_final_game_score_inventory(cast(Database, db), _inventory(snapshot))


def test_final_score_source_inventory_contains_no_market_price_context() -> None:
    db = _FixtureDatabase()
    snapshot = _snapshot("mfs-1", _feature_set())
    _insert_daily_slate(
        db,
        snapshot,
        scheduled_start=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
    )
    _insert_statcast_final(db)

    payload = load_final_game_score_inventory(cast(Database, db), _inventory(snapshot)).as_dict()
    text = str(payload).casefold()

    assert "bookmaker" not in text
    assert "sportsbook" not in text
    assert "best_price" not in text
    assert "american_price" not in text
    assert "no_vig" not in text
