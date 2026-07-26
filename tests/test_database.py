from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

import pytest

from app.database import BUSY_TIMEOUT_MS, Database, DatabaseInvariantError
from app.identifiers import generate_run_id
from app.migrations import CURRENT_SCHEMA_VERSION
from app.run_state import FailureStage, RunStatus


REQUESTED_DATE = date(2026, 7, 11)
TIMESTAMP = "2026-07-11T12:00:00+00:00"


def _game(event_id: str = "event-1") -> dict[str, object]:
    return {
        "id": event_id,
        "sport_key": "baseball_mlb",
        "commence_time": TIMESTAMP,
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [],
    }


def _create_run(database: Database) -> str:
    run_id = generate_run_id(REQUESTED_DATE)
    database.create_run(run_id, REQUESTED_DATE)
    return run_id


def test_every_connection_enables_fk_busy_timeout_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "settings.db")

    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == BUSY_TIMEOUT_MS
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_new_runs_record_current_schema_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "run-schema-version.db")
    run_id = _create_run(database)

    run = database.get_run(run_id)
    assert run is not None
    assert run["schema_version"] == CURRENT_SCHEMA_VERSION


def test_schema_rejects_noncanonical_requested_date_shape(tmp_path: Path) -> None:
    database = Database(tmp_path / "date-check.db")
    run_id = generate_run_id(REQUESTED_DATE)

    with pytest.raises(sqlite3.IntegrityError):
        with database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO collector_runs(
                    run_id, requested_date, status, created_at, queued_at,
                    updated_at, app_version, schema_version
                ) VALUES (?, '2026-7-11', 'queued', ?, ?, ?, 'test', 1)
                """,
                (run_id, TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )


def test_composite_fk_rejects_snapshot_without_run_game_association(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "foreign-key.db")
    run_id = _create_run(database)

    with pytest.raises(DatabaseInvariantError, match="run-game association"):
        database.insert_odds_batch(
            run_id,
            "missing-event",
            [
                {
                    "key": "book",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [{"name": "Home", "price": -110}],
                        }
                    ],
                }
            ],
            TIMESTAMP,
        )


def test_write_context_rolls_back_on_exception(tmp_path: Path) -> None:
    database = Database(tmp_path / "rollback.db")

    with pytest.raises(RuntimeError, match="abort"):
        with database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO games(
                    event_id, sport_key, commence_time, home_team, away_team,
                    raw_json, first_seen_at, last_seen_at
                ) VALUES ('rolled-back', 'baseball_mlb', ?, 'Home', 'Away', '{}', ?, ?)
                """,
                (TIMESTAMP, TIMESTAMP, TIMESTAMP),
            )
            raise RuntimeError("abort")

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM games WHERE event_id='rolled-back'"
        ).fetchone()[0] == 0


def test_repeated_odds_and_weather_snapshots_are_preserved(tmp_path: Path) -> None:
    database = Database(tmp_path / "snapshots.db")
    run_id = _create_run(database)
    database.upsert_game(_game(), "home", "away", run_id=run_id)
    bookmakers = [
        {
            "key": "book",
            "title": "Book",
            "last_update": TIMESTAMP,
            "markets": [
                {
                    "key": "h2h",
                    "last_update": TIMESTAMP,
                    "outcomes": [{"name": "Home", "price": -110}],
                }
            ],
        }
    ]

    assert database.insert_odds_batch(run_id, "event-1", bookmakers, TIMESTAMP) == 1
    assert database.insert_odds_batch(run_id, "event-1", bookmakers, TIMESTAMP) == 1
    weather = {"forecast_time": TIMESTAMP, "temperature_f": 70}
    database.insert_weather(run_id, "event-1", "nws", weather, TIMESTAMP)
    database.insert_weather(run_id, "event-1", "nws", weather, TIMESTAMP)

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM weather_snapshots").fetchone()[0] == 2


def test_odds_freshness_fields_are_persisted_from_nested_capture(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "freshness.db")
    run_id = _create_run(database)
    database.upsert_game(_game(), "home", "away", run_id=run_id)
    bookmakers = [
        {
            "key": "book",
            "provider_age_seconds": None,
            "bookmaker_age_seconds": 42.0,
            "markets": [
                {
                    "key": "totals",
                    "provider_age_seconds": None,
                    "market_age_seconds": 12.5,
                    "freshness_status": "fresh",
                    "outcomes": [{"name": "Over", "price": -110, "point": 8.5}],
                }
            ],
        }
    ]

    assert database.insert_odds_batch(run_id, "event-1", bookmakers, TIMESTAMP) == 1

    with database.connect() as connection:
        row = connection.execute("SELECT * FROM odds_snapshots").fetchone()
    assert row["provider_age_seconds"] is None
    assert row["bookmaker_age_seconds"] == 42.0
    assert row["market_age_seconds"] == 12.5
    assert row["freshness_status"] == "fresh"


def test_invalid_freshness_status_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-freshness.db")
    run_id = _create_run(database)
    database.upsert_game(_game(), "home", "away", run_id=run_id)

    with pytest.raises(sqlite3.IntegrityError):
        database.insert_odds_batch(
            run_id,
            "event-1",
            [
                {
                    "key": "book",
                    "markets": [
                        {
                            "key": "h2h",
                            "freshness_status": "expired",
                            "outcomes": [{"name": "Home", "price": -110}],
                        }
                    ],
                }
            ],
            TIMESTAMP,
        )


def test_negative_odds_age_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "invalid-age.db")
    run_id = _create_run(database)
    database.upsert_game(_game(), "home", "away", run_id=run_id)

    with pytest.raises(sqlite3.IntegrityError):
        database.insert_odds_batch(
            run_id,
            "event-1",
            [
                {
                    "key": "book",
                    "markets": [
                        {
                            "key": "h2h",
                            "market_age_seconds": -1,
                            "freshness_status": "fresh",
                            "outcomes": [{"name": "Arizona Diamondbacks", "price": -110}],
                        }
                    ],
                }
            ],
            TIMESTAMP,
        )

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 0


def test_game_association_and_all_odds_persist_as_one_unit(tmp_path: Path) -> None:
    database = Database(tmp_path / "atomic-game.db")
    run_id = _create_run(database)
    bookmakers = [
        {
            "key": "book",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Home", "price": -110},
                        {"name": "Away", "price": -105},
                    ],
                }
            ],
        }
    ]

    inserted = database.persist_game_with_odds(
        run_id, _game("atomic-event"), "home", "away", bookmakers, TIMESTAMP
    )

    assert inserted == 2
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM games WHERE event_id='atomic-event'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM run_games WHERE event_id='atomic-event'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_snapshots WHERE event_id='atomic-event'"
        ).fetchone()[0] == 2


def test_game_raw_json_preserves_provider_identifier_keys(tmp_path: Path) -> None:
    database = Database(tmp_path / "raw-provider-identifiers.db")
    run_id = _create_run(database)
    game = _game("raw-provider-identifiers")
    bookmakers = [
        {
            "key": "fanduel",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [{"name": "Home", "price": -110}],
                }
            ],
        }
    ]
    game["bookmakers"] = bookmakers

    database.persist_game_with_odds(
        run_id,
        game,
        "home",
        "away",
        bookmakers,
        TIMESTAMP,
    )

    with database.connect() as connection:
        raw = json.loads(
            connection.execute(
                "SELECT raw_json FROM games WHERE event_id='raw-provider-identifiers'"
            ).fetchone()[0]
        )
    assert raw["bookmakers"][0]["key"] == "fanduel"
    assert raw["bookmakers"][0]["markets"][0]["key"] == "h2h"


def test_game_odds_and_warnings_persist_atomically_and_are_queryable(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "atomic-warnings.db")
    run_id = _create_run(database)
    bookmakers = [
        {
            "key": "book",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [{"name": "Home", "price": -110}],
                }
            ],
        }
    ]

    inserted = database.persist_game_with_odds(
        run_id,
        _game("warning-event"),
        "home",
        "away",
        bookmakers,
        TIMESTAMP,
        warnings=[
            {
                "code": "incomplete_two_way_market",
                "bookmaker_key": "book",
                "market_key": "h2h",
                "message": "provider failed?apiKey=do-not-store",
                "created_at": TIMESTAMP,
            }
        ],
    )

    assert inserted == 1
    warnings = database.list_odds_warnings(run_id, event_id="warning-event")
    assert len(warnings) == 1
    assert warnings[0]["event_id"] == "warning-event"
    assert warnings[0]["code"] == "incomplete_two_way_market"
    assert "do-not-store" not in warnings[0]["message"]
    assert database.list_odds_warnings(run_id) == warnings


def test_warning_may_retain_unassociated_provider_event_id(tmp_path: Path) -> None:
    database = Database(tmp_path / "warning-foreign-key.db")
    run_id = _create_run(database)

    assert database.insert_odds_warnings(
        run_id,
        [{"event_id": "orphan", "code": "malformed_event", "message": "invalid"}],
    ) == 1

    assert database.list_odds_warnings(run_id, event_id="orphan")[0][
        "event_id"
    ] == "orphan"


def test_warning_requires_existing_run(tmp_path: Path) -> None:
    database = Database(tmp_path / "warning-run-foreign-key.db")
    missing_run_id = generate_run_id(REQUESTED_DATE)

    with pytest.raises(sqlite3.IntegrityError):
        database.insert_odds_warnings(
            missing_run_id,
            [{"event_id": "event", "code": "malformed_event", "message": "invalid"}],
        )


def test_warning_code_is_schema_constrained(tmp_path: Path) -> None:
    database = Database(tmp_path / "warning-code.db")
    run_id = _create_run(database)

    with pytest.raises(sqlite3.IntegrityError):
        database.insert_odds_warnings(
            run_id,
            [{"code": "invented_warning", "message": "invalid"}],
        )


def test_invalid_warning_prevents_game_and_odds_unit(tmp_path: Path) -> None:
    database = Database(tmp_path / "warning-atomic-rollback.db")
    run_id = _create_run(database)

    with pytest.raises(ValueError, match="non-empty code"):
        database.persist_game_with_odds(
            run_id,
            _game("new-event"),
            "home",
            "away",
            [
                {
                    "key": "book",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [{"name": "Home", "price": -110}],
                        }
                    ],
                }
            ],
            TIMESTAMP,
            warnings=[
                {
                    "code": "",
                    "message": "unknown",
                }
            ],
        )

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM games WHERE event_id='new-event'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_normalization_warnings"
        ).fetchone()[0] == 0


def test_odds_history_is_ordered_and_includes_game_orientation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "history.db")
    first_run = _create_run(database)
    second_run = _create_run(database)
    game = _game("history-event")
    bookmaker = {
        "key": "book",
        "last_update": TIMESTAMP,
        "markets": [
            {
                "key": "spreads",
                "last_update": TIMESTAMP,
                "freshness_status": "fresh",
                "outcomes": [{"name": "Home", "price": -110, "point": -1.5}],
            }
        ],
    }
    database.persist_game_with_odds(
        second_run,
        game,
        "home-key",
        "away-key",
        [bookmaker],
        "2026-07-11T13:00:00+00:00",
    )
    database.persist_game_with_odds(
        first_run,
        game,
        "home-key",
        "away-key",
        [bookmaker],
        "2026-07-11T12:00:00+00:00",
    )

    history = database.get_odds_history("history-event")

    assert [row["run_id"] for row in history] == [first_run, second_run]
    assert [row["retrieved_at"] for row in history] == [
        "2026-07-11T12:00:00+00:00",
        "2026-07-11T13:00:00+00:00",
    ]
    assert history[0]["home_team"] == "Home"
    assert history[0]["away_team"] == "Away"
    assert history[0]["home_team_key"] == "home-key"
    assert history[0]["market_last_update"] == TIMESTAMP
    assert history[0]["freshness_status"] == "fresh"


def test_game_and_association_roll_back_when_an_odds_insert_fails(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "atomic-rollback.db")
    run_id = _create_run(database)
    invalid_bookmakers = [
        {
            "key": "book",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [{"name": "Home", "price": {"invalid": True}}],
                }
            ],
        }
    ]

    with pytest.raises(sqlite3.ProgrammingError):
        database.persist_game_with_odds(
            run_id,
            _game("rolled-back-event"),
            "home",
            "away",
            invalid_bookmakers,
            TIMESTAMP,
        )

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM games WHERE event_id='rolled-back-event'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM run_games WHERE event_id='rolled-back-event'"
        ).fetchone()[0] == 0


def test_run_transitions_are_atomic_and_audited(tmp_path: Path) -> None:
    database = Database(tmp_path / "transitions.db")
    run_id = _create_run(database)

    running = database.transition_run(run_id, RunStatus.RUNNING)
    completed = database.transition_run(
        run_id,
        RunStatus.COMPLETED,
        artifact_relpath="2026-07-11/run.zip",
    )

    assert running["started_at"] is not None
    assert completed["status"] == RunStatus.COMPLETED.value
    assert completed["artifact_relpath"] == "2026-07-11/run.zip"
    assert [row["to_status"] for row in database.get_run_transitions(run_id)] == [
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.COMPLETED.value,
    ]


def test_startup_reconciliation_fails_only_incomplete_runs(tmp_path: Path) -> None:
    database = Database(tmp_path / "reconcile.db")
    queued_id = _create_run(database)
    running_id = _create_run(database)
    complete_id = _create_run(database)
    database.transition_run(running_id, RunStatus.RUNNING)
    database.transition_run(complete_id, RunStatus.RUNNING)
    database.transition_run(complete_id, RunStatus.COMPLETED)

    assert database.reconcile_incomplete_runs(error_message="service restarted") == 2
    queued = database.get_run(queued_id)
    running = database.get_run(running_id)
    complete = database.get_run(complete_id)
    assert queued is not None
    assert running is not None
    assert complete is not None
    assert queued["failure_stage"] == FailureStage.STARTUP_RECONCILIATION.value
    assert running["failure_stage"] == FailureStage.STARTUP_RECONCILIATION.value
    assert complete["status"] == RunStatus.COMPLETED.value


def test_raw_payload_metadata_uses_run_game_composite_fk(tmp_path: Path) -> None:
    database = Database(tmp_path / "raw.db")
    run_id = _create_run(database)
    database.upsert_game(_game(), "home", "away", run_id=run_id)

    row_id = database.insert_raw_payload(
        run_id=run_id,
        event_id="event-1",
        provider="nws",
        endpoint_category="hourly_forecast",
        provider_timestamp=TIMESTAMP,
        retrieved_at=TIMESTAMP,
        content_type="application/json",
        checksum_sha256="a" * 64,
        artifact_relpath="raw/nws/hourly.json",
    )

    assert row_id > 0
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM raw_provider_payloads WHERE id=?", (row_id,)
        ).fetchone()
    assert row["checksum_sha256"] == "a" * 64
    assert row["artifact_relpath"] == "raw/nws/hourly.json"


def test_second_writer_waits_for_first_transaction(tmp_path: Path) -> None:
    database = Database(tmp_path / "contention.db")
    second_run_id = generate_run_id(REQUESTED_DATE)
    started = threading.Event()
    finished = threading.Event()
    failures: list[BaseException] = []

    def second_writer() -> None:
        started.set()
        try:
            database.create_run(second_run_id, REQUESTED_DATE)
        except BaseException as exc:
            failures.append(exc)
        finally:
            finished.set()

    with database.connect(write=True):
        worker = threading.Thread(target=second_writer)
        worker.start()
        assert started.wait(timeout=1)
        time.sleep(0.1)
        assert not finished.is_set()

    worker.join(timeout=2)
    assert finished.is_set()
    assert failures == []
    assert database.get_run(second_run_id) is not None


def test_integrity_helper_reports_clean_database(tmp_path: Path) -> None:
    database = Database(tmp_path / "integrity.db")

    assert database.integrity_check() == {
        "ok": True,
        "integrity_check": ["ok"],
        "foreign_key_violations": [],
    }
    database.verify_integrity()
