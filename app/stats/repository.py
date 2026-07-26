from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from app.database import Database, utc_now
from app.redaction import redact_text, redact_value


_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_ENDPOINT_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_RUN_TRANSITIONS = {
    "queued": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "completed_with_warnings", "failed"}),
    "completed": frozenset(),
    "completed_with_warnings": frozenset(),
    "failed": frozenset(),
}
_CHECKPOINT_TRANSITIONS = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"completed", "partial", "failed"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
}
_PRESERVED_DOMAIN_FIELDS = (
    "id",
    "key",
    "source_id",
    "provider_id",
    "team_id",
    "player_id",
    "game_id",
    "play_id",
    "pitch_id",
    "venue_id",
)
DEFAULT_STATS_ADAPTER_VERSION = "DSE_STATS_ACQUISITION_V1"
_REVISION_HISTORY_QUERY_CHUNK_SIZE = 500
_IDENTITY_QUERY_PARAMETER_LIMIT = 500
_FIELDING_STRUCTURAL_STATS_FIELDS = frozenset(
    {"source_row_key", "position_code", "source_stint_key"}
)


class StatsRepositoryError(RuntimeError):
    pass


class StatsInvariantError(StatsRepositoryError):
    pass


class StatsNotFoundError(StatsRepositoryError):
    pass


class InvalidStatsTransition(StatsRepositoryError):
    pass


def _required_text(value: object, field: str) -> str:
    normalized = str(value).strip() if value is not None else ""
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _checksum(value: object, field: str = "checksum") -> str:
    normalized = _required_text(value, field).lower()
    if _CHECKSUM_RE.fullmatch(normalized) is None:
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return normalized


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(_required_text(value, field))
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _binary(value: object, field: str) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int) and value in {0, 1}:
        return value
    raise ValueError(f"{field} must be a boolean or the integer 0 or 1")


def _canonical_json(value: object) -> str:
    return json.dumps(
        redact_value(value, preserve_field_names=_PRESERVED_DOMAIN_FIELDS),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    )


def _normalized_checksum(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: object, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = _required_text(value, field)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _optional_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (datetime, str)):
        raise TypeError(f"{field} must be a datetime, string, or null")
    return _timestamp(value, field)


def _calendar_date(value: object, field: str) -> str:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must be a calendar date, not a timestamp")
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a date or YYYY-MM-DD string")
    raw = _required_text(value, field)
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise ValueError(f"{field} must be an exact YYYY-MM-DD calendar date")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid calendar date") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{field} must be an exact YYYY-MM-DD calendar date")
    return raw


def _optional_date(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, (date, str)) or isinstance(value, datetime):
        raise TypeError(f"{field} must be a date, string, or null")
    return _calendar_date(value, field)


def _artifact_relpath(value: object) -> str:
    normalized = _required_text(value, "artifact_relpath").replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts) or ":" in path.parts[0]:
        raise ValueError("artifact_relpath must be a contained relative path")
    return path.as_posix()


def _endpoint_category(value: object) -> str:
    normalized = _required_text(value, "endpoint_category").lower()
    if _ENDPOINT_CATEGORY_RE.fullmatch(normalized) is None:
        raise ValueError("endpoint_category must be an allowlisted category, not a URL")
    return normalized


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _same_value(observed: object, expected: object) -> bool:
    if isinstance(observed, float) and isinstance(expected, int | float):
        return observed == float(expected)
    return observed == expected


def _insert_once(
    connection: sqlite3.Connection,
    table: str,
    values: Mapping[str, object],
    identity: Mapping[str, object],
) -> dict[str, Any]:
    predicate_parts: list[str] = []
    predicate_values: list[object] = []
    for column, value in identity.items():
        if value is None:
            predicate_parts.append(f"{column} IS NULL")
        else:
            predicate_parts.append(f"{column}=?")
            predicate_values.append(value)
    predicate = " AND ".join(predicate_parts)
    existing = connection.execute(
        f"SELECT * FROM {table} WHERE {predicate}", tuple(predicate_values)
    ).fetchone()
    if existing is not None:
        mismatches = [column for column, expected in values.items() if not _same_value(existing[column], expected)]
        if mismatches:
            raise StatsInvariantError(
                f"{table} identity already exists with different fields: " + ", ".join(sorted(mismatches))
            )
        return dict(existing)

    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
        tuple(values[column] for column in columns),
    )
    created = connection.execute(
        f"SELECT * FROM {table} WHERE {predicate}", tuple(predicate_values)
    ).fetchone()
    if created is None:
        raise StatsInvariantError(f"{table} row could not be read after insert")
    return dict(created)


def _mapping(record: Mapping[str, object], key: str, default: object = None) -> object:
    return record[key] if key in record else default


class StatsRepository:
    """Transactional persistence for versioned MLB statistics source evidence."""

    def __init__(self, database: Database):
        self.database = database

    def create_ingestion_run(
        self,
        *,
        stats_run_id: str,
        run_id: str,
        provider: str,
        scope_key: str,
        requested_through_date: date | str,
        configuration_checksum: str,
        source_version: str | None = None,
        adapter_version: str = DEFAULT_STATS_ADAPTER_VERSION,
        created_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        timestamp = _timestamp(created_at or utc_now(), "created_at")
        safe_provider = _required_text(provider, "provider").lower()
        safe_requested_date = _calendar_date(
            requested_through_date, "requested_through_date"
        )
        values: dict[str, object] = {
            "stats_run_id": _required_text(stats_run_id, "stats_run_id"),
            "run_id": _required_text(run_id, "run_id"),
            "provider": safe_provider,
            "source_version": _required_text(
                source_version or f"{safe_provider}:{safe_requested_date[:4]}",
                "source_version",
            ),
            "adapter_version": _required_text(adapter_version, "adapter_version"),
            "scope_key": _required_text(scope_key, "scope_key"),
            "requested_through_date": safe_requested_date,
            "source_observed_at": None,
            "latest_ingested_completed_game_date": None,
            "contiguous_regular_season_complete_through_date": None,
            "partial_date": None,
            "partial_date_reason": None,
            "status": "queued",
            "created_at": timestamp,
            "started_at": None,
            "completed_at": None,
            "updated_at": timestamp,
            "failure_stage": None,
            "error_json": None,
            "configuration_checksum": _checksum(configuration_checksum, "configuration_checksum"),
        }
        with self.database.connect(write=True) as connection:
            return _insert_once(
                connection,
                "stats_ingestion_runs",
                values,
                {"stats_run_id": values["stats_run_id"]},
            )

    def transition_ingestion_run(
        self,
        stats_run_id: str,
        status: str,
        *,
        transitioned_at: datetime | str | None = None,
        failure_stage: str | None = None,
        error: object | None = None,
        source_observed_at: datetime | str | None = None,
        latest_ingested_completed_game_date: date | str | None = None,
        contiguous_regular_season_complete_through_date: date | str | None = None,
        partial_date: date | str | None = None,
        partial_date_reason: str | None = None,
    ) -> dict[str, Any]:
        target = _required_text(status, "status")
        timestamp = _timestamp(transitioned_at or utc_now(), "transitioned_at")
        safe_partial_date = _optional_date(partial_date, "partial_date")
        safe_partial_reason = _optional_text(partial_date_reason)
        if (safe_partial_date is None) != (safe_partial_reason is None):
            raise ValueError("partial_date and partial_date_reason must be supplied together")
        with self.database.connect(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (_required_text(stats_run_id, "stats_run_id"),),
            ).fetchone()
            if current is None:
                raise StatsNotFoundError(f"Stats ingestion run not found: {stats_run_id}")
            current_status = str(current["status"])
            if target == current_status:
                return dict(current)
            if target not in _RUN_TRANSITIONS[current_status]:
                raise InvalidStatsTransition(f"Invalid stats ingestion transition: {current_status} -> {target}")
            safe_failure_stage = _optional_text(failure_stage)
            if target == "failed" and safe_failure_stage is None:
                raise ValueError("failure_stage is required when a stats run fails")
            if target != "failed" and (safe_failure_stage is not None or error is not None):
                raise ValueError("failure details are permitted only for failed runs")
            started_at = current["started_at"]
            completed_at = current["completed_at"]
            if target == "running":
                started_at = timestamp
            if target in {"completed", "completed_with_warnings", "failed"}:
                completed_at = current["completed_at"] or timestamp
            safe_source_observed_at = (
                _optional_timestamp(source_observed_at, "source_observed_at") or current["source_observed_at"]
            )
            if target in {"completed", "completed_with_warnings"} and safe_source_observed_at is None:
                raise ValueError("source_observed_at is required when a stats run completes")
            values = {
                "status": target,
                "started_at": started_at,
                "completed_at": completed_at,
                "updated_at": timestamp,
                "failure_stage": safe_failure_stage,
                "error_json": _canonical_json(error) if error is not None else None,
                "source_observed_at": safe_source_observed_at,
                "latest_ingested_completed_game_date": _optional_date(
                    latest_ingested_completed_game_date,
                    "latest_ingested_completed_game_date",
                )
                or current["latest_ingested_completed_game_date"],
                "contiguous_regular_season_complete_through_date": _optional_date(
                    contiguous_regular_season_complete_through_date,
                    "contiguous_regular_season_complete_through_date",
                )
                or current["contiguous_regular_season_complete_through_date"],
                "partial_date": safe_partial_date or current["partial_date"],
                "partial_date_reason": safe_partial_reason or current["partial_date_reason"],
            }
            assignments = ",".join(f"{column}=?" for column in values)
            connection.execute(
                f"UPDATE stats_ingestion_runs SET {assignments} WHERE stats_run_id=?",
                (*values.values(), current["stats_run_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (current["stats_run_id"],),
            ).fetchone()
        if updated is None:
            raise StatsInvariantError("Stats ingestion transition disappeared")
        return dict(updated)

    def create_checkpoint(self, record: Mapping[str, object]) -> dict[str, Any]:
        timestamp = _timestamp(
            _mapping(record, "created_at", utc_now()),
            "created_at",
        )
        values: dict[str, object] = {
            "checkpoint_id": _required_text(record.get("checkpoint_id"), "checkpoint_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "dataset_key": _required_text(record.get("dataset_key"), "dataset_key"),
            "scope_key": _required_text(record.get("scope_key"), "scope_key"),
            "status": "pending",
            "cursor_before_json": (
                _canonical_json(record["cursor_before"]) if record.get("cursor_before") is not None else None
            ),
            "cursor_after_json": None,
            "observed_through": None,
            "complete_through": None,
            "records_seen": 0,
            "records_persisted": 0,
            "records_rejected": 0,
            "created_at": timestamp,
            "started_at": None,
            "completed_at": None,
            "updated_at": timestamp,
            "error_json": None,
        }
        with self.database.connect(write=True) as connection:
            return _insert_once(
                connection,
                "stats_checkpoints",
                values,
                {"checkpoint_id": values["checkpoint_id"]},
            )

    def transition_checkpoint(
        self,
        checkpoint_id: str,
        status: str,
        *,
        transitioned_at: datetime | str | None = None,
        cursor_after: object | None = None,
        observed_through: datetime | str | None = None,
        complete_through: datetime | str | None = None,
        records_seen: int | None = None,
        records_persisted: int | None = None,
        records_rejected: int | None = None,
        error: object | None = None,
    ) -> dict[str, Any]:
        target = _required_text(status, "status")
        timestamp = _timestamp(transitioned_at or utc_now(), "transitioned_at")
        with self.database.connect(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM stats_checkpoints WHERE checkpoint_id=?",
                (_required_text(checkpoint_id, "checkpoint_id"),),
            ).fetchone()
            if current is None:
                raise StatsNotFoundError(f"Stats checkpoint not found: {checkpoint_id}")
            current_status = str(current["status"])
            if target == current_status:
                return dict(current)
            if target not in _CHECKPOINT_TRANSITIONS[current_status]:
                raise InvalidStatsTransition(f"Invalid stats checkpoint transition: {current_status} -> {target}")
            if error is not None and target != "failed":
                raise ValueError("checkpoint error is permitted only for a failed checkpoint")
            counts = {
                "records_seen": current["records_seen"] if records_seen is None else records_seen,
                "records_persisted": (current["records_persisted"] if records_persisted is None else records_persisted),
                "records_rejected": (current["records_rejected"] if records_rejected is None else records_rejected),
            }
            if any(not isinstance(value, int) or value < 0 for value in counts.values()):
                raise ValueError("checkpoint record counts must be non-negative integers")
            values: dict[str, object] = {
                "status": target,
                "cursor_after_json": (
                    _canonical_json(cursor_after) if cursor_after is not None else current["cursor_after_json"]
                ),
                "observed_through": _optional_timestamp(observed_through, "observed_through")
                or current["observed_through"],
                "complete_through": _optional_timestamp(complete_through, "complete_through")
                or current["complete_through"],
                **counts,
                "started_at": current["started_at"] or timestamp,
                "completed_at": (timestamp if target in {"completed", "partial", "failed"} else None),
                "updated_at": timestamp,
                "error_json": _canonical_json(error) if error is not None else None,
            }
            assignments = ",".join(f"{column}=?" for column in values)
            connection.execute(
                f"UPDATE stats_checkpoints SET {assignments} WHERE checkpoint_id=?",
                (*values.values(), current["checkpoint_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM stats_checkpoints WHERE checkpoint_id=?",
                (current["checkpoint_id"],),
            ).fetchone()
        if updated is None:
            raise StatsInvariantError("Stats checkpoint transition disappeared")
        return dict(updated)

    def fail_active_checkpoints(
        self,
        stats_run_id: str,
        *,
        transitioned_at: datetime | str | None = None,
        reason_code: str,
        details: object | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Fail pending/running checkpoints before their owning run terminates."""

        safe_stats_run_id = _required_text(stats_run_id, "stats_run_id")
        timestamp = _timestamp(transitioned_at or utc_now(), "transitioned_at")
        error: dict[str, object] = {
            "code": _required_text(reason_code, "reason_code"),
        }
        if details is not None:
            error["details"] = redact_value(details)
        error_json = _canonical_json(error)
        updated: list[dict[str, Any]] = []
        with self.database.connect(write=True) as connection:
            owner = connection.execute(
                "SELECT stats_run_id FROM stats_ingestion_runs WHERE stats_run_id=?",
                (safe_stats_run_id,),
            ).fetchone()
            if owner is None:
                raise StatsNotFoundError(
                    f"Stats ingestion run not found: {safe_stats_run_id}"
                )
            rows = connection.execute(
                "SELECT * FROM stats_checkpoints "
                "WHERE stats_run_id=? AND status IN ('pending','running') "
                "ORDER BY created_at,checkpoint_id",
                (safe_stats_run_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE stats_checkpoints SET status='failed',"
                    "started_at=COALESCE(started_at,?),"
                    "completed_at=COALESCE(completed_at,?),updated_at=?,error_json=? "
                    "WHERE checkpoint_id=? AND status IN ('pending','running')",
                    (
                        timestamp,
                        timestamp,
                        timestamp,
                        error_json,
                        row["checkpoint_id"],
                    ),
                )
                current = connection.execute(
                    "SELECT * FROM stats_checkpoints WHERE checkpoint_id=?",
                    (row["checkpoint_id"],),
                ).fetchone()
                if current is None:
                    raise StatsInvariantError(
                        "Stats checkpoint terminalization disappeared"
                    )
                updated.append(dict(current))
        return tuple(updated)

    def reconcile_terminal_run_checkpoints(
        self,
        *,
        transitioned_at: datetime | str | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Repair legacy active checkpoints whose owning run is terminal."""

        timestamp = _timestamp(transitioned_at or utc_now(), "transitioned_at")
        updated: list[dict[str, Any]] = []
        with self.database.connect(write=True) as connection:
            rows = connection.execute(
                "SELECT checkpoint.*,run.status AS parent_status "
                "FROM stats_checkpoints AS checkpoint "
                "JOIN stats_ingestion_runs AS run "
                "ON run.stats_run_id=checkpoint.stats_run_id "
                "WHERE checkpoint.status IN ('pending','running') "
                "AND run.status IN ('completed','completed_with_warnings','failed') "
                "ORDER BY checkpoint.created_at,checkpoint.checkpoint_id"
            ).fetchall()
            for row in rows:
                error_json = _canonical_json(
                    {
                        "code": "terminal_parent_run_reconciliation",
                        "details": {"parent_status": str(row["parent_status"])},
                    }
                )
                connection.execute(
                    "UPDATE stats_checkpoints SET status='failed',"
                    "started_at=COALESCE(started_at,?),"
                    "completed_at=COALESCE(completed_at,?),updated_at=?,error_json=? "
                    "WHERE checkpoint_id=? AND status IN ('pending','running')",
                    (
                        timestamp,
                        timestamp,
                        timestamp,
                        error_json,
                        row["checkpoint_id"],
                    ),
                )
                current = connection.execute(
                    "SELECT * FROM stats_checkpoints WHERE checkpoint_id=?",
                    (row["checkpoint_id"],),
                ).fetchone()
                if current is None:
                    raise StatsInvariantError(
                        "Reconciled stats checkpoint disappeared"
                    )
                updated.append(dict(current))
        return tuple(updated)

    def record_raw_payload_metadata(self, record: Mapping[str, object]) -> dict[str, Any]:
        values: dict[str, object] = {
            "raw_payload_id": _required_text(record.get("raw_payload_id"), "raw_payload_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "checkpoint_id": _optional_text(record.get("checkpoint_id")),
            "phase1_raw_payload_id": record.get("phase1_raw_payload_id"),
            "provider": _required_text(record.get("provider"), "provider").lower(),
            "endpoint_category": _endpoint_category(record.get("endpoint_category")),
            "source_capture_id": _required_text(record.get("source_capture_id"), "source_capture_id"),
            "provider_record_id": _optional_text(record.get("provider_record_id")),
            "provider_updated_at": _optional_timestamp(record.get("provider_updated_at"), "provider_updated_at"),
            "retrieved_at": _timestamp(record.get("retrieved_at"), "retrieved_at"),
            "content_type": _required_text(record.get("content_type"), "content_type"),
            "checksum_sha256": _checksum(record.get("checksum_sha256"), "checksum_sha256"),
            "artifact_relpath": _artifact_relpath(record.get("artifact_relpath")),
            "metadata_json": _canonical_json(record.get("metadata", {})),
        }
        with self.database.connect(write=True) as connection:
            return _insert_once(
                connection,
                "stats_raw_payload_metadata",
                values,
                {"raw_payload_id": values["raw_payload_id"]},
            )

    def get_raw_payload_metadata(self, raw_payload_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            return _row(
                connection.execute(
                    "SELECT * FROM stats_raw_payload_metadata WHERE raw_payload_id=?",
                    (_required_text(raw_payload_id, "raw_payload_id"),),
                ).fetchone()
            )

    def record_excluded_source_row(
        self, record: Mapping[str, object]
    ) -> dict[str, Any]:
        return self.record_excluded_source_rows((record,))[0]

    def record_excluded_source_rows(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._excluded_source_row_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            results: list[dict[str, Any]] = []
            for values in prepared:
                natural_columns = (
                    "raw_payload_id",
                    "provider",
                    "dataset_key",
                    "source_row_id",
                    "source_row_checksum",
                    "classification",
                    "reason_code",
                )
                predicate = " AND ".join(
                    f"{column}=?" for column in natural_columns
                )
                existing = connection.execute(
                    "SELECT * FROM stats_excluded_source_rows WHERE " + predicate,
                    tuple(values[column] for column in natural_columns),
                ).fetchone()
                if existing is not None:
                    results.append(dict(existing))
                    continue
                results.append(
                    _insert_once(
                        connection,
                        "stats_excluded_source_rows",
                        values,
                        {"excluded_row_id": values["excluded_row_id"]},
                    )
                )
            return tuple(results)

    @staticmethod
    def _excluded_source_row_values(
        record: Mapping[str, object]
    ) -> dict[str, object]:
        return {
            "excluded_row_id": _required_text(
                record.get("excluded_row_id"), "excluded_row_id"
            ),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "raw_payload_id": _required_text(
                record.get("raw_payload_id"), "raw_payload_id"
            ),
            "provider": _required_text(record.get("provider"), "provider").lower(),
            "dataset_key": _required_text(record.get("dataset_key"), "dataset_key"),
            "source_row_id": _required_text(
                record.get("source_row_id"), "source_row_id"
            ),
            "classification": _required_text(
                record.get("classification"), "classification"
            ),
            "reason_code": _required_text(record.get("reason_code"), "reason_code"),
            "source_effective_date": _optional_date(
                record.get("source_effective_date"), "source_effective_date"
            ),
            "observed_at": _timestamp(record.get("observed_at"), "observed_at"),
            "source_row_checksum": _checksum(
                record.get("source_row_checksum"), "source_row_checksum"
            ),
            "details_json": _canonical_json(record.get("details", {})),
        }

    def upsert_team_identity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.upsert_team_identities((record,))[0]

    def upsert_team_identities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._identity_values("team", record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._upsert_identity_on_connection(
                    connection, "stats_team_identities", "team", item
                )
                for item in values
            )

    def get_team_identity(
        self, provider: str, provider_team_id: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stats_team_identities "
                "WHERE provider=? AND provider_team_id=?",
                (
                    _required_text(provider, "provider").lower(),
                    _required_text(provider_team_id, "provider_team_id"),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_player_identity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.upsert_player_identities((record,))[0]

    def upsert_player_identities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._identity_values("player", record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._upsert_identity_on_connection(
                    connection, "stats_player_identities", "player", item
                )
                for item in values
            )

    def record_canonical_player(
        self, record: Mapping[str, object]
    ) -> dict[str, Any]:
        return self.record_canonical_players((record,))[0]

    def get_canonical_player(self, canonical_player_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stats_canonical_players "
                "WHERE canonical_player_id=?",
                (_required_text(canonical_player_id, "canonical_player_id"),),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_player_identity(
        self, provider: str, provider_player_id: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stats_player_identities "
                "WHERE provider=? AND provider_player_id=?",
                (
                    _required_text(provider, "provider").lower(),
                    _required_text(provider_player_id, "provider_player_id"),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_canonical_players(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._canonical_player_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                _insert_once(
                    connection,
                    "stats_canonical_players",
                    values,
                    {"canonical_player_id": values["canonical_player_id"]},
                )
                for values in prepared
            )

    def get_game_identity(
        self, provider: str, provider_game_id: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            return _row(
                connection.execute(
                    "SELECT * FROM stats_game_identities "
                    "WHERE provider=? AND provider_game_id=?",
                    (
                        _required_text(provider, "provider").lower(),
                        _required_text(provider_game_id, "provider_game_id"),
                    ),
                ).fetchone()
            )

    @staticmethod
    def _canonical_player_values(
        record: Mapping[str, object]
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "canonical_player_id": _required_text(
                record.get("canonical_player_id"), "canonical_player_id"
            ),
            "created_stats_run_id": _required_text(
                record.get("created_stats_run_id"), "created_stats_run_id"
            ),
            "display_name": _required_text(record.get("display_name"), "display_name"),
            "birth_date": _optional_date(record.get("birth_date"), "birth_date"),
            "created_at": _timestamp(record.get("created_at"), "created_at"),
            "canonical_checksum": _checksum(
                record.get("canonical_checksum"), "canonical_checksum"
            ),
        }
        return values

    def record_player_identifier_mapping(
        self, record: Mapping[str, object]
    ) -> dict[str, Any]:
        return self.record_player_identifier_mappings((record,))[0]

    def record_player_identifier_mappings(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._player_mapping_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_player_mapping_on_connection(connection, values)
                for values in prepared
            )

    @staticmethod
    def _player_mapping_values(
        record: Mapping[str, object]
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "mapping_id": _required_text(record.get("mapping_id"), "mapping_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "canonical_player_id": _required_text(
                record.get("canonical_player_id"), "canonical_player_id"
            ),
            "player_identity_id": _required_text(
                record.get("player_identity_id"), "player_identity_id"
            ),
            "mapping_method": _required_text(
                record.get("mapping_method"), "mapping_method"
            ),
            "verification_status": _required_text(
                record.get("verification_status"), "verification_status"
            ),
            "source_version": _required_text(
                record.get("source_version"), "source_version"
            ),
            "adapter_version": _required_text(
                record.get("adapter_version"), "adapter_version"
            ),
            "observed_at": _timestamp(record.get("observed_at"), "observed_at"),
            "provenance_json": _canonical_json(record.get("provenance", {})),
            "source_checksum": _checksum(
                record.get("source_checksum"), "source_checksum"
            ),
        }
        return values

    @staticmethod
    def _record_player_mapping_on_connection(
        connection: sqlite3.Connection, values: Mapping[str, object]
    ) -> dict[str, Any]:
        natural_key: dict[str, object] = {
            "player_identity_id": values["player_identity_id"],
            "canonical_player_id": values["canonical_player_id"],
            "source_checksum": values["source_checksum"],
        }
        predicate = " AND ".join(f"{column}=?" for column in natural_key)
        existing = connection.execute(
            "SELECT * FROM stats_player_identifier_mappings WHERE " + predicate,
            tuple(natural_key.values()),
        ).fetchone()
        if existing is not None:
            ignored = {"mapping_id", "stats_run_id", "observed_at"}
            mismatches = [
                column
                for column, expected in values.items()
                if column not in ignored
                and not _same_value(existing[column], expected)
            ]
            if mismatches:
                raise StatsInvariantError(
                    "player mapping source checksum changed preserved provenance: "
                    + ", ".join(sorted(mismatches))
                )
            return dict(existing)
        return _insert_once(
            connection,
            "stats_player_identifier_mappings",
            values,
            {"mapping_id": values["mapping_id"]},
        )

    def load_player_crosswalk(
        self, canonical_player_id: str
    ) -> tuple[dict[str, Any], ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT mapping.*, identity.provider, identity.provider_player_id,
                       identity.full_name
                FROM stats_player_identifier_mappings AS mapping
                JOIN stats_player_identities AS identity
                  ON identity.player_identity_id = mapping.player_identity_id
                WHERE mapping.canonical_player_id = ?
                ORDER BY identity.provider, identity.provider_player_id,
                         mapping.observed_at, mapping.mapping_id
                """,
                (_required_text(canonical_player_id, "canonical_player_id"),),
            ).fetchall()
        decoded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["provenance"] = json.loads(str(item.pop("provenance_json")))
            decoded.append(item)
        return tuple(decoded)

    def resolve_canonical_player(
        self, provider: str, provider_player_id: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT canonical.*
                FROM stats_player_identities AS identity
                JOIN stats_player_identifier_mappings AS mapping
                  ON mapping.player_identity_id = identity.player_identity_id
                JOIN stats_canonical_players AS canonical
                  ON canonical.canonical_player_id = mapping.canonical_player_id
                WHERE identity.provider = ?
                  AND identity.provider_player_id = ?
                  AND mapping.verification_status = 'verified'
                ORDER BY canonical.canonical_player_id
                """,
                (
                    _required_text(provider, "provider").lower(),
                    _required_text(provider_player_id, "provider_player_id"),
                ),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise StatsInvariantError(
                "provider player identity has conflicting verified canonical mappings"
            )
        return dict(rows[0])

    def list_verified_canonical_player_mappings(
        self, provider: str
    ) -> dict[str, dict[str, Any]]:
        """Load one verified canonical mapping per player for a provider."""

        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT identity.provider_player_id,canonical.*
                FROM stats_player_identities AS identity
                JOIN stats_player_identifier_mappings AS mapping
                  ON mapping.player_identity_id=identity.player_identity_id
                JOIN stats_canonical_players AS canonical
                  ON canonical.canonical_player_id=mapping.canonical_player_id
                WHERE identity.provider=?
                  AND mapping.verification_status='verified'
                ORDER BY identity.provider_player_id,
                         canonical.canonical_player_id
                """,
                (_required_text(provider, "provider").lower(),),
            ).fetchall()
        results: dict[str, dict[str, Any]] = {}
        for row in rows:
            values = dict(row)
            source_id = str(values.pop("provider_player_id"))
            existing = results.get(source_id)
            if existing is not None and (
                existing["canonical_player_id"] != values["canonical_player_id"]
            ):
                raise StatsInvariantError(
                    "provider player identity has conflicting verified canonical mappings"
                )
            results[source_id] = values
        return results

    def _identity_values(
        self, kind: str, record: Mapping[str, object]
    ) -> dict[str, object]:
        id_column = f"{kind}_identity_id"
        provider_id_column = f"provider_{kind}_id"
        values: dict[str, object] = {
            id_column: _required_text(record.get(id_column), id_column),
            "provider": _required_text(record.get("provider"), "provider").lower(),
            provider_id_column: _required_text(record.get(provider_id_column), provider_id_column),
        }
        if kind == "team":
            values.update(
                {
                    "canonical_team_key": _optional_text(record.get("canonical_team_key")),
                    "current_name": _required_text(record.get("current_name"), "current_name"),
                }
            )
        else:
            values.update(
                {
                    "full_name": _required_text(record.get("full_name"), "full_name"),
                    "primary_position": _optional_text(record.get("primary_position")),
                    "bats": _optional_text(record.get("bats")),
                    "throws": _optional_text(record.get("throws")),
                }
            )
        values.update(
            {
                "active": _binary(record.get("active", True), "active"),
                "first_seen_at": _timestamp(record.get("first_seen_at"), "first_seen_at"),
                "last_seen_at": _timestamp(record.get("last_seen_at"), "last_seen_at"),
                "identity_checksum": _checksum(record.get("identity_checksum"), "identity_checksum"),
            }
        )
        return values

    @staticmethod
    def _upsert_identity_on_connection(
        connection: sqlite3.Connection,
        table: str,
        kind: str,
        values: Mapping[str, object],
    ) -> dict[str, Any]:
        id_column = f"{kind}_identity_id"
        provider_id_column = f"provider_{kind}_id"
        existing = connection.execute(
            f"SELECT * FROM {table} WHERE provider=? AND {provider_id_column}=?",
            (values["provider"], values[provider_id_column]),
        ).fetchone()
        if existing is None:
            if str(values["last_seen_at"]) < str(values["first_seen_at"]):
                raise ValueError(
                    f"{kind} identity last_seen_at must not precede first_seen_at"
                )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        else:
            if existing[id_column] != values[id_column]:
                raise StatsInvariantError(
                    f"{kind} provider identity is already bound to a different ID"
                )
            if str(values["last_seen_at"]) < str(existing["last_seen_at"]):
                raise StatsInvariantError(f"{kind} identity last_seen_at cannot regress")
            factual_columns = set(values) - {
                id_column,
                "provider",
                provider_id_column,
                "first_seen_at",
                "last_seen_at",
            }
            mismatches = [
                column
                for column in factual_columns
                if not _same_value(existing[column], values[column])
            ]
            if mismatches:
                raise StatsInvariantError(
                    f"{kind} identity conflict would overwrite preserved fields: "
                    + ", ".join(sorted(mismatches))
                )
            if existing["last_seen_at"] != values["last_seen_at"]:
                connection.execute(
                    f"UPDATE {table} SET last_seen_at=? WHERE {id_column}=?",
                    (values["last_seen_at"], values[id_column]),
                )
        result = connection.execute(
            f"SELECT * FROM {table} WHERE {id_column}=?", (values[id_column],)
        ).fetchone()
        if result is None:
            raise StatsInvariantError(f"{kind} identity could not be read after upsert")
        return dict(result)

    def upsert_game_identity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.upsert_game_identities((record,))[0]

    def upsert_game_identities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._game_identity_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._upsert_game_identity_on_connection(connection, item)
                for item in values
            )

    @staticmethod
    def _game_identity_values(record: Mapping[str, object]) -> dict[str, object]:
        values: dict[str, object] = {
            "game_identity_id": _required_text(record.get("game_identity_id"), "game_identity_id"),
            "provider": _required_text(record.get("provider"), "provider").lower(),
            "provider_game_id": _required_text(record.get("provider_game_id"), "provider_game_id"),
            "event_id": _optional_text(record.get("event_id")),
            "season": _integer(record.get("season"), "season"),
            "game_type": _required_text(record.get("game_type"), "game_type"),
            "official_date": _calendar_date(record.get("official_date"), "official_date"),
            "scheduled_start": _optional_timestamp(record.get("scheduled_start"), "scheduled_start"),
            "home_team_identity_id": _required_text(record.get("home_team_identity_id"), "home_team_identity_id"),
            "away_team_identity_id": _required_text(record.get("away_team_identity_id"), "away_team_identity_id"),
            "venue_provider_id": _optional_text(record.get("venue_provider_id")),
            "venue_name": _optional_text(record.get("venue_name")),
            "first_seen_at": _timestamp(record.get("first_seen_at"), "first_seen_at"),
            "last_seen_at": _timestamp(record.get("last_seen_at"), "last_seen_at"),
            "identity_checksum": _checksum(record.get("identity_checksum"), "identity_checksum"),
        }
        return values

    @staticmethod
    def _upsert_game_identity_on_connection(
        connection: sqlite3.Connection, values: Mapping[str, object]
    ) -> dict[str, Any]:
        existing = connection.execute(
            "SELECT * FROM stats_game_identities WHERE provider=? AND provider_game_id=?",
            (values["provider"], values["provider_game_id"]),
        ).fetchone()
        if existing is None:
            if str(values["last_seen_at"]) < str(values["first_seen_at"]):
                raise ValueError(
                    "game identity last_seen_at must not precede first_seen_at"
                )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO stats_game_identities({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        else:
            if existing["game_identity_id"] != values["game_identity_id"]:
                raise StatsInvariantError(
                    "provider game identity is already bound to a different ID"
                )
            if str(values["last_seen_at"]) < str(existing["last_seen_at"]):
                raise StatsInvariantError("game identity last_seen_at cannot regress")
            factual_columns = set(values) - {
                "game_identity_id",
                "provider",
                "provider_game_id",
                "first_seen_at",
                "last_seen_at",
            }
            mismatches = [
                column
                for column in factual_columns
                if not _same_value(existing[column], values[column])
            ]
            if mismatches:
                raise StatsInvariantError(
                    "game identity conflict would overwrite preserved fields: "
                    + ", ".join(sorted(mismatches))
                )
            if existing["last_seen_at"] != values["last_seen_at"]:
                connection.execute(
                    "UPDATE stats_game_identities SET last_seen_at=? "
                    "WHERE game_identity_id=?",
                    (values["last_seen_at"], values["game_identity_id"]),
                )
        result = connection.execute(
            "SELECT * FROM stats_game_identities WHERE game_identity_id=?",
            (values["game_identity_id"],),
        ).fetchone()
        if result is None:
            raise StatsInvariantError("game identity could not be read after upsert")
        return dict(result)

    def record_game_status(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_game_statuses((record,))[0]

    def record_game_statuses(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._game_status_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_versioned_snapshot_on_connection(
                    connection,
                    table="stats_game_status_observations",
                    id_column="status_observation_id",
                    logical_columns=("game_identity_id",),
                    values=item,
                )
                for item in values
            )

    def has_game_status_evidence(self, record: Mapping[str, object]) -> bool:
        """Return whether equivalent immutable normalized evidence already exists."""

        values = self._game_status_values(record)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stats_game_status_observations "
                "WHERE game_identity_id=?",
                (values["game_identity_id"],),
            ).fetchall()
        expected_status = json.loads(str(values["status_json"]))
        core_status_keys = (
            "status",
            "status_reason",
            "home_score",
            "away_score",
        )
        for row in rows:
            if row["normalized_checksum"] == values["normalized_checksum"]:
                return True
            if row["source_checksum"] != values["source_checksum"]:
                continue
            if any(
                not _same_value(row[column], values[column])
                for column in (
                    "abstract_state",
                    "detailed_state",
                    "status_code",
                    "scheduled_start",
                    "provider_updated_at",
                )
            ):
                continue
            try:
                observed_status = json.loads(str(row["status_json"]))
            except json.JSONDecodeError:
                continue
            if all(
                observed_status.get(key) == expected_status.get(key)
                for key in core_status_keys
            ):
                return True
        return False

    def _game_status_values(self, record: Mapping[str, object]) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "game_identity_id": _required_text(record.get("game_identity_id"), "game_identity_id"),
                "abstract_state": _optional_text(record.get("abstract_state")),
                "detailed_state": _optional_text(record.get("detailed_state")),
                "status_code": _optional_text(record.get("status_code")),
                "scheduled_start": _optional_timestamp(record.get("scheduled_start"), "scheduled_start"),
                "status_json": _canonical_json(record.get("status", {})),
            }
        )
        values["normalized_checksum"] = _normalized_checksum(
            {
                key: values[key]
                for key in (
                    "game_identity_id",
                    "abstract_state",
                    "detailed_state",
                    "status_code",
                    "scheduled_start",
                    "provider_updated_at",
                    "status_json",
                )
            }
        )
        return values

    def record_game_team_snapshot(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_game_team_snapshots((record,))[0]

    def record_game_team_snapshots(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._game_team_snapshot_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_versioned_snapshot_on_connection(
                    connection,
                    table="stats_game_team_snapshots",
                    id_column="team_snapshot_id",
                    logical_columns=(
                        "game_identity_id",
                        "team_identity_id",
                        "side",
                        "snapshot_kind",
                    ),
                    values=item,
                )
                for item in values
            )

    def _game_team_snapshot_values(
        self, record: Mapping[str, object]
    ) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "game_identity_id": _required_text(record.get("game_identity_id"), "game_identity_id"),
                "team_identity_id": _required_text(record.get("team_identity_id"), "team_identity_id"),
                "side": _required_text(record.get("side"), "side"),
                "snapshot_kind": _required_text(record.get("snapshot_kind"), "snapshot_kind"),
                "stats_json": _canonical_json(record.get("stats", {})),
            }
        )
        values["normalized_checksum"] = _normalized_checksum(
            {
                key: values[key]
                for key in (
                    "game_identity_id",
                    "team_identity_id",
                    "side",
                    "snapshot_kind",
                    "provider_updated_at",
                    "stats_json",
                )
            }
        )
        return values

    def record_game_player_snapshot(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_game_player_snapshots((record,))[0]

    def record_game_player_snapshots(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._game_player_snapshot_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_versioned_snapshot_on_connection(
                    connection,
                    table="stats_game_player_snapshots",
                    id_column="player_snapshot_id",
                    logical_columns=(
                        "game_identity_id",
                        "team_identity_id",
                        "player_identity_id",
                        "role",
                        "source_row_key",
                    ),
                    values=item,
                )
                for item in values
            )

    def _game_player_snapshot_values(
        self, record: Mapping[str, object]
    ) -> dict[str, object]:
        values = self._snapshot_base(record)
        role = _required_text(record.get("role"), "role")
        stats_payload = record.get("stats", {})
        if not isinstance(stats_payload, Mapping):
            raise TypeError("stats must be a mapping")

        raw_position = record.get("position_code")
        position_code = (
            str(raw_position).strip()
            if raw_position is not None and str(raw_position).strip()
            else None
        )
        if role == "fielding" and position_code is None:
            raw_values = stats_payload.get("values")
            if isinstance(raw_values, Mapping):
                raw_position = raw_values.get("d_pos")
                position_code = (
                    str(raw_position).strip()
                    if raw_position is not None and str(raw_position).strip()
                    else None
                )
        if role == "fielding" and position_code is None:
            raise ValueError("fielding snapshots require position_code")
        if role != "fielding" and position_code is not None:
            raise ValueError("position_code is valid only for fielding snapshots")

        raw_stint = record.get("source_stint_key", "000001")
        source_stint_key = _required_text(raw_stint, "source_stint_key")
        expected_source_row_key = f"{role}:{position_code or '_'}:{source_stint_key}"
        source_row_key = _required_text(
            record.get("source_row_key", expected_source_row_key),
            "source_row_key",
        )
        if source_row_key != expected_source_row_key:
            raise ValueError("source_row_key does not match role, position, and stint")

        canonical_stats_payload = (
            {
                key: value
                for key, value in stats_payload.items()
                if key not in _FIELDING_STRUCTURAL_STATS_FIELDS
            }
            if role == "fielding"
            else stats_payload
        )
        values.update(
            {
                "game_identity_id": _required_text(
                    record.get("game_identity_id"), "game_identity_id"
                ),
                "team_identity_id": _required_text(
                    record.get("team_identity_id"), "team_identity_id"
                ),
                "player_identity_id": _required_text(
                    record.get("player_identity_id"), "player_identity_id"
                ),
                "role": role,
                "source_row_key": source_row_key,
                "position_code": position_code,
                "source_stint_key": source_stint_key,
                "stats_json": _canonical_json(canonical_stats_payload),
            }
        )
        values["normalized_checksum"] = self._game_player_normalized_checksum(values)
        return values

    @staticmethod
    def _game_player_normalized_checksum(values: Mapping[str, object]) -> str:
        return _normalized_checksum(
            {
                key: values[key]
                for key in (
                    "game_identity_id",
                    "team_identity_id",
                    "player_identity_id",
                    "role",
                    "provider_updated_at",
                    "stats_json",
                )
            }
        )

    @staticmethod
    def _record_versioned_snapshot_on_connection(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        logical_columns: tuple[str, ...],
        values: Mapping[str, object],
    ) -> dict[str, Any]:
        predicate = " AND ".join(f"{column}=?" for column in logical_columns)
        logical_values = tuple(values[column] for column in logical_columns)
        history = connection.execute(
            f"SELECT * FROM {table} WHERE {predicate} ORDER BY revision_number",
            logical_values,
        ).fetchall()
        normalized = next(
            (
                row
                for row in history
                if row["normalized_checksum"] == values["normalized_checksum"]
            ),
            None,
        )
        if normalized is not None:
            return dict(normalized)
        existing = next(
            (
                row
                for row in history
                if row["source_checksum"] == values["source_checksum"]
            ),
            None,
        )
        if existing is not None:
            if existing["normalized_checksum"] != values["normalized_checksum"]:
                migrated_fielding_evidence_matches = (
                    table == "stats_game_player_snapshots"
                    and values.get("role") == "fielding"
                    and StatsRepository._game_player_normalized_checksum(
                        dict(existing)
                    )
                    == values["normalized_checksum"]
                )
                if not migrated_fielding_evidence_matches:
                    raise StatsInvariantError(
                        f"{table} source checksum maps to conflicting normalized evidence"
                    )
            return dict(existing)
        latest = history[-1] if history else None
        revision_number = 1 if latest is None else int(latest["revision_number"]) + 1
        insert_values = {
            **values,
            "revision_number": revision_number,
            "revision_kind": "initial" if latest is None else "correction",
        }
        columns = tuple(insert_values)
        cursor = connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(insert_values[column] for column in columns),
        )
        if cursor.lastrowid is None:
            raise StatsInvariantError(f"{table} revision could not be read after insert")
        return {id_column: cursor.lastrowid, **insert_values}

    def record_lineup_snapshot(
        self, record: Mapping[str, object], entries: Sequence[Mapping[str, object]]
    ) -> dict[str, Any]:
        return self.record_lineup_snapshots(((record, entries),))[0]

    def record_lineup_snapshots(
        self,
        snapshots: Sequence[
            tuple[Mapping[str, object], Sequence[Mapping[str, object]]]
        ],
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(
            (self._lineup_snapshot_values(record), tuple(entries))
            for record, entries in snapshots
        )
        results: list[dict[str, Any]] = []
        with self.database.connect(write=True) as connection:
            for values, entries in prepared:
                result = _insert_once(
                    connection,
                    "stats_lineup_snapshots",
                    values,
                    {"lineup_snapshot_id": values["lineup_snapshot_id"]},
                )
                for entry in entries:
                    entry_values: dict[str, object] = {
                        "lineup_snapshot_id": values["lineup_snapshot_id"],
                        "player_identity_id": _required_text(
                            entry.get("player_identity_id"), "player_identity_id"
                        ),
                        "batting_order": entry.get("batting_order"),
                        "position_code": _optional_text(entry.get("position_code")),
                        "lineup_role": _required_text(
                            entry.get("lineup_role"), "lineup_role"
                        ),
                        "entry_json": _canonical_json(entry.get("entry", entry)),
                    }
                    _insert_once(
                        connection,
                        "stats_lineup_entries",
                        entry_values,
                        {
                            "lineup_snapshot_id": values["lineup_snapshot_id"],
                            "player_identity_id": entry_values[
                                "player_identity_id"
                            ],
                            "lineup_role": entry_values["lineup_role"],
                            "batting_order": entry_values["batting_order"],
                        },
                    )
                results.append(result)
        return tuple(results)

    def _lineup_snapshot_values(
        self, record: Mapping[str, object]
    ) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "lineup_snapshot_id": _required_text(record.get("lineup_snapshot_id"), "lineup_snapshot_id"),
                "game_identity_id": _required_text(record.get("game_identity_id"), "game_identity_id"),
                "team_identity_id": _required_text(record.get("team_identity_id"), "team_identity_id"),
                "lineup_state": _required_text(record.get("lineup_state"), "lineup_state"),
            }
        )
        return values

    def upsert_play_identity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.upsert_play_identities((record,))[0]

    def upsert_play_identities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._event_identity_values("play", record) for record in records)
        with self.database.connect(write=True) as connection:
            return self._upsert_event_identities_on_connection(
                connection, "stats_play_identities", "play", values
            )

    def upsert_pitch_identity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.upsert_pitch_identities((record,))[0]

    def upsert_pitch_identities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._event_identity_values("pitch", record) for record in records)
        with self.database.connect(write=True) as connection:
            return self._upsert_event_identities_on_connection(
                connection, "statcast_pitch_identities", "pitch", values
            )

    def get_statcast_pitch_identities(
        self, identities: Sequence[tuple[int, int, int]]
    ) -> dict[tuple[int, int, int], dict[str, Any]]:
        """Load Statcast pitch identities in bounded SQLite parameter batches."""

        natural_keys = tuple(
            dict.fromkeys(
                (
                    _integer(game_pk, "game_pk"),
                    _integer(at_bat_number, "at_bat_number"),
                    _integer(pitch_number, "pitch_number"),
                )
                for game_pk, at_bat_number, pitch_number in identities
            )
        )
        if not natural_keys:
            return {}
        results: dict[tuple[int, int, int], dict[str, Any]] = {}
        chunk_size = max(1, (_IDENTITY_QUERY_PARAMETER_LIMIT - 1) // 3)
        value_group = "(?,?,?)"
        with self.database.connect() as connection:
            for offset in range(0, len(natural_keys), chunk_size):
                chunk = natural_keys[offset : offset + chunk_size]
                placeholders = ",".join(value_group for _ in chunk)
                parameters = tuple(value for key in chunk for value in key)
                rows = connection.execute(
                    "SELECT * FROM statcast_pitch_identities "
                    "WHERE provider='statcast' "
                    "AND (game_pk,at_bat_number,pitch_number) "
                    f"IN (VALUES {placeholders})",
                    parameters,
                ).fetchall()
                for row in rows:
                    values = dict(row)
                    key = (
                        int(values["game_pk"]),
                        int(values["at_bat_number"]),
                        int(values["pitch_number"]),
                    )
                    results[key] = values
        return results

    @staticmethod
    def _event_identity_values(
        kind: str, record: Mapping[str, object]
    ) -> dict[str, object]:
        id_column = f"{kind}_identity_id"
        provider_id_column = f"provider_{kind}_id"
        values: dict[str, object] = {
            id_column: _required_text(record.get(id_column), id_column),
            "provider": _required_text(record.get("provider"), "provider").lower(),
            "game_identity_id": _required_text(record.get("game_identity_id"), "game_identity_id"),
            provider_id_column: _required_text(record.get(provider_id_column), provider_id_column),
            "at_bat_index": record.get("at_bat_index"),
            "first_seen_at": _timestamp(record.get("first_seen_at"), "first_seen_at"),
            "last_seen_at": _timestamp(record.get("last_seen_at"), "last_seen_at"),
        }
        if kind == "pitch":
            if values["provider"] != "statcast":
                raise StatsInvariantError("pitch identities require the statcast provider")
            values["play_identity_id"] = _optional_text(record.get("play_identity_id"))
            values["game_pk"] = _integer(record.get("game_pk"), "game_pk")
            values.pop("at_bat_index")
            values["at_bat_number"] = _integer(
                record.get("at_bat_number"), "at_bat_number"
            )
            values["pitch_number"] = _integer(
                record.get("pitch_number"), "pitch_number"
            )
            expected_provider_pitch_id = (
                f"{values['game_pk']}:{values['at_bat_number']}:"
                f"{values['pitch_number']}"
            )
            if values[provider_id_column] != expected_provider_pitch_id:
                raise StatsInvariantError(
                    "Statcast provider_pitch_id must equal "
                    "game_pk:at_bat_number:pitch_number"
                )
        return values

    @staticmethod
    def _upsert_event_identity_on_connection(
        connection: sqlite3.Connection,
        table: str,
        kind: str,
        values: Mapping[str, object],
    ) -> dict[str, Any]:
        return StatsRepository._upsert_event_identities_on_connection(
            connection, table, kind, (values,)
        )[0]

    @staticmethod
    def _upsert_event_identities_on_connection(
        connection: sqlite3.Connection,
        table: str,
        kind: str,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[dict[str, Any], ...]:
        if not records:
            return ()

        provider_id_column = f"provider_{kind}_id"
        natural_columns: tuple[str, ...]
        if kind == "pitch":
            natural_columns = (
                "provider",
                "game_pk",
                "at_bat_number",
                "pitch_number",
            )
        else:
            natural_columns = (
                "provider",
                "game_identity_id",
                provider_id_column,
            )
        natural_keys = tuple(
            dict.fromkeys(
                tuple(values[column] for column in natural_columns)
                for values in records
            )
        )
        existing_by_natural_key: dict[tuple[object, ...], dict[str, Any]] = {}
        chunk_size = max(
            1, _IDENTITY_QUERY_PARAMETER_LIMIT // len(natural_columns)
        )
        column_list = ",".join(natural_columns)
        value_group = "(" + ",".join("?" for _ in natural_columns) + ")"
        for offset in range(0, len(natural_keys), chunk_size):
            chunk = natural_keys[offset : offset + chunk_size]
            placeholders = ",".join(value_group for _ in chunk)
            parameters = tuple(value for key in chunk for value in key)
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE ({column_list}) "
                f"IN (VALUES {placeholders})",
                parameters,
            ).fetchall()
            for row in rows:
                row_values = dict(row)
                key = tuple(row_values[column] for column in natural_columns)
                existing_by_natural_key[key] = row_values

        results: list[dict[str, Any]] = []
        for values in records:
            natural_key = tuple(values[column] for column in natural_columns)
            existing = existing_by_natural_key.get(natural_key)
            result = StatsRepository._upsert_event_identity_from_existing(
                connection,
                table=table,
                kind=kind,
                values=values,
                existing=existing,
            )
            existing_by_natural_key[natural_key] = result
            results.append(result)
        return tuple(results)

    @staticmethod
    def _upsert_event_identity_from_existing(
        connection: sqlite3.Connection,
        *,
        table: str,
        kind: str,
        values: Mapping[str, object],
        existing: Mapping[str, object] | None,
    ) -> dict[str, Any]:
        id_column = f"{kind}_identity_id"
        provider_id_column = f"provider_{kind}_id"
        if existing is None:
            if str(values["last_seen_at"]) < str(values["first_seen_at"]):
                raise ValueError(
                    f"{kind} identity last_seen_at must not precede first_seen_at"
                )
            columns = tuple(values)
            connection.execute(
                f"INSERT INTO {table}({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values[column] for column in columns),
            )
            return dict(values)
        else:
            if existing[id_column] != values[id_column]:
                raise StatsInvariantError(
                    f"provider {kind} identity is already bound to a different ID"
                )
            if str(values["last_seen_at"]) < str(existing["last_seen_at"]):
                raise StatsInvariantError(f"{kind} identity last_seen_at cannot regress")
            factual_columns = set(values) - {
                id_column,
                "provider",
                provider_id_column,
                "first_seen_at",
                "last_seen_at",
            }
            mismatches = [
                column
                for column in factual_columns
                if not _same_value(existing[column], values[column])
            ]
            if mismatches:
                raise StatsInvariantError(
                    f"{kind} identity conflict would overwrite preserved fields: "
                    + ", ".join(sorted(mismatches))
                )
            if existing["last_seen_at"] != values["last_seen_at"]:
                connection.execute(
                    f"UPDATE {table} SET last_seen_at=? WHERE {id_column}=?",
                    (values["last_seen_at"], values[id_column]),
                )
            result = dict(existing)
            result["last_seen_at"] = values["last_seen_at"]
            return result

    def record_play_revision(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_play_revisions((record,))[0]

    def record_play_revisions(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._play_revision_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return self._record_revisions_on_connection(
                connection,
                table="stats_play_revisions",
                id_column="play_revision_id",
                entity_column="play_identity_id",
                records=values,
            )

    def _play_revision_values(self, record: Mapping[str, object]) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "play_identity_id": _required_text(record.get("play_identity_id"), "play_identity_id"),
                "revision_number": _integer(record.get("revision_number"), "revision_number"),
                "revision_kind": _required_text(record.get("revision_kind"), "revision_kind"),
                "inning": record.get("inning"),
                "half_inning": _optional_text(record.get("half_inning")),
                "event_type": _optional_text(record.get("event_type")),
                "batter_identity_id": _optional_text(record.get("batter_identity_id")),
                "pitcher_identity_id": _optional_text(record.get("pitcher_identity_id")),
                "started_at": _optional_timestamp(record.get("started_at"), "started_at"),
                "ended_at": _optional_timestamp(record.get("ended_at"), "ended_at"),
                "play_json": _canonical_json(record.get("play", {})),
            }
        )
        return values

    def record_statcast_revision(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_statcast_revisions((record,))[0]

    def record_statcast_revisions(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        values = tuple(self._statcast_revision_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return self._record_revisions_on_connection(
                connection,
                table="statcast_pitch_revisions",
                id_column="statcast_revision_id",
                entity_column="pitch_identity_id",
                records=values,
            )

    def get_latest_statcast_revisions(
        self, pitch_identity_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Return the latest retained revision for each requested pitch identity."""

        identity_ids = tuple(
            dict.fromkeys(
                _required_text(value, "pitch_identity_id")
                for value in pitch_identity_ids
            )
        )
        if not identity_ids:
            return {}
        results: dict[str, dict[str, Any]] = {}
        with self.database.connect() as connection:
            for offset in range(
                0, len(identity_ids), _REVISION_HISTORY_QUERY_CHUNK_SIZE
            ):
                chunk = identity_ids[
                    offset : offset + _REVISION_HISTORY_QUERY_CHUNK_SIZE
                ]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    "SELECT * FROM statcast_pitch_revisions "
                    f"WHERE pitch_identity_id IN ({placeholders}) "
                    "ORDER BY pitch_identity_id,revision_number DESC",
                    chunk,
                ).fetchall()
                for row in rows:
                    identity_id = str(row["pitch_identity_id"])
                    results.setdefault(identity_id, dict(row))
        return results

    def _statcast_revision_values(
        self, record: Mapping[str, object]
    ) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "pitch_identity_id": _required_text(record.get("pitch_identity_id"), "pitch_identity_id"),
                "revision_number": _integer(record.get("revision_number"), "revision_number"),
                "revision_kind": _required_text(record.get("revision_kind"), "revision_kind"),
                "metrics_json": _canonical_json(record.get("metrics", {})),
            }
        )
        return values

    @staticmethod
    def _record_revision_on_connection(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_column: str,
        values: Mapping[str, object],
    ) -> dict[str, Any]:
        return StatsRepository._record_revisions_on_connection(
            connection,
            table=table,
            id_column=id_column,
            entity_column=entity_column,
            records=(values,),
        )[0]

    @staticmethod
    def _record_revisions_on_connection(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_column: str,
        records: Sequence[Mapping[str, object]],
    ) -> tuple[dict[str, Any], ...]:
        if not records:
            return ()

        entity_ids = tuple(
            dict.fromkeys(str(values[entity_column]) for values in records)
        )
        histories: dict[str, list[dict[str, Any]]] = {
            entity_id: [] for entity_id in entity_ids
        }
        for offset in range(0, len(entity_ids), _REVISION_HISTORY_QUERY_CHUNK_SIZE):
            chunk = entity_ids[offset : offset + _REVISION_HISTORY_QUERY_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {entity_column} IN ({placeholders}) "
                f"ORDER BY {entity_column},revision_number",
                chunk,
            ).fetchall()
            for row in rows:
                histories[str(row[entity_column])].append(dict(row))

        results: list[dict[str, Any]] = []
        for values in records:
            history = histories[str(values[entity_column])]
            result = StatsRepository._record_revision_from_history(
                connection,
                table=table,
                id_column=id_column,
                values=values,
                history=history,
            )
            results.append(result)
        return tuple(results)

    @staticmethod
    def _record_revision_from_history(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        values: Mapping[str, object],
        history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = next(
            (
                row
                for row in history
                if row["source_checksum"] == values["source_checksum"]
            ),
            None,
        )
        ignored = {
            id_column,
            "stats_run_id",
            "raw_payload_id",
            "retrieved_at",
            "revision_number",
            "revision_kind",
        }
        if existing is not None:
            mismatches = [
                column
                for column, expected in values.items()
                if column not in ignored and not _same_value(existing[column], expected)
            ]
            if mismatches:
                raise StatsInvariantError(
                    f"{table} checksum collision changed preserved fields: "
                    + ", ".join(sorted(mismatches))
                )
            return dict(existing)

        numbered = next(
            (
                row
                for row in history
                if row["revision_number"] == values["revision_number"]
            ),
            None,
        )
        if numbered is not None:
            mismatches = [
                column
                for column, expected in values.items()
                if column != id_column and not _same_value(numbered[column], expected)
            ]
            raise StatsInvariantError(
                f"{table} identity already exists with different fields: "
                + ", ".join(sorted(mismatches))
            )

        latest = history[-1] if history else None
        expected_number = 1 if latest is None else int(latest["revision_number"]) + 1
        expected_kind = "initial" if latest is None else None
        if values["revision_number"] != expected_number:
            raise StatsInvariantError(
                f"{table} revision_number must be the next sequential revision"
            )
        if expected_kind is not None and values["revision_kind"] != expected_kind:
            raise StatsInvariantError(f"{table} first revision must be initial")
        if expected_kind is None and values["revision_kind"] not in {
            "correction",
            "tombstone",
        }:
            raise StatsInvariantError(
                f"{table} later revisions must be correction or tombstone"
            )
        columns = tuple(values)
        cursor = connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
        if cursor.lastrowid is None:
            raise StatsInvariantError(f"{table} revision could not be read after insert")
        created = {id_column: cursor.lastrowid, **values}
        history.append(created)
        return created

    def record_season_snapshot(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_season_snapshots((record,))[0]

    def record_season_snapshots(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._season_snapshot_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_season_snapshot_on_connection(connection, values)
                for values in prepared
            )

    def has_season_snapshot_evidence(self, record: Mapping[str, object]) -> bool:
        """Verify a parsed snapshot against globally retained immutable evidence."""

        values = self._season_snapshot_values(record)
        entity_column = (
            "team_identity_id"
            if values["entity_kind"] == "team"
            else "player_identity_id"
        )
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM stats_season_snapshots "
                f"WHERE provider=? AND season=? AND split_key=? AND {entity_column}=? "
                "AND source_team_id=? AND source_stint_key=? "
                "AND source_checksum=? AND normalized_checksum=? LIMIT 1",
                (
                    values["provider"],
                    values["season"],
                    values["split_key"],
                    values[entity_column],
                    values["source_team_id"],
                    values["source_stint_key"],
                    values["source_checksum"],
                    values["normalized_checksum"],
                ),
            ).fetchone()
        return row is not None

    def _season_snapshot_values(
        self, record: Mapping[str, object]
    ) -> dict[str, object]:
        values = self._snapshot_base(record)
        values.update(
            {
                "season_snapshot_id": _required_text(record.get("season_snapshot_id"), "season_snapshot_id"),
                "provider": _required_text(record.get("provider"), "provider").lower(),
                "season": _integer(record.get("season"), "season"),
                "entity_kind": _required_text(record.get("entity_kind"), "entity_kind"),
                "team_identity_id": _optional_text(record.get("team_identity_id")),
                "player_identity_id": _optional_text(record.get("player_identity_id")),
                "source_team_id": _required_text(
                    record.get("source_team_id")
                    or record.get("team_identity_id")
                    or "ALL",
                    "source_team_id",
                ),
                "source_stint_key": _required_text(
                    record.get("source_stint_key") or "all",
                    "source_stint_key",
                ),
                "split_key": _required_text(record.get("split_key"), "split_key"),
                "snapshot_as_of": _timestamp(record.get("snapshot_as_of"), "snapshot_as_of"),
                "stats_json": _canonical_json(record.get("stats", {})),
            }
        )
        entity_column: str = (
            "team_identity_id"
            if values["entity_kind"] == "team"
            else "player_identity_id"
        )
        entity_id = values[entity_column]
        if entity_id is None:
            raise ValueError(
                f"season snapshot {values['entity_kind']} requires {entity_column}"
            )
        values["normalized_checksum"] = _normalized_checksum(
            {
                "provider": values["provider"],
                "season": values["season"],
                "entity_kind": values["entity_kind"],
                entity_column: entity_id,
                "split_key": values["split_key"],
                "source_team_id": values["source_team_id"],
                "source_stint_key": values["source_stint_key"],
                "provider_updated_at": values["provider_updated_at"],
                "stats_json": values["stats_json"],
            }
        )
        return values

    @staticmethod
    def _record_season_snapshot_on_connection(
        connection: sqlite3.Connection, values: Mapping[str, object]
    ) -> dict[str, Any]:
        entity_column = (
            "team_identity_id"
            if values["entity_kind"] == "team"
            else "player_identity_id"
        )
        entity_id = values[entity_column]
        existing = connection.execute(
            "SELECT * FROM stats_season_snapshots "
            f"WHERE provider=? AND season=? AND split_key=? AND {entity_column}=? "
            "AND source_team_id=? AND source_stint_key=? AND source_checksum=?",
            (
                values["provider"],
                values["season"],
                values["split_key"],
                entity_id,
                values["source_team_id"],
                values["source_stint_key"],
                values["source_checksum"],
            ),
        ).fetchone()
        if existing is not None:
            if existing["normalized_checksum"] != values["normalized_checksum"]:
                raise StatsInvariantError(
                    "season snapshot source checksum changed normalized evidence"
                )
            return dict(existing)
        return _insert_once(
            connection,
            "stats_season_snapshots",
            values,
            {"season_snapshot_id": values["season_snapshot_id"]},
        )

    def record_feature_snapshot(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.record_feature_snapshots((record,))[0]

    def record_feature_snapshots(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._feature_snapshot_values(record) for record in records)
        with self.database.connect(write=True) as connection:
            return tuple(
                self._record_feature_snapshot_on_connection(connection, values)
                for values in prepared
            )

    @staticmethod
    def _feature_snapshot_values(
        record: Mapping[str, object]
    ) -> dict[str, object]:
        values: dict[str, object] = {
            "feature_snapshot_id": _required_text(record.get("feature_snapshot_id"), "feature_snapshot_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "feature_version": _required_text(record.get("feature_version"), "feature_version"),
            "entity_kind": _required_text(record.get("entity_kind"), "entity_kind"),
            "game_identity_id": _optional_text(record.get("game_identity_id")),
            "team_identity_id": _optional_text(record.get("team_identity_id")),
            "player_identity_id": _optional_text(record.get("player_identity_id")),
            "canonical_player_id": _optional_text(record.get("canonical_player_id")),
            "feature_as_of": _timestamp(record.get("feature_as_of"), "feature_as_of"),
            "completeness_state": _required_text(record.get("completeness_state"), "completeness_state"),
            "observed_through": _optional_timestamp(record.get("observed_through"), "observed_through"),
            "complete_through": _optional_timestamp(record.get("complete_through"), "complete_through"),
            "input_checksum": _checksum(record.get("input_checksum"), "input_checksum"),
            "feature_checksum": _checksum(record.get("feature_checksum"), "feature_checksum"),
            "features_json": _canonical_json(record.get("features", {})),
            "created_at": _timestamp(record.get("created_at"), "created_at"),
        }
        entity_columns = {
            "game": "game_identity_id",
            "team": "team_identity_id",
            "player": "canonical_player_id",
        }
        try:
            entity_column = entity_columns[str(values["entity_kind"])]
        except KeyError as exc:
            raise ValueError("feature snapshot entity_kind is invalid") from exc
        entity_id = values[entity_column]
        if entity_id is None:
            raise ValueError(
                f"feature snapshot {values['entity_kind']} requires {entity_column}"
            )
        return values

    @staticmethod
    def _record_feature_snapshot_on_connection(
        connection: sqlite3.Connection, values: Mapping[str, object]
    ) -> dict[str, Any]:
        entity_columns = {
            "game": "game_identity_id",
            "team": "team_identity_id",
            "player": "canonical_player_id",
        }
        entity_column = entity_columns[str(values["entity_kind"])]
        entity_id = values[entity_column]
        existing = connection.execute(
            "SELECT * FROM stats_feature_snapshots "
            "WHERE feature_version=? AND entity_kind=? AND feature_as_of=? "
            f"AND input_checksum=? AND {entity_column}=?",
            (
                values["feature_version"],
                values["entity_kind"],
                values["feature_as_of"],
                values["input_checksum"],
                entity_id,
            ),
        ).fetchone()
        if existing is not None:
            ignored = {"feature_snapshot_id", "stats_run_id", "created_at"}
            mismatches = [
                column
                for column, expected in values.items()
                if column not in ignored
                and not _same_value(existing[column], expected)
            ]
            if mismatches:
                raise StatsInvariantError(
                    "feature input identity changed preserved output fields: "
                    + ", ".join(sorted(mismatches))
                )
            return dict(existing)
        return _insert_once(
            connection,
            "stats_feature_snapshots",
            values,
            {"feature_snapshot_id": values["feature_snapshot_id"]},
        )

    def record_conflict(self, record: Mapping[str, object]) -> dict[str, Any]:
        values: dict[str, object] = {
            "conflict_id": _required_text(record.get("conflict_id"), "conflict_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "checkpoint_id": _optional_text(record.get("checkpoint_id")),
            "conflict_code": _required_text(record.get("conflict_code"), "conflict_code"),
            "entity_kind": _required_text(record.get("entity_kind"), "entity_kind"),
            "entity_key": _required_text(record.get("entity_key"), "entity_key"),
            "existing_value_json": (
                _canonical_json(record["existing_value"]) if record.get("existing_value") is not None else None
            ),
            "observed_value_json": (
                _canonical_json(record["observed_value"]) if record.get("observed_value") is not None else None
            ),
            "detected_at": _timestamp(record.get("detected_at"), "detected_at"),
            "source_checksum": _checksum(record.get("source_checksum"), "source_checksum"),
        }
        with self.database.connect(write=True) as connection:
            return _insert_once(connection, "stats_conflicts", values, {"conflict_id": values["conflict_id"]})

    def record_conflict_resolution(self, record: Mapping[str, object]) -> dict[str, Any]:
        values: dict[str, object] = {
            "conflict_resolution_id": _required_text(record.get("conflict_resolution_id"), "conflict_resolution_id"),
            "conflict_id": _required_text(record.get("conflict_id"), "conflict_id"),
            "reconciliation_id": _optional_text(record.get("reconciliation_id")),
            "resolution": _required_text(record.get("resolution"), "resolution"),
            "resolver_id": _required_text(record.get("resolver_id"), "resolver_id"),
            "resolved_at": _timestamp(record.get("resolved_at"), "resolved_at"),
            "details_json": _canonical_json(record.get("details", {})),
            "source_checksum": _checksum(record.get("source_checksum"), "source_checksum"),
        }
        with self.database.connect(write=True) as connection:
            return _insert_once(
                connection,
                "stats_conflict_resolutions",
                values,
                {"conflict_resolution_id": values["conflict_resolution_id"]},
            )

    def record_reconciliation(
        self, record: Mapping[str, object], items: Sequence[Mapping[str, object]] = ()
    ) -> dict[str, Any]:
        values: dict[str, object] = {
            "reconciliation_id": _required_text(record.get("reconciliation_id"), "reconciliation_id"),
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "checkpoint_id": _optional_text(record.get("checkpoint_id")),
            "dataset_key": _required_text(record.get("dataset_key"), "dataset_key"),
            "scope_key": _required_text(record.get("scope_key"), "scope_key"),
            "status": _required_text(record.get("status"), "status"),
            "expected_count": record.get("expected_count"),
            "observed_count": record.get("observed_count"),
            "conflict_count": _integer(record.get("conflict_count", 0), "conflict_count"),
            "started_at": _timestamp(record.get("started_at"), "started_at"),
            "completed_at": _timestamp(record.get("completed_at"), "completed_at"),
            "details_json": _canonical_json(record.get("details", {})),
            "source_checksum": _checksum(record.get("source_checksum"), "source_checksum"),
        }
        with self.database.connect(write=True) as connection:
            result = _insert_once(
                connection,
                "stats_reconciliations",
                values,
                {"reconciliation_id": values["reconciliation_id"]},
            )
            for item in items:
                item_values: dict[str, object] = {
                    "reconciliation_id": values["reconciliation_id"],
                    "severity": _required_text(item.get("severity"), "severity"),
                    "code": _required_text(item.get("code"), "code"),
                    "entity_kind": _optional_text(item.get("entity_kind")),
                    "entity_key": _optional_text(item.get("entity_key")),
                    "message": redact_text(_required_text(item.get("message"), "message")),
                    "details_json": _canonical_json(item.get("details", {})),
                    "source_checksum": _checksum(item.get("source_checksum"), "source_checksum"),
                }
                identity = {
                    "reconciliation_id": values["reconciliation_id"],
                    "code": item_values["code"],
                    "source_checksum": item_values["source_checksum"],
                }
                _insert_once(connection, "stats_reconciliation_items", item_values, identity)
        return result

    def advance_completeness_watermark(
        self,
        *,
        provider: str,
        dataset_key: str,
        scope_key: str,
        requested_through_date: date | str,
        source_observed_at: datetime | str,
        latest_ingested_completed_game_date: date | str | None,
        contiguous_regular_season_complete_through_date: date | str | None,
        source_stats_run_id: str,
        reconciliation_id: str,
        source_checksum: str,
        partial_date: date | str | None = None,
        partial_date_reason: str | None = None,
        cursor: object | None = None,
        updated_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        safe_partial_date = _optional_date(partial_date, "partial_date")
        safe_partial_reason = _optional_text(partial_date_reason)
        if (safe_partial_date is None) != (safe_partial_reason is None):
            raise ValueError("partial_date and partial_date_reason must be supplied together")
        safe_requested_date = _calendar_date(
            requested_through_date, "requested_through_date"
        )
        safe_source_observed_at = _timestamp(
            source_observed_at, "source_observed_at"
        )
        key = {
            "provider": _required_text(provider, "provider").lower(),
            "dataset_key": _required_text(dataset_key, "dataset_key"),
            "scope_key": _required_text(scope_key, "scope_key"),
        }
        with self.database.connect(write=True) as connection:
            reconciliation = connection.execute(
                "SELECT status, stats_run_id FROM stats_reconciliations WHERE reconciliation_id=?",
                (_required_text(reconciliation_id, "reconciliation_id"),),
            ).fetchone()
            if reconciliation is None:
                raise StatsNotFoundError(f"Reconciliation not found: {reconciliation_id}")
            if reconciliation["status"] == "failed":
                raise StatsInvariantError("failed reconciliation cannot advance a watermark")
            if reconciliation["stats_run_id"] != source_stats_run_id:
                raise StatsInvariantError("watermark reconciliation belongs to another stats run")
            current = connection.execute(
                "SELECT * FROM stats_completeness_watermarks WHERE provider=? AND dataset_key=? AND scope_key=?",
                tuple(key.values()),
            ).fetchone()
            latest_date = _optional_date(
                latest_ingested_completed_game_date,
                "latest_ingested_completed_game_date",
            )
            contiguous_date = _optional_date(
                contiguous_regular_season_complete_through_date,
                "contiguous_regular_season_complete_through_date",
            )
            effective_source_observed_at = safe_source_observed_at
            if current is not None:
                current_requested_date = str(current["requested_through_date"])
                current_source_observed_at = str(current["source_observed_at"])
                if safe_requested_date < current_requested_date or (
                    safe_requested_date == current_requested_date
                    and safe_source_observed_at < current_source_observed_at
                ):
                    return dict(current)
                effective_source_observed_at = max(
                    safe_source_observed_at, current_source_observed_at
                )
                latest_date = max(
                    value
                    for value in (
                        latest_date,
                        current["latest_ingested_completed_game_date"],
                    )
                    if value is not None
                ) if (
                    latest_date is not None
                    or current["latest_ingested_completed_game_date"] is not None
                ) else None
                contiguous_date = max(
                    value
                    for value in (
                        contiguous_date,
                        current["contiguous_regular_season_complete_through_date"],
                    )
                    if value is not None
                ) if (
                    contiguous_date is not None
                    or current["contiguous_regular_season_complete_through_date"]
                    is not None
                ) else None
            values: dict[str, object] = {
                **key,
                "requested_through_date": safe_requested_date,
                "source_observed_at": effective_source_observed_at,
                "latest_ingested_completed_game_date": latest_date,
                "contiguous_regular_season_complete_through_date": contiguous_date,
                "partial_date": safe_partial_date,
                "partial_date_reason": safe_partial_reason,
                "cursor_json": _canonical_json(cursor) if cursor is not None else None,
                "source_stats_run_id": _required_text(source_stats_run_id, "source_stats_run_id"),
                "reconciliation_id": _required_text(reconciliation_id, "reconciliation_id"),
                "revision": 1 if current is None else int(current["revision"]) + 1,
                "updated_at": max(
                    _timestamp(updated_at or utc_now(), "updated_at"),
                    str(current["updated_at"]) if current is not None else "",
                ),
                "source_checksum": _checksum(source_checksum, "source_checksum"),
            }
            if current is None:
                columns = tuple(values)
                connection.execute(
                    f"INSERT INTO stats_completeness_watermarks({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(values[column] for column in columns),
                )
            else:
                evidence_columns = (
                    "requested_through_date",
                    "source_observed_at",
                    "latest_ingested_completed_game_date",
                    "contiguous_regular_season_complete_through_date",
                    "partial_date",
                    "partial_date_reason",
                    "cursor_json",
                    "source_stats_run_id",
                    "reconciliation_id",
                    "source_checksum",
                )
                if all(_same_value(current[column], values[column]) for column in evidence_columns):
                    return dict(current)
                mutable = {column: value for column, value in values.items() if column not in key}
                assignments = ",".join(f"{column}=?" for column in mutable)
                connection.execute(
                    f"UPDATE stats_completeness_watermarks SET {assignments} "
                    "WHERE provider=? AND dataset_key=? AND scope_key=?",
                    (*mutable.values(), *key.values()),
                )
            result = connection.execute(
                "SELECT * FROM stats_completeness_watermarks WHERE provider=? AND dataset_key=? AND scope_key=?",
                tuple(key.values()),
            ).fetchone()
        if result is None:
            raise StatsInvariantError("completeness watermark could not be read after write")
        return dict(result)

    def link_raw_entity(self, record: Mapping[str, object]) -> dict[str, Any]:
        return self.link_raw_entities((record,))[0]

    def link_raw_entities(
        self, records: Sequence[Mapping[str, object]]
    ) -> tuple[dict[str, Any], ...]:
        prepared = tuple(self._raw_entity_link_values(record) for record in records)
        if not prepared:
            return ()
        entity_fields = (
            "game_identity_id",
            "team_identity_id",
            "player_identity_id",
            "play_identity_id",
            "pitch_identity_id",
        )
        grouped: dict[
            tuple[str, str, str], list[tuple[int, dict[str, object]]]
        ] = {}
        for index, (values, _) in enumerate(prepared):
            entity_field = next(
                field for field in entity_fields if values[field] is not None
            )
            key = (
                str(values["raw_payload_id"]),
                str(values["link_role"]),
                entity_field,
            )
            grouped.setdefault(key, []).append((index, values))

        results: list[dict[str, Any] | None] = [None] * len(prepared)
        with self.database.connect(write=True) as connection:
            for (raw_payload_id, link_role, entity_field), group in grouped.items():
                rows = connection.execute(
                    "SELECT * FROM stats_raw_entity_links "
                    f"WHERE raw_payload_id=? AND link_role=? "
                    f"AND {entity_field} IS NOT NULL",
                    (raw_payload_id, link_role),
                ).fetchall()
                existing: dict[str, dict[str, Any]] = {}
                for row in rows:
                    row_values = dict(row)
                    entity_id = str(row_values[entity_field])
                    if entity_id in existing:
                        raise StatsInvariantError(
                            "raw entity link identity has duplicate retained rows"
                        )
                    existing[entity_id] = row_values

                for index, values in group:
                    entity_id = str(values[entity_field])
                    retained = existing.get(entity_id)
                    if retained is not None:
                        mismatches = [
                            column
                            for column, expected in values.items()
                            if not _same_value(retained[column], expected)
                        ]
                        if mismatches:
                            raise StatsInvariantError(
                                "stats_raw_entity_links identity already exists with "
                                "different fields: "
                                + ", ".join(sorted(mismatches))
                            )
                        results[index] = retained
                        continue
                    columns = tuple(values)
                    cursor = connection.execute(
                        "INSERT INTO stats_raw_entity_links("
                        + ",".join(columns)
                        + ") VALUES ("
                        + ",".join("?" for _ in columns)
                        + ")",
                        tuple(values[column] for column in columns),
                    )
                    if cursor.lastrowid is None:
                        raise StatsInvariantError("raw entity link insert disappeared")
                    created = {"raw_entity_link_id": cursor.lastrowid, **values}
                    existing[entity_id] = created
                    results[index] = created
        if any(result is None for result in results):
            raise StatsInvariantError("raw entity link batch result disappeared")
        return tuple(result for result in results if result is not None)

    @staticmethod
    def _raw_entity_link_values(
        record: Mapping[str, object]
    ) -> tuple[dict[str, object], dict[str, object]]:
        entity_fields = (
            "game_identity_id",
            "team_identity_id",
            "player_identity_id",
            "play_identity_id",
            "pitch_identity_id",
        )
        values: dict[str, object] = {
            "raw_payload_id": _required_text(record.get("raw_payload_id"), "raw_payload_id"),
            "link_role": _required_text(record.get("link_role"), "link_role"),
            **{field: _optional_text(record.get(field)) for field in entity_fields},
        }
        populated = [field for field in entity_fields if values[field] is not None]
        if len(populated) != 1:
            raise ValueError("raw entity links require exactly one typed entity ID")
        identity = {
            "raw_payload_id": values["raw_payload_id"],
            "link_role": values["link_role"],
            populated[0]: values[populated[0]],
        }
        return values, identity

    def load_player_game_history(
        self,
        player_identity_id: str,
        *,
        before_game_date: date | str,
        observed_before: datetime | str,
    ) -> tuple[dict[str, Any], ...]:
        cutoff_date = _calendar_date(before_game_date, "before_game_date")
        cutoff_time = _timestamp(observed_before, "observed_before")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH eligible AS (
                    SELECT snapshot.*,
                           game.official_date,
                           game.provider_game_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY snapshot.game_identity_id,
                                            snapshot.team_identity_id,
                                            snapshot.player_identity_id,
                                            snapshot.role
                               ORDER BY snapshot.revision_number DESC
                           ) AS history_rank
                    FROM stats_game_player_snapshots AS snapshot
                    JOIN stats_game_identities AS game
                      ON game.game_identity_id = snapshot.game_identity_id
                    WHERE snapshot.player_identity_id = ?
                      AND game.official_date < ?
                      AND snapshot.retrieved_at < ?
                )
                SELECT * FROM eligible
                WHERE history_rank = 1
                ORDER BY official_date, game_identity_id, team_identity_id, role
                """,
                (
                    _required_text(player_identity_id, "player_identity_id"),
                    cutoff_date,
                    cutoff_time,
                ),
            ).fetchall()
        return self._decode_history_rows(rows, "stats_json", "stats")

    def load_team_game_history(
        self,
        team_identity_id: str,
        *,
        before_game_date: date | str,
        observed_before: datetime | str,
    ) -> tuple[dict[str, Any], ...]:
        cutoff_date = _calendar_date(before_game_date, "before_game_date")
        cutoff_time = _timestamp(observed_before, "observed_before")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH eligible AS (
                    SELECT snapshot.*,
                           game.official_date,
                           game.provider_game_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY snapshot.game_identity_id,
                                            snapshot.team_identity_id,
                                            snapshot.side,
                                            snapshot.snapshot_kind
                               ORDER BY snapshot.revision_number DESC
                           ) AS history_rank
                    FROM stats_game_team_snapshots AS snapshot
                    JOIN stats_game_identities AS game
                      ON game.game_identity_id = snapshot.game_identity_id
                    WHERE snapshot.team_identity_id = ?
                      AND game.official_date < ?
                      AND snapshot.retrieved_at < ?
                )
                SELECT * FROM eligible
                WHERE history_rank = 1
                ORDER BY official_date, game_identity_id, snapshot_kind
                """,
                (
                    _required_text(team_identity_id, "team_identity_id"),
                    cutoff_date,
                    cutoff_time,
                ),
            ).fetchall()
        return self._decode_history_rows(rows, "stats_json", "stats")

    def load_statcast_pitch_history(
        self,
        game_identity_id: str,
        *,
        observed_before: datetime | str,
    ) -> tuple[dict[str, Any], ...]:
        cutoff_time = _timestamp(observed_before, "observed_before")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                WITH eligible AS (
                    SELECT revision.*,
                           identity.game_identity_id,
                           identity.game_pk,
                           identity.at_bat_number,
                           identity.pitch_number,
                           ROW_NUMBER() OVER (
                               PARTITION BY revision.pitch_identity_id
                               ORDER BY revision.revision_number DESC
                           ) AS history_rank
                    FROM statcast_pitch_revisions AS revision
                    JOIN statcast_pitch_identities AS identity
                      ON identity.pitch_identity_id = revision.pitch_identity_id
                    WHERE identity.game_identity_id = ?
                      AND revision.retrieved_at < ?
                )
                SELECT * FROM eligible
                WHERE history_rank = 1
                ORDER BY at_bat_number, pitch_number, pitch_identity_id
                """,
                (
                    _required_text(game_identity_id, "game_identity_id"),
                    cutoff_time,
                ),
            ).fetchall()
        return self._decode_history_rows(rows, "metrics_json", "metrics")

    @staticmethod
    def _decode_history_rows(
        rows: Sequence[sqlite3.Row], source_column: str, target_column: str
    ) -> tuple[dict[str, Any], ...]:
        decoded: list[dict[str, Any]] = []
        for source in rows:
            item = dict(source)
            item[target_column] = json.loads(str(item.pop(source_column)))
            item.pop("history_rank", None)
            decoded.append(item)
        return tuple(decoded)

    def get_ingestion_run(self, stats_run_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            result = connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (_required_text(stats_run_id, "stats_run_id"),),
            ).fetchone()
        return _row(result)

    def get_checkpoint(
        self, stats_run_id: str, dataset_key: str
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            result = connection.execute(
                "SELECT * FROM stats_checkpoints "
                "WHERE stats_run_id=? AND dataset_key=?",
                (
                    _required_text(stats_run_id, "stats_run_id"),
                    _required_text(dataset_key, "dataset_key"),
                ),
            ).fetchone()
        return _row(result)

    def list_checkpoint_raw_payloads(
        self, checkpoint_id: str
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stats_raw_payload_metadata "
                "WHERE checkpoint_id=? ORDER BY retrieved_at,raw_payload_id",
                (_required_text(checkpoint_id, "checkpoint_id"),),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_checkpoint_schedule_observations(
        self, stats_run_id: str, checkpoint_id: str
    ) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT observation.*, game.provider_game_id, game.official_date,
                       home.canonical_team_key AS home_team_key,
                       away.canonical_team_key AS away_team_key,
                       raw.checkpoint_id
                FROM stats_game_status_observations AS observation
                JOIN stats_game_identities AS game
                  ON game.game_identity_id=observation.game_identity_id
                JOIN stats_team_identities AS home
                  ON home.team_identity_id=game.home_team_identity_id
                JOIN stats_team_identities AS away
                  ON away.team_identity_id=game.away_team_identity_id
                JOIN stats_raw_payload_metadata AS raw
                  ON raw.raw_payload_id=observation.raw_payload_id
                WHERE observation.stats_run_id=? AND raw.checkpoint_id=?
                  AND game.provider='baseball_reference'
                ORDER BY game.official_date,game.provider_game_id,
                         observation.retrieved_at,observation.raw_payload_id
                """,
                (
                    _required_text(stats_run_id, "stats_run_id"),
                    _required_text(checkpoint_id, "checkpoint_id"),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_checkpoint_season_snapshots(
        self, stats_run_id: str, checkpoint_id: str
    ) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM stats_season_snapshots AS snapshot
                JOIN stats_raw_payload_metadata AS raw
                  ON raw.raw_payload_id=snapshot.raw_payload_id
                WHERE snapshot.stats_run_id=? AND raw.checkpoint_id=?
                """,
                (
                    _required_text(stats_run_id, "stats_run_id"),
                    _required_text(checkpoint_id, "checkpoint_id"),
                ),
            ).fetchone()
        return int(row[0])

    def has_statcast_revision(
        self,
        *,
        game_pk: int,
        at_bat_number: int,
        pitch_number: int,
        source_checksum: str,
    ) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM statcast_pitch_identities AS identity
                JOIN statcast_pitch_revisions AS revision
                  ON revision.pitch_identity_id=identity.pitch_identity_id
                WHERE identity.game_pk=? AND identity.at_bat_number=?
                  AND identity.pitch_number=? AND revision.source_checksum=?
                LIMIT 1
                """,
                (
                    game_pk,
                    at_bat_number,
                    pitch_number,
                    _checksum(source_checksum, "source_checksum"),
                ),
            ).fetchone()
        return row is not None

    def list_scope_run_ids(self, provider: str, season: int) -> list[str]:
        scope_key = f"regular-season:{season}"
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT stats_run_id FROM stats_ingestion_runs "
                "WHERE provider=? AND scope_key=? ORDER BY created_at,stats_run_id",
                (_required_text(provider, "provider").lower(), scope_key),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def get_completeness_watermark(
        self,
        *,
        provider: str,
        dataset_key: str,
        season: int,
    ) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM stats_completeness_watermarks "
                "WHERE provider=? AND dataset_key=? AND scope_key=?",
                (
                    _required_text(provider, "provider").lower(),
                    _required_text(dataset_key, "dataset_key"),
                    f"season:{season}",
                ),
            ).fetchone()
        return _row(row)

    def _snapshot_base(self, record: Mapping[str, object]) -> dict[str, object]:
        return {
            "stats_run_id": _required_text(record.get("stats_run_id"), "stats_run_id"),
            "raw_payload_id": _optional_text(record.get("raw_payload_id")),
            "provider_updated_at": _optional_timestamp(record.get("provider_updated_at"), "provider_updated_at"),
            "retrieved_at": _timestamp(record.get("retrieved_at"), "retrieved_at"),
            "source_checksum": _checksum(record.get("source_checksum"), "source_checksum"),
        }
