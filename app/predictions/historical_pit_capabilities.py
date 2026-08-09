from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from app.predictions.historical_pit_sources import (
    HistoricalPITSourceError,
    build_historical_pit_source_inventory,
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
        keys = tuple(
            (item.table_name, item.provider, item.season)
            for item in self.capabilities
        )
        if len(keys) != len(set(keys)):
            raise HistoricalPITCapabilityError("duplicate capability coverage row")
        if self.contract_version != HISTORICAL_PIT_RECONSTRUCTION_CAPABILITY_CONTRACT_VERSION:
            raise HistoricalPITCapabilityError("unsupported reconstruction capability contract")

    def coverage_for(
        self,
        table_name: str,
        provider: str,
        season: int,
    ) -> HistoricalGameScopedCapabilityV1 | None:
        for item in self.capabilities:
            if (
                item.table_name == table_name
                and item.provider == provider
                and item.season == season
            ):
                return item
        return None

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
        return {
            "capabilities": [item.as_dict() for item in self.capabilities],
            "capability_checksum": self.checksum,
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "source_inventory_checksum": self.source_inventory_checksum,
            "start_date": self.start_date.isoformat(),
            "tables_present": list(self.tables_present),
        }


@dataclass(frozen=True, slots=True)
class _CapabilitySpec:
    table_name: str
    game_join_sql: str
    detail_column: str | None = None
    json_column: str | None = None


_CAPABILITY_SPECS = (
    _CapabilitySpec(
        "stats_game_team_snapshots",
        "candidate.game_identity_id=game.game_identity_id",
        detail_column="snapshot_kind",
        json_column="stats_json",
    ),
    _CapabilitySpec(
        "stats_game_player_snapshots",
        "candidate.game_identity_id=game.game_identity_id",
        detail_column="role",
        json_column="stats_json",
    ),
    _CapabilitySpec(
        "stats_lineup_snapshots",
        "candidate.game_identity_id=game.game_identity_id",
        detail_column="lineup_state",
    ),
    _CapabilitySpec(
        "stats_play_identities",
        "candidate.game_identity_id=game.game_identity_id",
    ),
)


def _direct_capabilities(
    connection: sqlite3.Connection,
    spec: _CapabilitySpec,
    *,
    start_date: date,
    end_date: date,
) -> tuple[HistoricalGameScopedCapabilityV1, ...]:
    table_columns = _columns(connection, spec.table_name)
    if "game_identity_id" not in table_columns:
        return ()
    detail_select = (
        f", GROUP_CONCAT(DISTINCT candidate.\"{spec.detail_column}\") AS details"
        if spec.detail_column is not None and spec.detail_column in table_columns
        else ", NULL AS details"
    )
    rows = connection.execute(
        f"""
        SELECT game.provider,game.season,
               COUNT(*) AS row_count,
               COUNT(DISTINCT candidate.game_identity_id) AS game_count
               {detail_select}
        FROM \"{spec.table_name}\" AS candidate
        JOIN stats_game_identities AS game ON {spec.game_join_sql}
        WHERE game.game_type='R'
          AND game.official_date BETWEEN ? AND ?
        GROUP BY game.provider,game.season
        ORDER BY game.season,game.provider
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()

    output: list[HistoricalGameScopedCapabilityV1] = []
    for row in rows:
        details = tuple(
            sorted(
                value.strip()
                for value in str(row["details"] or "").split(",")
                if value.strip()
            )
        )
        sample_keys: tuple[str, ...] = ()
        if spec.json_column is not None and spec.json_column in table_columns:
            sample = connection.execute(
                f"""
                SELECT candidate.\"{spec.json_column}\"
                FROM \"{spec.table_name}\" AS candidate
                JOIN stats_game_identities AS game ON {spec.game_join_sql}
                WHERE game.game_type='R'
                  AND game.provider=?
                  AND game.season=?
                  AND game.official_date BETWEEN ? AND ?
                ORDER BY game.official_date,candidate.rowid
                LIMIT 1
                """,
                (
                    str(row["provider"]),
                    int(row["season"]),
                    start_date.isoformat(),
                    end_date.isoformat(),
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
                detail_values=details,
                sample_json_keys=sample_keys,
            )
        )
    return tuple(output)


def _lineup_entry_capabilities(
    connection: sqlite3.Connection,
    *,
    start_date: date,
    end_date: date,
) -> tuple[HistoricalGameScopedCapabilityV1, ...]:
    tables = _tables(connection)
    if not {"stats_lineup_entries", "stats_lineup_snapshots"} <= tables:
        return ()
    rows = connection.execute(
        """
        SELECT game.provider,game.season,
               COUNT(*) AS row_count,
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
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return tuple(
        HistoricalGameScopedCapabilityV1(
            table_name="stats_lineup_entries",
            provider=str(row["provider"]),
            season=int(row["season"]),
            row_count=int(row["row_count"]),
            game_count=int(row["game_count"]),
            detail_values=tuple(
                sorted(
                    value.strip()
                    for value in str(row["details"] or "").split(",")
                    if value.strip()
                )
            ),
        )
        for row in rows
    )


def build_historical_pit_reconstruction_capabilities(
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
        source_inventory = build_historical_pit_source_inventory(
            database_path,
            start_date=start,
            end_date=end,
        )
    except HistoricalPITSourceError as exc:
        raise HistoricalPITCapabilityError(str(exc)) from exc

    with open_historical_source_read_only(database_path) as connection:
        present = _tables(connection)
        capability_tables = tuple(
            sorted(
                present
                & {
                    "stats_game_team_snapshots",
                    "stats_game_player_snapshots",
                    "stats_lineup_snapshots",
                    "stats_lineup_entries",
                    "stats_play_identities",
                }
            )
        )
        capabilities: list[HistoricalGameScopedCapabilityV1] = []
        for spec in _CAPABILITY_SPECS:
            if spec.table_name in present:
                capabilities.extend(
                    _direct_capabilities(
                        connection,
                        spec,
                        start_date=start,
                        end_date=end,
                    )
                )
        capabilities.extend(
            _lineup_entry_capabilities(
                connection,
                start_date=start,
                end_date=end,
            )
        )

    capabilities.sort(key=lambda item: (item.season, item.provider, item.table_name))
    return HistoricalPITReconstructionCapabilityInventoryV1(
        start_date=start,
        end_date=end,
        source_inventory_checksum=source_inventory.checksum,
        capabilities=tuple(capabilities),
        tables_present=capability_tables,
    )
