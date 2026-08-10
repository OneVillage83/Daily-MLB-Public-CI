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


HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_V1"
)


class HistoricalPITCapabilityError(RuntimeError):
    """Raised when historical reconstruction capability cannot be established."""


def _parse_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HistoricalPITCapabilityError(
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


def _json_keys(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        payload = json.loads(str(value))
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, dict):
        return ()
    return tuple(sorted(str(key) for key in payload))


@dataclass(frozen=True, slots=True)
class HistoricalGameScopedCapabilityV1:
    table_name: str
    provider: str
    season: int
    row_count: int
    game_count: int
    detail_values: tuple[str, ...] = ()
    sample_json_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.table_name.strip() or not self.provider.strip():
            raise HistoricalPITCapabilityError("capability identity must not be empty")
        if self.season <= 0 or self.row_count <= 0 or self.game_count <= 0:
            raise HistoricalPITCapabilityError("capability counts must be positive")
        if self.game_count > self.row_count:
            raise HistoricalPITCapabilityError("capability game_count exceeds row_count")
        if tuple(sorted(set(self.detail_values))) != self.detail_values:
            raise HistoricalPITCapabilityError("capability detail_values must be sorted unique")
        if tuple(sorted(set(self.sample_json_keys))) != self.sample_json_keys:
            raise HistoricalPITCapabilityError(
                "capability sample_json_keys must be sorted unique"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "detail_values": list(self.detail_values),
            "game_count": self.game_count,
            "provider": self.provider,
            "row_count": self.row_count,
            "sample_json_keys": list(self.sample_json_keys),
            "season": self.season,
            "table_name": self.table_name,
        }


@dataclass(frozen=True, slots=True)
class HistoricalPITReconstructionCapabilityInventoryV1:
    start_date: date
    end_date: date
    source_inventory_checksum: str
    capabilities: tuple[HistoricalGameScopedCapabilityV1, ...]
    tables_present: tuple[str, ...]
    contract_version: str = HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise HistoricalPITCapabilityError("capability end_date precedes start_date")
        if len(self.source_inventory_checksum) != 64:
            raise HistoricalPITCapabilityError("source inventory checksum is malformed")
        if tuple(sorted(set(self.tables_present))) != self.tables_present:
            raise HistoricalPITCapabilityError("tables_present must be sorted unique")
        identities = tuple(
            (item.table_name, item.provider, item.season)
            for item in self.capabilities
        )
        if len(identities) != len(set(identities)):
            raise HistoricalPITCapabilityError("duplicate capability coverage row")
        if self.contract_version != HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION:
            raise HistoricalPITCapabilityError("unsupported reconstruction capability contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "capabilities": [item.as_dict() for item in self.capabilities],
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "source_inventory_checksum": self.source_inventory_checksum,
            "start_date": self.start_date.isoformat(),
            "tables_present": list(self.tables_present),
        }

    @property
    def checksum(self) -> str:
        return _sha(self.as_dict())

    def summary_as_dict(self) -> dict[str, object]:
        payload = self.as_dict()
        payload["capability_checksum"] = self.checksum
        return payload


@dataclass(frozen=True, slots=True)
class _CapabilitySpec:
    table_name: str
    detail_column: str | None = None
    json_column: str | None = None


_DIRECT_SPECS = (
    _CapabilitySpec(
        "stats_game_team_snapshots",
        detail_column="snapshot_kind",
        json_column="stats_json",
    ),
    _CapabilitySpec(
        "stats_game_player_snapshots",
        detail_column="role",
        json_column="stats_json",
    ),
    _CapabilitySpec("stats_lineup_snapshots", detail_column="lineup_state"),
    _CapabilitySpec("stats_play_identities"),
)

_CAPABILITY_TABLES = frozenset(
    {
        "stats_game_team_snapshots",
        "stats_game_player_snapshots",
        "stats_lineup_snapshots",
        "stats_lineup_entries",
        "stats_play_identities",
    }
)


def _details(value: object) -> tuple[str, ...]:
    return tuple(
        sorted(
            item.strip()
            for item in str(value or "").split(",")
            if item.strip()
        )
    )


def _direct_capabilities(
    connection: sqlite3.Connection,
    spec: _CapabilitySpec,
    start: date,
    end: date,
) -> tuple[HistoricalGameScopedCapabilityV1, ...]:
    columns = _columns(connection, spec.table_name)
    if "game_identity_id" not in columns:
        return ()
    detail_sql = (
        f',GROUP_CONCAT(DISTINCT candidate."{spec.detail_column}") AS details'
        if spec.detail_column and spec.detail_column in columns
        else ",NULL AS details"
    )
    rows = connection.execute(
        f"""
        SELECT game.provider,game.season,COUNT(*) AS row_count,
               COUNT(DISTINCT candidate.game_identity_id) AS game_count
               {detail_sql}
        FROM "{spec.table_name}" AS candidate
        JOIN stats_game_identities AS game
          ON game.game_identity_id=candidate.game_identity_id
        WHERE game.game_type='R'
          AND game.official_date BETWEEN ? AND ?
        GROUP BY game.provider,game.season
        ORDER BY game.season,game.provider
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    output: list[HistoricalGameScopedCapabilityV1] = []
    for row in rows:
        sample_keys: tuple[str, ...] = ()
        if spec.json_column and spec.json_column in columns:
            sample = connection.execute(
                f"""
                SELECT candidate."{spec.json_column}"
                FROM "{spec.table_name}" AS candidate
                JOIN stats_game_identities AS game
                  ON game.game_identity_id=candidate.game_identity_id
                WHERE game.game_type='R'
                  AND game.provider=? AND game.season=?
                  AND game.official_date BETWEEN ? AND ?
                ORDER BY game.official_date,candidate.rowid
                LIMIT 1
                """,
                (
                    str(row["provider"]),
                    int(row["season"]),
                    start.isoformat(),
                    end.isoformat(),
                ),
            ).fetchone()
            if sample is not None:
                sample_keys = _json_keys(sample[0])
        output.append(
            HistoricalGameScopedCapabilityV1(
                table_name=spec.table_name,
                provider=str(row["provider"]),
                season=int(row["season"]),
                row_count=int(row["row_count"]),
                game_count=int(row["game_count"]),
                detail_values=_details(row["details"]),
                sample_json_keys=sample_keys,
            )
        )
    return tuple(output)


def _lineup_entries(
    connection: sqlite3.Connection,
    start: date,
    end: date,
) -> tuple[HistoricalGameScopedCapabilityV1, ...]:
    if not {"stats_lineup_entries", "stats_lineup_snapshots"} <= _tables(connection):
        return ()
    rows = connection.execute(
        """
        SELECT game.provider,game.season,COUNT(*) AS row_count,
               COUNT(DISTINCT lineup.game_identity_id) AS game_count,
               GROUP_CONCAT(DISTINCT entry.lineup_role) AS details
        FROM stats_lineup_entries AS entry
        JOIN stats_lineup_snapshots AS lineup
          ON lineup.lineup_snapshot_id=entry.lineup_snapshot_id
        JOIN stats_game_identities AS game
          ON game.game_identity_id=lineup.game_identity_id
        WHERE game.game_type='R'
          AND game.official_date BETWEEN ? AND ?
        GROUP BY game.provider,game.season
        ORDER BY game.season,game.provider
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return tuple(
        HistoricalGameScopedCapabilityV1(
            table_name="stats_lineup_entries",
            provider=str(row["provider"]),
            season=int(row["season"]),
            row_count=int(row["row_count"]),
            game_count=int(row["game_count"]),
            detail_values=_details(row["details"]),
        )
        for row in rows
    )


def inventory_historical_pit_reconstruction_capabilities(
    database_path: Path | str,
    *,
    start_date: date | str,
    end_date: date | str,
) -> HistoricalPITReconstructionCapabilityInventoryV1:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if end < start:
        raise HistoricalPITCapabilityError("end_date must not precede start_date")
    try:
        source_inventory = inventory_historical_pit_source(
            database_path,
            start_date=start,
            end_date=end,
        )
    except HistoricalPITSourceError as exc:
        raise HistoricalPITCapabilityError(str(exc)) from exc

    capabilities: list[HistoricalGameScopedCapabilityV1] = []
    with open_historical_source_read_only(database_path) as connection:
        present = _tables(connection)
        for spec in _DIRECT_SPECS:
            if spec.table_name in present:
                capabilities.extend(_direct_capabilities(connection, spec, start, end))
        capabilities.extend(_lineup_entries(connection, start, end))
        tables_present = tuple(sorted(present & _CAPABILITY_TABLES))

    capabilities.sort(key=lambda item: (item.season, item.provider, item.table_name))
    return HistoricalPITReconstructionCapabilityInventoryV1(
        start_date=start,
        end_date=end,
        source_inventory_checksum=source_inventory.checksum,
        capabilities=tuple(capabilities),
        tables_present=tables_present,
    )
