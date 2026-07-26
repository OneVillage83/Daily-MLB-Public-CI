from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.identifiers import parse_requested_date, validate_run_id
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    FORMAL_SCHEMA_V7_FINGERPRINT,
    MIGRATION_V7_CHECKSUM,
    MigrationResult,
    ensure_schema,
    schema_fingerprint,
)
from app.redaction import redact_text, redact_value
from app.run_state import FailureStage, RunStatus, validate_transition


BUSY_TIMEOUT_MS = 5_000
TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNINGS,
    RunStatus.FAILED,
}


class DatabaseError(RuntimeError):
    pass


class DatabaseInvariantError(DatabaseError):
    pass


class RunNotFoundError(DatabaseError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _required_text(record: Mapping[str, Any], key: str) -> str:
    value = str(record.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} must not be empty")
    return value


def _checksum(record: Mapping[str, Any], key: str) -> str:
    value = _required_text(record, key).lower()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{key} must be 64 lowercase hexadecimal characters")
    return value


def _checksum_alias(
    record: Mapping[str, Any], key: str, *aliases: str
) -> str:
    if key in record:
        return _checksum(record, key)
    for alias in aliases:
        if alias in record:
            value = _required_text(record, alias).lower()
            if len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(
                    f"{alias} must be 64 lowercase hexadecimal characters"
                )
            return value
    raise ValueError(f"{key} must not be empty")


def _payload_json(value: Any) -> str:
    return _canonical_json(
        redact_value(
            value,
            preserve_field_names=(
                "id",
                "key",
                "source_id",
                "provider_id",
                "location_id",
                "venue_id",
                "bookmaker_key",
            ),
        )
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _artifact_relpath(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in path.parts[0]
    ):
        raise ValueError("artifact_relpath must be a contained relative path")
    return path.as_posix()


def _enum_or_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _odds_snapshot_values(
    run_id: str,
    event_id: str,
    bookmakers: list[dict[str, Any]],
    retrieved_at: str,
) -> list[tuple[Any, ...]]:
    values: list[tuple[Any, ...]] = []
    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            provider_age = market.get(
                "provider_age_seconds", bookmaker.get("provider_age_seconds")
            )
            bookmaker_age = market.get(
                "bookmaker_age_seconds", bookmaker.get("bookmaker_age_seconds")
            )
            market_age = market.get("market_age_seconds")
            freshness = _enum_or_value(
                market.get(
                    "freshness_status",
                    bookmaker.get("freshness_status", "unknown"),
                )
            )
            for outcome in market.get("outcomes", []):
                values.append(
                    (
                        run_id,
                        event_id,
                        bookmaker.get("key", "unknown"),
                        bookmaker.get("title"),
                        market.get("key", "unknown"),
                        market.get("last_update"),
                        outcome.get("name", "unknown"),
                        outcome.get("price"),
                        outcome.get("point"),
                        bookmaker.get("last_update"),
                        retrieved_at,
                        _canonical_json(redact_value(outcome)),
                        provider_age,
                        bookmaker_age,
                        market_age,
                        freshness,
                    )
                )
    return values


def _odds_warning_values(
    run_id: str,
    warnings: Sequence[Mapping[str, object]],
    *,
    default_event_id: str | None = None,
) -> list[tuple[Any, ...]]:
    values: list[tuple[Any, ...]] = []
    for warning in warnings:
        raw_code = _enum_or_value(warning.get("code"))
        raw_message = warning.get("message")
        code = "" if raw_code is None else str(raw_code).strip()
        message = (
            "" if raw_message is None else redact_text(str(raw_message)).strip()
        )
        if not code or not message:
            raise ValueError("Odds warnings require non-empty code and message")
        event_id = (
            warning["event_id"]
            if "event_id" in warning
            else default_event_id
        )
        values.append(
            (
                run_id,
                event_id,
                warning.get("bookmaker_key", warning.get("bookmaker")),
                warning.get("market_key", warning.get("market")),
                code,
                message,
                str(warning.get("created_at") or utc_now()),
            )
        )
    return values


class Database:
    def __init__(self, path: Path | str, *, busy_timeout_ms: int = BUSY_TIMEOUT_MS):
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if str(path) == ":memory:":
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="mlb-phase1-sqlite-"
            )
            self.path = Path(self._temporary_directory.name) / "database.sqlite3"
        else:
            self.path = Path(path).resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self.migration_result: MigrationResult = ensure_schema(self.path)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            connection.close()
            raise DatabaseInvariantError("SQLite foreign-key enforcement is required")
        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            connection.close()
            raise DatabaseInvariantError("File-backed SQLite databases must use WAL")
        return connection

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def create_run(
        self,
        run_id: str,
        requested_date: date | str,
        app_version: str = "0.1.0",
    ) -> dict[str, Any]:
        if isinstance(requested_date, datetime):
            raise TypeError("requested_date must be a date or exact YYYY-MM-DD string")
        if isinstance(requested_date, date):
            parsed_date = requested_date
        else:
            parsed_date = parse_requested_date(requested_date)
        safe_run_id = validate_run_id(run_id, expected_date=parsed_date)
        if not app_version.strip():
            raise ValueError("app_version must not be empty")
        created_at = utc_now()
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO collector_runs(
                    run_id, requested_date, status, created_at, queued_at,
                    started_at, completed_at, updated_at, failure_stage,
                    error_message, artifact_relpath, app_version, schema_version
                ) VALUES (?, ?, 'queued', ?, ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    safe_run_id,
                    parsed_date.isoformat(),
                    created_at,
                    created_at,
                    created_at,
                    app_version.strip(),
                    CURRENT_SCHEMA_VERSION,
                ),
            )
            connection.execute(
                """
                INSERT INTO collector_run_transitions(
                    run_id, from_status, to_status, failure_stage,
                    error_message, transitioned_at
                ) VALUES (?, NULL, 'queued', NULL, NULL, ?)
                """,
                (safe_run_id, created_at),
            )
            row = connection.execute(
                "SELECT * FROM collector_runs WHERE run_id=?", (safe_run_id,)
            ).fetchone()
        result = _row_dict(row)
        if result is None:
            raise DatabaseInvariantError("Created run could not be read back")
        return result

    def transition_run(
        self,
        run_id: str,
        target: RunStatus,
        *,
        failure_stage: FailureStage | None = None,
        error_message: str | None = None,
        artifact_relpath: str | None = None,
        transitioned_at: str | None = None,
    ) -> dict[str, Any]:
        safe_run_id = validate_run_id(run_id)
        target_status = target if isinstance(target, RunStatus) else RunStatus(target)
        safe_failure_stage = (
            failure_stage
            if failure_stage is None or isinstance(failure_stage, FailureStage)
            else FailureStage(failure_stage)
        )
        safe_error = redact_text(error_message) if error_message is not None else None
        safe_artifact = _artifact_relpath(artifact_relpath)
        timestamp = transitioned_at or utc_now()

        with self.connect(write=True) as connection:
            current_row = connection.execute(
                "SELECT * FROM collector_runs WHERE run_id=?", (safe_run_id,)
            ).fetchone()
            if current_row is None:
                raise RunNotFoundError(f"Run not found: {safe_run_id}")
            current_status = RunStatus(current_row["status"])
            validate_transition(
                current_status,
                target_status,
                failure_stage=safe_failure_stage,
            )

            started_at = current_row["started_at"]
            completed_at = current_row["completed_at"]
            if target_status is RunStatus.RUNNING:
                started_at = timestamp
            if target_status in TERMINAL_RUN_STATUSES:
                completed_at = timestamp

            connection.execute(
                """
                UPDATE collector_runs
                SET status=?, started_at=?, completed_at=?, updated_at=?,
                    failure_stage=?, error_message=?,
                    artifact_relpath=COALESCE(?, artifact_relpath)
                WHERE run_id=?
                """,
                (
                    target_status.value,
                    started_at,
                    completed_at,
                    timestamp,
                    safe_failure_stage.value if safe_failure_stage else None,
                    safe_error,
                    safe_artifact,
                    safe_run_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO collector_run_transitions(
                    run_id, from_status, to_status, failure_stage,
                    error_message, transitioned_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_run_id,
                    current_status.value,
                    target_status.value,
                    safe_failure_stage.value if safe_failure_stage else None,
                    safe_error,
                    timestamp,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM collector_runs WHERE run_id=?", (safe_run_id,)
            ).fetchone()
        result = _row_dict(updated)
        if result is None:
            raise DatabaseInvariantError("Transitioned run could not be read back")
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        safe_run_id = validate_run_id(run_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM collector_runs WHERE run_id=?", (safe_run_id,)
            ).fetchone()
        return _row_dict(row)

    def get_run_transitions(self, run_id: str) -> list[dict[str, Any]]:
        safe_run_id = validate_run_id(run_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM collector_run_transitions
                WHERE run_id=? ORDER BY transitioned_at, id
                """,
                (safe_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def reconcile_incomplete_runs(
        self,
        *,
        error_message: str = "Run failed during startup reconciliation",
    ) -> int:
        timestamp = utc_now()
        stage = FailureStage.STARTUP_RECONCILIATION
        safe_error = redact_text(error_message)
        with self.connect(write=True) as connection:
            rows = connection.execute(
                """
                SELECT run_id, status FROM collector_runs
                WHERE status IN ('queued', 'running') ORDER BY run_id
                """
            ).fetchall()
            for row in rows:
                current = RunStatus(row["status"])
                validate_transition(current, RunStatus.FAILED, failure_stage=stage)
                connection.execute(
                    """
                    UPDATE collector_runs
                    SET status='failed', completed_at=?, updated_at=?, failure_stage=?,
                        error_message=?
                    WHERE run_id=?
                    """,
                    (timestamp, timestamp, stage.value, safe_error, row["run_id"]),
                )
                connection.execute(
                    """
                    INSERT INTO collector_run_transitions(
                        run_id, from_status, to_status, failure_stage,
                        error_message, transitioned_at
                    ) VALUES (?, ?, 'failed', ?, ?, ?)
                    """,
                    (
                        row["run_id"],
                        current.value,
                        stage.value,
                        safe_error,
                        timestamp,
                    ),
                )
        return len(rows)

    def upsert_game(
        self,
        game: Mapping[str, Any],
        home_key: str | None,
        away_key: str | None,
        *,
        run_id: str | None = None,
    ) -> None:
        event_id = str(game["id"])
        now = utc_now()
        safe_run_id = validate_run_id(run_id) if run_id is not None else None
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO games(
                    event_id, sport_key, commence_time, home_team, away_team,
                    home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    sport_key=excluded.sport_key,
                    commence_time=excluded.commence_time,
                    home_team=excluded.home_team,
                    away_team=excluded.away_team,
                    home_team_key=excluded.home_team_key,
                    away_team_key=excluded.away_team_key,
                    raw_json=excluded.raw_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    event_id,
                    game.get("sport_key", "baseball_mlb"),
                    game["commence_time"],
                    game["home_team"],
                    game["away_team"],
                    home_key,
                    away_key,
                    _canonical_json(
                        redact_value(dict(game), preserve_field_names=("key",))
                    ),
                    now,
                    now,
                ),
            )
            if safe_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO run_games(run_id, event_id, associated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(run_id, event_id) DO NOTHING
                    """,
                    (safe_run_id, event_id, now),
                )

    def associate_run_game(
        self, run_id: str, event_id: str, *, associated_at: str | None = None
    ) -> None:
        safe_run_id = validate_run_id(run_id)
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO run_games(run_id, event_id, associated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id, event_id) DO NOTHING
                """,
                (safe_run_id, event_id, associated_at or utc_now()),
            )

    def persist_game_with_odds(
        self,
        run_id: str,
        game: dict[str, Any],
        home_key: str | None,
        away_key: str | None,
        bookmakers: list[dict[str, Any]],
        retrieved_at: str,
        *,
        warnings: Sequence[Mapping[str, object]] | None = None,
    ) -> int:
        safe_run_id = validate_run_id(run_id)
        event_id = str(game["id"])
        now = utc_now()
        values = _odds_snapshot_values(
            safe_run_id, event_id, bookmakers, retrieved_at
        )
        warning_values = _odds_warning_values(
            safe_run_id,
            warnings or [],
            default_event_id=event_id,
        )

        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO games(
                    event_id, sport_key, commence_time, home_team, away_team,
                    home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    sport_key=excluded.sport_key,
                    commence_time=excluded.commence_time,
                    home_team=excluded.home_team,
                    away_team=excluded.away_team,
                    home_team_key=excluded.home_team_key,
                    away_team_key=excluded.away_team_key,
                    raw_json=excluded.raw_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    event_id,
                    game.get("sport_key", "baseball_mlb"),
                    game["commence_time"],
                    game["home_team"],
                    game["away_team"],
                    home_key,
                    away_key,
                    _canonical_json(
                        redact_value(game, preserve_field_names=("key",))
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO run_games(run_id, event_id, associated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id, event_id) DO NOTHING
                """,
                (safe_run_id, event_id, now),
            )
            if values:
                connection.executemany(
                    """
                    INSERT INTO odds_snapshots(
                        run_id, event_id, bookmaker_key, bookmaker_title,
                        market_key, market_last_update, outcome_name, price, point,
                        bookmaker_last_update, retrieved_at, raw_json,
                        provider_age_seconds, bookmaker_age_seconds,
                        market_age_seconds, freshness_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            if warning_values:
                connection.executemany(
                    """
                    INSERT INTO odds_normalization_warnings(
                        run_id, event_id, bookmaker_key, market_key,
                        code, message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    warning_values,
                )
        return len(values)

    def insert_odds_batch(
        self,
        run_id: str,
        event_id: str,
        bookmakers: list[dict[str, Any]],
        retrieved_at: str,
    ) -> int:
        safe_run_id = validate_run_id(run_id)
        values = _odds_snapshot_values(
            safe_run_id, event_id, bookmakers, retrieved_at
        )
        with self.connect(write=True) as connection:
            association = connection.execute(
                "SELECT 1 FROM run_games WHERE run_id=? AND event_id=?",
                (safe_run_id, event_id),
            ).fetchone()
            if association is None:
                raise DatabaseInvariantError(
                    "Odds snapshots require an existing run-game association"
                )
            if values:
                connection.executemany(
                    """
                    INSERT INTO odds_snapshots(
                        run_id, event_id, bookmaker_key, bookmaker_title,
                        market_key, market_last_update, outcome_name, price, point,
                        bookmaker_last_update, retrieved_at, raw_json,
                        provider_age_seconds, bookmaker_age_seconds,
                        market_age_seconds, freshness_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
        return len(values)

    def insert_odds(
        self,
        run_id: str,
        event_id: str,
        bookmaker: dict[str, Any],
        market: dict[str, Any],
        outcome: dict[str, Any],
        retrieved_at: str,
    ) -> None:
        nested_bookmaker = dict(bookmaker)
        nested_market = dict(market)
        nested_market["outcomes"] = [outcome]
        nested_bookmaker["markets"] = [nested_market]
        self.insert_odds_batch(run_id, event_id, [nested_bookmaker], retrieved_at)

    def insert_odds_warnings(
        self,
        run_id: str,
        warnings: Sequence[Mapping[str, object]],
        *,
        default_event_id: str | None = None,
    ) -> int:
        safe_run_id = validate_run_id(run_id)
        values = _odds_warning_values(
            safe_run_id,
            warnings,
            default_event_id=default_event_id,
        )
        if not values:
            return 0
        with self.connect(write=True) as connection:
            connection.executemany(
                """
                INSERT INTO odds_normalization_warnings(
                    run_id, event_id, bookmaker_key, market_key,
                    code, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return len(values)

    def list_odds_warnings(
        self,
        run_id: str,
        *,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_run_id = validate_run_id(run_id)
        with self.connect() as connection:
            if event_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM odds_normalization_warnings
                    WHERE run_id=? ORDER BY created_at, id
                    """,
                    (safe_run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM odds_normalization_warnings
                    WHERE run_id=? AND event_id=? ORDER BY created_at, id
                    """,
                    (safe_run_id, event_id),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_odds_history(self, event_id: str) -> list[dict[str, Any]]:
        safe_event_id = event_id.strip()
        if not safe_event_id:
            raise ValueError("event_id must not be empty")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    snapshots.*,
                    games.sport_key,
                    games.commence_time,
                    games.home_team,
                    games.away_team,
                    games.home_team_key,
                    games.away_team_key
                FROM odds_snapshots AS snapshots
                JOIN games ON games.event_id = snapshots.event_id
                WHERE snapshots.event_id=?
                ORDER BY snapshots.retrieved_at, snapshots.id
                """,
                (safe_event_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_weather(
        self,
        run_id: str,
        event_id: str,
        provider: str,
        row: Mapping[str, Any],
        retrieved_at: str,
        *,
        raw_json: Any | None = None,
    ) -> None:
        safe_run_id = validate_run_id(run_id)
        payload = dict(row) if raw_json is None else raw_json
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO weather_snapshots(
                    run_id, event_id, provider, forecast_time, temperature_f,
                    humidity_pct, precipitation_probability_pct, wind_speed_mph,
                    wind_direction_deg, short_forecast, raw_json, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_run_id,
                    event_id,
                    provider,
                    row.get("forecast_time"),
                    row.get("temperature_f"),
                    row.get("humidity_pct"),
                    row.get("precipitation_probability_pct"),
                    row.get("wind_speed_mph"),
                    row.get("wind_direction_deg"),
                    row.get("short_forecast"),
                    _canonical_json(redact_value(payload)),
                    retrieved_at,
                ),
            )

    def add_error(
        self,
        run_id: str,
        stage: str,
        message: str,
        event_id: str | None = None,
        details: str | None = None,
        *,
        provider: str | None = None,
    ) -> None:
        safe_run_id = validate_run_id(run_id)
        safe_message = redact_text(message)
        safe_details = redact_text(details) if details is not None else None
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO collector_errors(
                    run_id, stage, provider, event_id, message, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_run_id,
                    stage,
                    provider,
                    event_id,
                    safe_message,
                    safe_details,
                    utc_now(),
                ),
            )

    def get_latest_error(
        self,
        run_id: str,
        *,
        stage: str,
    ) -> dict[str, Any] | None:
        safe_run_id = validate_run_id(run_id)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM collector_errors
                WHERE run_id=? AND stage=?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (safe_run_id, stage),
            ).fetchone()
        return _row_dict(row)

    def insert_raw_payload(
        self,
        *,
        run_id: str,
        event_id: str | None,
        provider: str,
        endpoint_category: str,
        provider_timestamp: str | None,
        retrieved_at: str,
        content_type: str,
        checksum_sha256: str,
        artifact_relpath: str,
    ) -> int:
        safe_run_id = validate_run_id(run_id)
        safe_artifact = _artifact_relpath(artifact_relpath)
        normalized_checksum = checksum_sha256.strip().lower()
        if len(normalized_checksum) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_checksum
        ):
            raise ValueError("checksum_sha256 must be 64 lowercase hexadecimal characters")
        with self.connect(write=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO raw_provider_payloads(
                    run_id, event_id, provider, endpoint_category,
                    provider_timestamp, retrieved_at, content_type,
                    checksum_sha256, artifact_relpath
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    safe_run_id,
                    event_id,
                    provider,
                    endpoint_category,
                    provider_timestamp,
                    retrieved_at,
                    content_type,
                    normalized_checksum,
                    safe_artifact,
                ),
            )
            row_id = cursor.lastrowid
        if row_id is None:
            raise DatabaseInvariantError("Raw payload insert did not return an identifier")
        return int(row_id)

    def list_run_games(self, run_id: str) -> list[dict[str, Any]]:
        safe_run_id = validate_run_id(run_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT games.*, run_games.associated_at
                FROM run_games
                JOIN games ON games.event_id = run_games.event_id
                WHERE run_games.run_id=?
                ORDER BY games.commence_time, games.event_id
                """,
                (safe_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_odds_snapshots(
        self, run_id: str, *, event_id: str | None = None
    ) -> list[dict[str, Any]]:
        safe_run_id = validate_run_id(run_id)
        parameters: tuple[str, ...]
        predicate = "run_id=?"
        parameters = (safe_run_id,)
        if event_id is not None:
            safe_event_id = event_id.strip()
            if not safe_event_id:
                raise ValueError("event_id must not be empty")
            predicate += " AND event_id=?"
            parameters = (safe_run_id, safe_event_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM odds_snapshots
                WHERE {predicate}
                ORDER BY event_id, retrieved_at, id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_weather_snapshots(
        self, run_id: str, *, event_id: str | None = None
    ) -> list[dict[str, Any]]:
        safe_run_id = validate_run_id(run_id)
        parameters: tuple[str, ...]
        predicate = "run_id=?"
        parameters = (safe_run_id,)
        if event_id is not None:
            safe_event_id = event_id.strip()
            if not safe_event_id:
                raise ValueError("event_id must not be empty")
            predicate += " AND event_id=?"
            parameters = (safe_run_id, safe_event_id)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM weather_snapshots
                WHERE {predicate}
                ORDER BY event_id, retrieved_at, id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_sealed_prediction(
        self,
        *,
        evidence: Mapping[str, Any],
        prediction: Mapping[str, Any],
    ) -> dict[str, Any]:
        evidence_id = _required_text(evidence, "evidence_id")
        prediction_id = _required_text(prediction, "prediction_id")
        run_id = validate_run_id(_required_text(prediction, "run_id"))
        event_id = _required_text(prediction, "event_id")
        if run_id != validate_run_id(_required_text(evidence, "run_id")):
            raise ValueError("prediction and evidence run_id must match")
        if event_id != _required_text(evidence, "event_id"):
            raise ValueError("prediction and evidence event_id must match")
        evidence_checksum = _checksum(evidence, "evidence_checksum")
        if evidence_checksum != _checksum(prediction, "evidence_checksum"):
            raise ValueError("prediction and evidence checksums must match")

        evidence_values = (
            evidence_id,
            run_id,
            event_id,
            _required_text(evidence, "contract_version"),
            _required_text(evidence, "analyst_id"),
            _required_text(evidence, "method_version"),
            _payload_json(evidence.get("evidence", evidence.get("payload", evidence))),
            evidence_checksum,
            _checksum(evidence, "source_checksum"),
            _required_text(evidence, "created_at"),
        )
        prediction_values = (
            prediction_id,
            evidence_id,
            run_id,
            event_id,
            _required_text(prediction, "contract_version"),
            _required_text(prediction, "analyst_id"),
            _required_text(prediction, "method_version"),
            float(prediction["home_probability"]),
            float(prediction["away_probability"]),
            float(prediction["lower_bound"]),
            float(prediction["upper_bound"]),
            _checksum_alias(
                prediction, "feature_checksum", "canonical_game_checksum"
            ),
            evidence_checksum,
            _checksum_alias(prediction, "prediction_checksum", "checksum"),
            _payload_json(prediction.get("payload", prediction)),
            _required_text(prediction, "sealed_at"),
        )

        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO reviewed_prediction_evidence(
                    evidence_id, run_id, event_id, contract_version, analyst_id,
                    method_version, evidence_json, evidence_checksum,
                    source_checksum, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                evidence_values,
            )
            connection.execute(
                """
                INSERT INTO reviewed_predictions(
                    prediction_id, evidence_id, run_id, event_id, contract_version,
                    analyst_id, method_version, home_probability, away_probability,
                    lower_bound, upper_bound, feature_checksum, evidence_checksum,
                    prediction_checksum, payload_json, sealed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prediction_values,
            )
        result = self.get_sealed_prediction(prediction_id)
        if result is None:
            raise DatabaseInvariantError("Sealed prediction could not be read back")
        return result

    def get_sealed_prediction(self, prediction_id: str) -> dict[str, Any] | None:
        safe_id = prediction_id.strip()
        if not safe_id:
            raise ValueError("prediction_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT prediction.*, evidence.evidence_json, evidence.source_checksum
                    AS evidence_source_checksum, evidence.created_at AS evidence_created_at
                FROM reviewed_predictions AS prediction
                JOIN reviewed_prediction_evidence AS evidence
                  ON evidence.evidence_id = prediction.evidence_id
                WHERE prediction.prediction_id=?
                """,
                (safe_id,),
            ).fetchone()
        return _row_dict(row)

    def insert_market_evaluation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        evaluation_id = _required_text(record, "evaluation_id")
        run_id = validate_run_id(_required_text(record, "run_id"))
        values = (
            evaluation_id,
            _required_text(record, "prediction_id"),
            run_id,
            _required_text(record, "event_id"),
            _required_text(record, "policy_version"),
            _checksum(record, "market_snapshot_checksum"),
            _optional_float(record.get("market_no_vig_probability")),
            _optional_float(record.get("best_price")),
            _payload_json(record.get("best_price_books", [])),
            _optional_float(record.get("break_even_probability")),
            _optional_float(record.get("edge_percentage_points")),
            _optional_float(record.get("expected_value_per_unit_risk")),
            int(record["bookmaker_count"]),
            _checksum(record, "source_checksum"),
            _payload_json(record.get("payload", record)),
            _required_text(record, "evaluated_at"),
        )
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO market_evaluations(
                    evaluation_id, prediction_id, run_id, event_id, policy_version,
                    market_snapshot_checksum, market_no_vig_probability, best_price,
                    best_price_books_json, break_even_probability,
                    edge_percentage_points, expected_value_per_unit_risk,
                    bookmaker_count, source_checksum, payload_json, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        result = self.get_market_evaluation(evaluation_id)
        if result is None:
            raise DatabaseInvariantError("Market evaluation could not be read back")
        return result

    def get_market_evaluation(self, evaluation_id: str) -> dict[str, Any] | None:
        safe_id = evaluation_id.strip()
        if not safe_id:
            raise ValueError("evaluation_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_evaluations WHERE evaluation_id=?", (safe_id,)
            ).fetchone()
        return _row_dict(row)

    def insert_policy_evaluation(
        self,
        record: Mapping[str, Any],
        gate_results: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        policy_evaluation_id = _required_text(record, "policy_evaluation_id")
        all_gates_passed = bool(record["all_gates_passed"])
        if not gate_results:
            raise ValueError("policy evaluation requires individual gate results")
        if all(bool(gate["passed"]) for gate in gate_results) != all_gates_passed:
            raise ValueError("all_gates_passed must match individual gate results")
        policy_values = (
            policy_evaluation_id,
            _required_text(record, "evaluation_id"),
            _required_text(record, "prediction_id"),
            _required_text(record, "policy_version"),
            _required_text(record, "outcome"),
            int(all_gates_passed),
            _checksum(record, "source_checksum"),
            _payload_json(record.get("payload", record)),
            _required_text(record, "evaluated_at"),
        )
        gate_values = [
            (
                _required_text(gate, "gate_result_id"),
                policy_evaluation_id,
                _required_text(gate, "gate_code"),
                _payload_json(gate.get("threshold")),
                _payload_json(gate.get("observed")),
                int(bool(gate["passed"])),
                redact_text(_required_text(gate, "reason")),
                _checksum(gate, "source_checksum"),
                _required_text(gate, "evaluated_at"),
            )
            for gate in gate_results
        ]
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO policy_evaluations(
                    policy_evaluation_id, evaluation_id, prediction_id,
                    policy_version, outcome, all_gates_passed, source_checksum,
                    payload_json, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                policy_values,
            )
            connection.executemany(
                """
                INSERT INTO policy_gate_results(
                    gate_result_id, policy_evaluation_id, gate_code,
                    threshold_json, observed_json, passed, reason,
                    source_checksum, evaluated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                gate_values,
            )
        result = self.get_policy_evaluation(policy_evaluation_id)
        if result is None:
            raise DatabaseInvariantError("Policy evaluation could not be read back")
        return result

    def get_policy_evaluation(self, policy_evaluation_id: str) -> dict[str, Any] | None:
        safe_id = policy_evaluation_id.strip()
        if not safe_id:
            raise ValueError("policy_evaluation_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM policy_evaluations WHERE policy_evaluation_id=?",
                (safe_id,),
            ).fetchone()
            if row is None:
                return None
            gates = connection.execute(
                """
                SELECT * FROM policy_gate_results
                WHERE policy_evaluation_id=? ORDER BY gate_code
                """,
                (safe_id,),
            ).fetchall()
        result = dict(row)
        result["gate_results"] = [dict(gate) for gate in gates]
        return result

    def insert_card_draft(
        self,
        record: Mapping[str, Any],
        policy_evaluation_ids: Sequence[str],
    ) -> dict[str, Any]:
        draft_id = _required_text(record, "draft_id")
        run_id = validate_run_id(_required_text(record, "run_id"))
        unique_evaluations = tuple(dict.fromkeys(value.strip() for value in policy_evaluation_ids))
        if any(not value for value in unique_evaluations):
            raise ValueError("policy_evaluation_ids must not contain empty values")
        candidate_count = int(record["candidate_count"])
        if candidate_count != len(unique_evaluations):
            raise ValueError("candidate_count must match linked policy evaluations")
        values = (
            draft_id,
            run_id,
            parse_requested_date(_required_text(record, "requested_date")).isoformat(),
            _required_text(record, "policy_version"),
            candidate_count,
            _checksum(record, "draft_checksum"),
            _checksum(record, "source_checksum"),
            _payload_json(record.get("payload", record)),
            _required_text(record, "created_at"),
        )
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO card_drafts(
                    draft_id, run_id, requested_date, policy_version,
                    candidate_count, draft_checksum, source_checksum,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            connection.executemany(
                """
                INSERT INTO draft_policy_evaluations(draft_id, policy_evaluation_id)
                VALUES (?, ?)
                """,
                [(draft_id, value) for value in unique_evaluations],
            )
        result = self.get_card_draft(draft_id)
        if result is None:
            raise DatabaseInvariantError("Card draft could not be read back")
        return result

    def get_card_draft(self, draft_id: str) -> dict[str, Any] | None:
        safe_id = draft_id.strip()
        if not safe_id:
            raise ValueError("draft_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM card_drafts WHERE draft_id=?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            evaluations = connection.execute(
                """
                SELECT policy_evaluation_id FROM draft_policy_evaluations
                WHERE draft_id=? ORDER BY policy_evaluation_id
                """,
                (safe_id,),
            ).fetchall()
        result = dict(row)
        result["policy_evaluation_ids"] = [item[0] for item in evaluations]
        return result

    def insert_review_decision(self, record: Mapping[str, Any]) -> dict[str, Any]:
        decision_id = _required_text(record, "decision_id")
        values = (
            decision_id,
            _required_text(record, "draft_id"),
            _required_text(record, "policy_evaluation_id"),
            _required_text(record, "reviewer_id"),
            _required_text(record, "decision"),
            redact_text(_required_text(record, "reason")),
            _checksum(record, "draft_checksum"),
            _checksum(record, "prediction_checksum"),
            _required_text(record, "policy_version"),
            _checksum(record, "source_checksum"),
            _required_text(record, "decided_at"),
        )
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO review_decisions(
                    decision_id, draft_id, policy_evaluation_id, reviewer_id,
                    decision, reason, draft_checksum, prediction_checksum,
                    policy_version, source_checksum, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM review_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        result = _row_dict(row)
        if result is None:
            raise DatabaseInvariantError("Review decision could not be read back")
        return result

    def list_review_decisions(self, draft_id: str) -> list[dict[str, Any]]:
        safe_id = draft_id.strip()
        if not safe_id:
            raise ValueError("draft_id must not be empty")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_decisions
                WHERE draft_id=? ORDER BY decided_at, decision_id
                """,
                (safe_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_review_decision(self, decision_id: str) -> dict[str, Any] | None:
        safe_id = decision_id.strip()
        if not safe_id:
            raise ValueError("decision_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM review_decisions WHERE decision_id=?", (safe_id,)
            ).fetchone()
        return _row_dict(row)

    def insert_publication_batch(
        self,
        batch: Mapping[str, Any],
        plays: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        batch_id = _required_text(batch, "batch_id")
        run_id = validate_run_id(_required_text(batch, "run_id"))
        batch_values = (
            batch_id,
            _required_text(batch, "draft_id"),
            run_id,
            parse_requested_date(_required_text(batch, "requested_date")).isoformat(),
            _required_text(batch, "policy_version"),
            _checksum(batch, "draft_checksum"),
            _checksum(batch, "source_checksum"),
            _required_text(batch, "reviewer_id"),
            _payload_json(batch.get("payload", batch)),
            _required_text(batch, "published_at"),
        )
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO publication_batches(
                    batch_id, draft_id, run_id, requested_date, policy_version,
                    draft_checksum, source_checksum, reviewer_id, payload_json,
                    published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                batch_values,
            )
            play_values = [
                self._authoritative_publication_play_values(
                    connection,
                    batch_id=batch_id,
                    draft_id=_required_text(batch, "draft_id"),
                    batch_policy_version=_required_text(batch, "policy_version"),
                    batch_published_at=_required_text(batch, "published_at"),
                    play=play,
                )
                for play in plays
            ]
            connection.executemany(
                """
                INSERT INTO publication_plays(
                    play_id, batch_id, policy_evaluation_id, prediction_id,
                    event_id, market_key, selection, publication_price,
                    bookmaker_key, market_no_vig_probability,
                    break_even_probability, edge_percentage_points,
                    expected_value_per_unit_risk, confidence_grade,
                    prediction_checksum, evidence_checksum, policy_version,
                    source_checksum, gate_results_json, payload_json, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                play_values,
            )
        result = self.get_publication_batch(batch_id)
        if result is None:
            raise DatabaseInvariantError("Publication batch could not be read back")
        return result

    def _authoritative_publication_play_values(
        self,
        connection: sqlite3.Connection,
        *,
        batch_id: str,
        draft_id: str,
        batch_policy_version: str,
        batch_published_at: str,
        play: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        policy_evaluation_id = _required_text(play, "policy_evaluation_id")
        row = connection.execute(
            """
            SELECT
                policy.policy_evaluation_id,
                policy.prediction_id,
                policy.policy_version,
                policy.payload_json AS policy_payload_json,
                evaluation.event_id,
                evaluation.best_price,
                evaluation.best_price_books_json,
                evaluation.market_no_vig_probability,
                evaluation.break_even_probability,
                evaluation.edge_percentage_points,
                evaluation.expected_value_per_unit_risk,
                prediction.prediction_checksum,
                prediction.evidence_checksum,
                prediction.payload_json AS prediction_payload_json,
                evidence.evidence_json,
                decision.source_checksum
            FROM policy_evaluations AS policy
            JOIN market_evaluations AS evaluation
              ON evaluation.evaluation_id = policy.evaluation_id
            JOIN reviewed_predictions AS prediction
              ON prediction.prediction_id = policy.prediction_id
            JOIN reviewed_prediction_evidence AS evidence
              ON evidence.evidence_id = prediction.evidence_id
            JOIN review_decisions AS decision
              ON decision.policy_evaluation_id = policy.policy_evaluation_id
             AND decision.draft_id = ?
            WHERE policy.policy_evaluation_id=?
              AND policy.outcome='CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed=1
              AND decision.decision='approved'
            """,
            (draft_id, policy_evaluation_id),
        ).fetchone()
        if row is None:
            raise DatabaseInvariantError(
                "Publication play requires an approved persisted candidate evaluation"
            )
        policy_payload = json.loads(str(row["policy_payload_json"]))
        if not isinstance(policy_payload, dict):
            raise DatabaseInvariantError("Policy evaluation payload must be an object")
        selection = str(policy_payload.get("selected_team_key") or "").strip()
        confidence_grade = str(policy_payload.get("confidence_grade") or "").strip()
        if not selection or confidence_grade not in {"A", "B", "C", "D"}:
            raise DatabaseInvariantError(
                "Policy evaluation lacks authoritative selection or confidence"
            )
        best_books_raw = json.loads(str(row["best_price_books_json"]))
        if not isinstance(best_books_raw, list):
            raise DatabaseInvariantError("Best-price bookmakers must be a JSON list")
        best_books = [str(book) for book in best_books_raw]
        bookmaker_key = _required_text(play, "bookmaker_key")
        if bookmaker_key not in best_books:
            raise DatabaseInvariantError(
                "Publication bookmaker must offer the authoritative best price"
            )

        gates = connection.execute(
            """
            SELECT gate_code, threshold_json, observed_json, passed, reason,
                   source_checksum, evaluated_at
            FROM policy_gate_results
            WHERE policy_evaluation_id=? ORDER BY gate_code
            """,
            (policy_evaluation_id,),
        ).fetchall()
        authoritative_gates = [
            {
                "gate_code": str(gate["gate_code"]),
                "threshold": json.loads(str(gate["threshold_json"])),
                "observed": json.loads(str(gate["observed_json"])),
                "passed": bool(gate["passed"]),
                "reason": str(gate["reason"]),
                "source_checksum": str(gate["source_checksum"]),
                "evaluated_at": str(gate["evaluated_at"]),
                "policy_version": str(row["policy_version"]),
            }
            for gate in gates
        ]
        authoritative: dict[str, Any] = {
            "prediction_id": str(row["prediction_id"]),
            "event_id": str(row["event_id"]),
            "market_key": "h2h",
            "selection": selection,
            "publication_price": row["best_price"],
            "bookmaker_key": bookmaker_key,
            "market_no_vig_probability": row["market_no_vig_probability"],
            "break_even_probability": row["break_even_probability"],
            "edge_percentage_points": row["edge_percentage_points"],
            "expected_value_per_unit_risk": row["expected_value_per_unit_risk"],
            "confidence_grade": confidence_grade,
            "prediction_checksum": str(row["prediction_checksum"]),
            "evidence_checksum": str(row["evidence_checksum"]),
            "policy_version": str(row["policy_version"]),
            "source_checksum": str(row["source_checksum"]),
            "gate_results": authoritative_gates,
            "prediction_snapshot": json.loads(str(row["prediction_payload_json"])),
            "evidence_snapshot": json.loads(str(row["evidence_json"])),
            "published_at": batch_published_at,
        }
        if authoritative["policy_version"] != batch_policy_version:
            raise DatabaseInvariantError(
                "Publication policy version does not match the approved evaluation"
            )
        numeric_fields = {
            "publication_price",
            "market_no_vig_probability",
            "break_even_probability",
            "edge_percentage_points",
            "expected_value_per_unit_risk",
        }
        for key, expected in authoritative.items():
            if key == "gate_results" or key not in play:
                continue
            observed = play[key]
            if key in numeric_fields:
                matches = observed is not None and expected is not None and float(observed) == float(expected)
            else:
                matches = str(observed) == str(expected)
            if not matches:
                raise DatabaseInvariantError(
                    f"Publication play {key} does not match authoritative evaluation"
                )
        gate_json = _payload_json(authoritative_gates)
        payload_json = _payload_json(
            {
                "play_id": _required_text(play, "play_id"),
                "policy_evaluation_id": policy_evaluation_id,
                **authoritative,
            }
        )
        return (
            _required_text(play, "play_id"),
            batch_id,
            policy_evaluation_id,
            authoritative["prediction_id"],
            authoritative["event_id"],
            authoritative["market_key"],
            authoritative["selection"],
            authoritative["publication_price"],
            bookmaker_key,
            authoritative["market_no_vig_probability"],
            authoritative["break_even_probability"],
            authoritative["edge_percentage_points"],
            authoritative["expected_value_per_unit_risk"],
            authoritative["confidence_grade"],
            authoritative["prediction_checksum"],
            authoritative["evidence_checksum"],
            authoritative["policy_version"],
            authoritative["source_checksum"],
            gate_json,
            payload_json,
            batch_published_at,
        )

    def get_publication_batch(self, batch_id: str) -> dict[str, Any] | None:
        safe_id = batch_id.strip()
        if not safe_id:
            raise ValueError("batch_id must not be empty")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM publication_batches WHERE batch_id=?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            plays = connection.execute(
                """
                SELECT * FROM publication_plays
                WHERE batch_id=? ORDER BY event_id, play_id
                """,
                (safe_id,),
            ).fetchall()
        result = dict(row)
        result["plays"] = [dict(play) for play in plays]
        return result

    def list_publication_plays(self, batch_id: str) -> list[dict[str, Any]]:
        safe_id = batch_id.strip()
        if not safe_id:
            raise ValueError("batch_id must not be empty")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM publication_plays
                WHERE batch_id=? ORDER BY event_id, play_id
                """,
                (safe_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_settlement_event(self, record: Mapping[str, Any]) -> dict[str, Any]:
        settlement_event_id = _required_text(record, "settlement_event_id")
        event_kind = _required_text(record, "event_kind")
        corrects_event_id = record.get("corrects_event_id")
        if corrects_event_id is not None:
            corrects_event_id = str(corrects_event_id).strip() or None
        values = (
            settlement_event_id,
            _required_text(record, "play_id"),
            event_kind,
            _required_text(record, "result"),
            corrects_event_id,
            _required_text(record, "settled_at"),
            _required_text(record, "recorded_at"),
            _checksum(record, "source_checksum"),
            redact_text(record["notes"]) if record.get("notes") is not None else None,
            _payload_json(record.get("payload", record)),
        )
        with self.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO settlement_events(
                    settlement_event_id, play_id, event_kind, result,
                    corrects_event_id, settled_at, recorded_at, source_checksum,
                    notes, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM settlement_events WHERE settlement_event_id=?",
                (settlement_event_id,),
            ).fetchone()
        result = _row_dict(row)
        if result is None:
            raise DatabaseInvariantError("Settlement event could not be read back")
        return result

    def list_settlement_ledger(
        self, *, play_id: str | None = None, batch_id: str | None = None
    ) -> list[dict[str, Any]]:
        if play_id is not None and batch_id is not None:
            raise ValueError("filter by play_id or batch_id, not both")
        predicate = "1=1"
        parameters: tuple[str, ...] = ()
        if play_id is not None:
            predicate = "settlement.play_id=?"
            parameters = (play_id.strip(),)
        elif batch_id is not None:
            predicate = "play.batch_id=?"
            parameters = (batch_id.strip(),)
        if parameters and not parameters[0]:
            raise ValueError("ledger identifier must not be empty")
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT settlement.*, play.batch_id, play.event_id,
                       play.selection, play.publication_price
                FROM settlement_events AS settlement
                JOIN publication_plays AS play ON play.play_id = settlement.play_id
                WHERE {predicate}
                ORDER BY settlement.recorded_at, settlement.settlement_event_id
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def schema_info(self) -> dict[str, Any]:
        with self.connect() as connection:
            migration = connection.execute(
                """
                SELECT version, name, checksum, applied_at
                FROM schema_migrations ORDER BY version DESC LIMIT 1
                """
            ).fetchone()
            observed_fingerprint = schema_fingerprint(connection)
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        return {
            "version": int(migration["version"]) if migration else None,
            "name": migration["name"] if migration else None,
            "checksum": migration["checksum"] if migration else None,
            "applied_at": migration["applied_at"] if migration else None,
            "user_version": user_version,
            "fingerprint": observed_fingerprint,
            "expected_checksum": MIGRATION_V7_CHECKSUM,
            "expected_fingerprint": FORMAL_SCHEMA_V7_FINGERPRINT,
        }

    def integrity_check(self) -> dict[str, Any]:
        with self.connect() as connection:
            integrity_rows = [
                str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()
            ]
            foreign_key_rows = [
                tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            ]
        return {
            "ok": integrity_rows == ["ok"] and not foreign_key_rows,
            "integrity_check": integrity_rows,
            "foreign_key_violations": foreign_key_rows,
        }

    def verify_integrity(self) -> None:
        result = self.integrity_check()
        if not result["ok"]:
            raise DatabaseInvariantError("SQLite integrity verification failed")
