from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.predictions.historical_pit_sources import (
    HistoricalPITSourceError,
    inventory_historical_pit_source,
    open_historical_source_read_only,
)


HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_PIT_FIELD_COVERAGE_V1"
)

_PLAYER_TABLE = "stats_game_player_snapshots"
_TEAM_TABLE = "stats_game_team_snapshots"
_PLAYER_STATS_TYPES = ("batting", "fielding", "pitching")
_SUPPORTED_TABLES = (_PLAYER_TABLE, _TEAM_TABLE)
_JSON_TYPE_NAMES = {
    "array": "array",
    "false": "boolean",
    "integer": "integer",
    "null": "null",
    "object": "object",
    "real": "float",
    "text": "string",
    "true": "boolean",
}
_OBSERVED_TYPE_CLASSIFICATIONS = frozenset(
    {"array", "boolean", "float", "integer", "mixed", "null", "object", "string"}
)


class HistoricalPITFieldCoverageError(RuntimeError):
    """Raised when nested historical PIT field coverage cannot be trusted."""


def _parse_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HistoricalPITFieldCoverageError(
            f"{field} must be exact YYYY-MM-DD"
        ) from exc


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_checksum(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _observed_type(raw_types: tuple[str, ...]) -> str:
    non_null_types = tuple(value for value in raw_types if value != "null")
    if not non_null_types:
        return "null"
    if len(non_null_types) == 1:
        return non_null_types[0]
    return "mixed"


@dataclass(frozen=True, slots=True)
class HistoricalPITNestedFieldCoverageV1:
    season: int
    table_name: str
    stats_type: str
    field_name: str
    total_applicable_row_count: int
    non_null_row_count: int
    distinct_game_count: int
    distinct_player_count: int | None
    observed_type: str
    observed_types: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.season, bool) or not isinstance(self.season, int) or self.season <= 0:
            raise HistoricalPITFieldCoverageError("field coverage season must be positive")
        if self.table_name not in _SUPPORTED_TABLES:
            raise HistoricalPITFieldCoverageError("unsupported field coverage table")
        if not self.stats_type or self.stats_type != self.stats_type.strip():
            raise HistoricalPITFieldCoverageError("field coverage stats_type is invalid")
        if self.table_name == _PLAYER_TABLE and self.stats_type not in _PLAYER_STATS_TYPES:
            raise HistoricalPITFieldCoverageError("unsupported player stats_type")
        if not self.field_name or self.field_name != self.field_name.strip():
            raise HistoricalPITFieldCoverageError("field coverage field_name is invalid")
        counts = (
            self.total_applicable_row_count,
            self.non_null_row_count,
            self.distinct_game_count,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
            raise HistoricalPITFieldCoverageError("field coverage counts must be integers")
        if self.total_applicable_row_count <= 0:
            raise HistoricalPITFieldCoverageError(
                "field coverage total_applicable_row_count must be positive"
            )
        if not 0 <= self.non_null_row_count <= self.total_applicable_row_count:
            raise HistoricalPITFieldCoverageError("field coverage non-null count is invalid")
        if not 0 <= self.distinct_game_count <= self.non_null_row_count:
            raise HistoricalPITFieldCoverageError("field coverage game count is invalid")
        if self.table_name == _PLAYER_TABLE:
            if (
                isinstance(self.distinct_player_count, bool)
                or not isinstance(self.distinct_player_count, int)
                or not 0 <= self.distinct_player_count <= self.non_null_row_count
            ):
                raise HistoricalPITFieldCoverageError(
                    "player field coverage requires a valid distinct player count"
                )
        elif self.distinct_player_count is not None:
            raise HistoricalPITFieldCoverageError(
                "team field coverage must not contain a player count"
            )
        if tuple(sorted(set(self.observed_types))) != self.observed_types:
            raise HistoricalPITFieldCoverageError("observed_types must be sorted unique")
        if not self.observed_types or not set(self.observed_types) <= (
            _OBSERVED_TYPE_CLASSIFICATIONS - {"mixed"}
        ):
            raise HistoricalPITFieldCoverageError("observed_types contains an invalid type")
        if self.observed_type != _observed_type(self.observed_types):
            raise HistoricalPITFieldCoverageError(
                "observed_type does not agree with observed_types"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "distinct_game_count": self.distinct_game_count,
            "distinct_player_count": self.distinct_player_count,
            "field_name": self.field_name,
            "non_null_row_count": self.non_null_row_count,
            "observed_type": self.observed_type,
            "observed_types": list(self.observed_types),
            "season": self.season,
            "stats_type": self.stats_type,
            "table_name": self.table_name,
            "total_applicable_row_count": self.total_applicable_row_count,
        }


@dataclass(frozen=True, slots=True)
class HistoricalPITFieldCoverageInventoryV1:
    start_date: date
    end_date: date
    source_inventory_checksum: str
    field_coverage: tuple[HistoricalPITNestedFieldCoverageV1, ...]
    provider: str = "retrosheet"
    contract_version: str = HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise HistoricalPITFieldCoverageError("field coverage date range is inverted")
        if not _is_checksum(self.source_inventory_checksum):
            raise HistoricalPITFieldCoverageError("source inventory checksum is malformed")
        if self.provider != "retrosheet":
            raise HistoricalPITFieldCoverageError("field coverage provider must be retrosheet")
        if self.contract_version != HISTORICAL_PIT_FIELD_COVERAGE_CONTRACT_VERSION:
            raise HistoricalPITFieldCoverageError("unsupported field coverage contract")
        identities = tuple(
            (item.season, item.table_name, item.stats_type, item.field_name)
            for item in self.field_coverage
        )
        if identities != tuple(sorted(identities)):
            raise HistoricalPITFieldCoverageError(
                "field coverage inventory must use deterministic order"
            )
        if len(identities) != len(set(identities)):
            raise HistoricalPITFieldCoverageError("duplicate field coverage identity")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "field_coverage": [item.as_dict() for item in self.field_coverage],
            "provider": self.provider,
            "source_inventory_checksum": self.source_inventory_checksum,
            "start_date": self.start_date.isoformat(),
        }

    @property
    def field_coverage_checksum(self) -> str:
        return _sha(self.as_dict())

    @property
    def checksum(self) -> str:
        return self.field_coverage_checksum

    def summary_as_dict(self) -> dict[str, object]:
        payload = self.as_dict()
        payload["field_coverage_checksum"] = self.field_coverage_checksum
        return payload


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    }


def _validate_field_coverage_schema(connection: sqlite3.Connection) -> None:
    tables = _tables(connection)
    missing_tables = sorted(set(_SUPPORTED_TABLES) - tables)
    if missing_tables:
        raise HistoricalPITFieldCoverageError(
            "historical PIT field coverage is missing required table(s): "
            + ", ".join(missing_tables)
        )
    required_columns = {
        _PLAYER_TABLE: {
            "game_identity_id",
            "player_identity_id",
            "role",
            "stats_json",
        },
        _TEAM_TABLE: {"game_identity_id", "snapshot_kind", "stats_json"},
    }
    for table, required in required_columns.items():
        missing_columns = sorted(required - _columns(connection, table))
        if missing_columns:
            raise HistoricalPITFieldCoverageError(
                f"{table} is missing required field coverage column(s): "
                + ", ".join(missing_columns)
            )


def _validate_nested_values(
    connection: sqlite3.Connection,
    table: str,
    start: date,
    end: date,
) -> None:
    type_predicate = (
        "snapshot.role IN ('batting','fielding','pitching')"
        if table == _PLAYER_TABLE
        else "1=1"
    )
    invalid_count = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM "{table}" AS snapshot
            JOIN stats_game_identities AS game
              ON game.game_identity_id=snapshot.game_identity_id
            WHERE game.provider='retrosheet'
              AND game.game_type='R'
              AND game.official_date BETWEEN ? AND ?
              AND {type_predicate}
              AND CASE
                    WHEN json_valid(snapshot.stats_json)=0 THEN 1
                    WHEN json_type(snapshot.stats_json, '$') IS NOT 'object' THEN 1
                    WHEN json_type(snapshot.stats_json, '$.values') IS NOT 'object' THEN 1
                    ELSE 0
                  END=1
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchone()[0]
    )
    if invalid_count:
        raise HistoricalPITFieldCoverageError(
            f"{table} contains {invalid_count} applicable row(s) without a valid nested values object"
        )
    if table == _TEAM_TABLE:
        invalid_type_count = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM "{table}" AS snapshot
                JOIN stats_game_identities AS game
                  ON game.game_identity_id=snapshot.game_identity_id
                WHERE game.provider='retrosheet'
                  AND game.game_type='R'
                  AND game.official_date BETWEEN ? AND ?
                  AND CASE
                        WHEN json_type(snapshot.stats_json, '$.stats_type') IS NOT 'text'
                          THEN 1
                        WHEN length(trim(json_extract(snapshot.stats_json, '$.stats_type')))=0
                          THEN 1
                        ELSE 0
                      END=1
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()[0]
        )
        if invalid_type_count:
            raise HistoricalPITFieldCoverageError(
                "stats_game_team_snapshots contains applicable row(s) without a retained stats type"
            )


def _coverage_query(table: str) -> str:
    is_player = table == _PLAYER_TABLE
    stats_type_expression = (
        "snapshot.role"
        if is_player
        else "trim(CAST(json_extract(snapshot.stats_json, '$.stats_type') AS TEXT))"
    )
    player_select = "snapshot.player_identity_id" if is_player else "NULL"
    type_predicate = (
        "AND snapshot.role IN ('batting','fielding','pitching')" if is_player else ""
    )
    player_count = (
        "COUNT(DISTINCT CASE WHEN field.type <> 'null' "
        "THEN eligible.player_identity_id END)"
        if is_player
        else "NULL"
    )
    return f"""
        WITH eligible AS (
            SELECT snapshot.rowid AS snapshot_rowid,
                   game.season AS season,
                   {stats_type_expression} AS stats_type,
                   snapshot.game_identity_id AS game_identity_id,
                   {player_select} AS player_identity_id,
                   snapshot.stats_json AS stats_json
            FROM "{table}" AS snapshot
            JOIN stats_game_identities AS game
              ON game.game_identity_id=snapshot.game_identity_id
            WHERE game.provider='retrosheet'
              AND game.game_type='R'
              AND game.official_date BETWEEN ? AND ?
              {type_predicate}
        ),
        totals AS (
            SELECT season,stats_type,COUNT(*) AS total_applicable_row_count
            FROM eligible
            GROUP BY season,stats_type
        ),
        fields AS (
            SELECT eligible.season AS season,
                   eligible.stats_type AS stats_type,
                   field.key AS field_name,
                   COUNT(DISTINCT CASE WHEN field.type <> 'null'
                                      THEN eligible.snapshot_rowid END) AS non_null_row_count,
                   COUNT(DISTINCT CASE WHEN field.type <> 'null'
                                      THEN eligible.game_identity_id END) AS distinct_game_count,
                   {player_count} AS distinct_player_count,
                   GROUP_CONCAT(DISTINCT field.type) AS observed_types
            FROM eligible
            JOIN json_each(eligible.stats_json, '$.values') AS field
            GROUP BY eligible.season,eligible.stats_type,field.key
        )
        SELECT fields.season,fields.stats_type,fields.field_name,
               totals.total_applicable_row_count,fields.non_null_row_count,
               fields.distinct_game_count,fields.distinct_player_count,
               fields.observed_types
        FROM fields
        JOIN totals
          ON totals.season=fields.season
         AND totals.stats_type=fields.stats_type
        ORDER BY fields.season,fields.stats_type,fields.field_name
    """


def _normalize_types(value: object) -> tuple[str, ...]:
    raw_types = tuple(str(item) for item in str(value or "").split(",") if item)
    try:
        return tuple(sorted({_JSON_TYPE_NAMES[item] for item in raw_types}))
    except KeyError as exc:
        raise HistoricalPITFieldCoverageError(
            "nested field contains an unsupported SQLite JSON type"
        ) from exc


def _field_coverage(
    connection: sqlite3.Connection,
    table: str,
    start: date,
    end: date,
) -> tuple[HistoricalPITNestedFieldCoverageV1, ...]:
    rows = connection.execute(
        _coverage_query(table),
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    result: list[HistoricalPITNestedFieldCoverageV1] = []
    for row in rows:
        observed_types = _normalize_types(row["observed_types"])
        result.append(
            HistoricalPITNestedFieldCoverageV1(
                season=int(row["season"]),
                table_name=table,
                stats_type=str(row["stats_type"]),
                field_name=str(row["field_name"]),
                total_applicable_row_count=int(row["total_applicable_row_count"]),
                non_null_row_count=int(row["non_null_row_count"]),
                distinct_game_count=int(row["distinct_game_count"]),
                distinct_player_count=(
                    int(row["distinct_player_count"])
                    if row["distinct_player_count"] is not None
                    else None
                ),
                observed_type=_observed_type(observed_types),
                observed_types=observed_types,
            )
        )
    return tuple(result)


def inventory_historical_pit_field_coverage(
    database_path: Path | str,
    *,
    start_date: date | str,
    end_date: date | str,
) -> HistoricalPITFieldCoverageInventoryV1:
    """Inventory nested Retrosheet field coverage without emitting source values."""

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if end < start:
        raise HistoricalPITFieldCoverageError("end_date must not precede start_date")
    try:
        source_inventory = inventory_historical_pit_source(
            database_path,
            start_date=start,
            end_date=end,
        )
        coverage: list[HistoricalPITNestedFieldCoverageV1] = []
        with open_historical_source_read_only(database_path) as connection:
            _validate_field_coverage_schema(connection)
            for table in _SUPPORTED_TABLES:
                _validate_nested_values(connection, table, start, end)
                coverage.extend(_field_coverage(connection, table, start, end))
    except HistoricalPITSourceError as exc:
        raise HistoricalPITFieldCoverageError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise HistoricalPITFieldCoverageError(
            "historical PIT nested field coverage query failed"
        ) from exc

    coverage.sort(
        key=lambda item: (item.season, item.table_name, item.stats_type, item.field_name)
    )
    return HistoricalPITFieldCoverageInventoryV1(
        start_date=start,
        end_date=end,
        source_inventory_checksum=source_inventory.checksum,
        field_coverage=tuple(coverage),
    )
