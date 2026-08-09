from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote


HISTORICAL_PIT_SOURCE_INVENTORY_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_PIT_SOURCE_INVENTORY_V1"
)

_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "stats_game_identities": frozenset(
        {
            "game_identity_id",
            "provider",
            "provider_game_id",
            "season",
            "game_type",
            "official_date",
            "scheduled_start",
            "home_team_identity_id",
            "away_team_identity_id",
            "identity_checksum",
        }
    ),
    "stats_game_status_observations": frozenset(
        {
            "game_identity_id",
            "abstract_state",
            "status_code",
            "retrieved_at",
            "revision_number",
            "status_json",
            "source_checksum",
        }
    ),
    "stats_completeness_watermarks": frozenset(
        {
            "provider",
            "dataset_key",
            "scope_key",
            "requested_through_date",
            "source_observed_at",
            "latest_ingested_completed_game_date",
            "contiguous_regular_season_complete_through_date",
            "partial_date",
            "partial_date_reason",
            "source_stats_run_id",
            "revision",
            "updated_at",
            "source_checksum",
        }
    ),
}

_OPTIONAL_SCHEMA_TABLES = (
    "statcast_pitch_identities",
    "statcast_pitch_revisions",
    "stats_feature_snapshots",
    "stats_season_snapshots",
    "stats_checkpoints",
    "stats_raw_payload_metadata",
    "stats_player_identities",
    "stats_player_identifier_mappings",
)


class HistoricalPITSourceError(RuntimeError):
    """Raised when retained historical evidence cannot be trusted read-only."""


def _parse_date(value: date | str, field: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise HistoricalPITSourceError(
            f"{field} must be exact YYYY-MM-DD"
        ) from exc
    return parsed


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


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _historical_source_path(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise HistoricalPITSourceError("historical source database must not be a symlink")
    source = candidate.resolve()
    if not source.exists() or not source.is_file():
        raise HistoricalPITSourceError(f"historical source database not found: {source}")
    return source


@dataclass(frozen=True, slots=True)
class HistoricalProviderSeasonCoverageV1:
    provider: str
    season: int
    game_count: int
    first_game_date: date
    last_game_date: date
    scheduled_start_count: int

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise HistoricalPITSourceError("coverage provider must not be empty")
        if self.season <= 0 or self.game_count <= 0:
            raise HistoricalPITSourceError("coverage season/game_count must be positive")
        if self.first_game_date > self.last_game_date:
            raise HistoricalPITSourceError("coverage date range is inverted")
        if not 0 <= self.scheduled_start_count <= self.game_count:
            raise HistoricalPITSourceError("scheduled_start_count is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "first_game_date": self.first_game_date.isoformat(),
            "game_count": self.game_count,
            "last_game_date": self.last_game_date.isoformat(),
            "provider": self.provider,
            "scheduled_start_count": self.scheduled_start_count,
            "season": self.season,
        }


@dataclass(frozen=True, slots=True)
class HistoricalFinalCoverageV1:
    provider: str
    season: int
    final_game_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "final_game_count": self.final_game_count,
            "provider": self.provider,
            "season": self.season,
        }


@dataclass(frozen=True, slots=True)
class HistoricalStatcastPitchCoverageV1:
    season: int
    game_count: int
    pitch_identity_count: int
    first_game_date: date
    last_game_date: date

    def as_dict(self) -> dict[str, object]:
        return {
            "first_game_date": self.first_game_date.isoformat(),
            "game_count": self.game_count,
            "last_game_date": self.last_game_date.isoformat(),
            "pitch_identity_count": self.pitch_identity_count,
            "season": self.season,
        }


@dataclass(frozen=True, slots=True)
class HistoricalFeatureSnapshotCoverageV1:
    feature_version: str
    entity_kind: str
    snapshot_count: int
    first_feature_date: date | None
    last_feature_date: date | None

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_kind": self.entity_kind,
            "feature_version": self.feature_version,
            "first_feature_date": (
                self.first_feature_date.isoformat()
                if self.first_feature_date is not None
                else None
            ),
            "last_feature_date": (
                self.last_feature_date.isoformat()
                if self.last_feature_date is not None
                else None
            ),
            "snapshot_count": self.snapshot_count,
        }


@dataclass(frozen=True, slots=True)
class HistoricalCompletenessWatermarkV1:
    provider: str
    dataset_key: str
    scope_key: str
    requested_through_date: date
    latest_ingested_completed_game_date: date | None
    contiguous_regular_season_complete_through_date: date | None
    partial_date: date | None
    partial_date_reason: str | None
    source_stats_run_id: str
    revision: int
    source_checksum: str

    def as_dict(self) -> dict[str, object]:
        return {
            "contiguous_regular_season_complete_through_date": (
                self.contiguous_regular_season_complete_through_date.isoformat()
                if self.contiguous_regular_season_complete_through_date is not None
                else None
            ),
            "dataset_key": self.dataset_key,
            "latest_ingested_completed_game_date": (
                self.latest_ingested_completed_game_date.isoformat()
                if self.latest_ingested_completed_game_date is not None
                else None
            ),
            "partial_date": (
                self.partial_date.isoformat() if self.partial_date is not None else None
            ),
            "partial_date_reason": self.partial_date_reason,
            "provider": self.provider,
            "requested_through_date": self.requested_through_date.isoformat(),
            "revision": self.revision,
            "scope_key": self.scope_key,
            "source_checksum": self.source_checksum,
            "source_stats_run_id": self.source_stats_run_id,
        }


@dataclass(frozen=True, slots=True)
class HistoricalPITSourceInventoryV1:
    start_date: date
    end_date: date
    source_size_bytes: int
    source_schema_checksum: str
    provider_season_coverage: tuple[HistoricalProviderSeasonCoverageV1, ...]
    final_coverage: tuple[HistoricalFinalCoverageV1, ...]
    statcast_pitch_coverage: tuple[HistoricalStatcastPitchCoverageV1, ...]
    feature_snapshot_coverage: tuple[HistoricalFeatureSnapshotCoverageV1, ...]
    completeness_watermarks: tuple[HistoricalCompletenessWatermarkV1, ...]
    optional_tables_present: tuple[str, ...]
    contract_version: str = HISTORICAL_PIT_SOURCE_INVENTORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise HistoricalPITSourceError("inventory end_date precedes start_date")
        if self.source_size_bytes <= 0:
            raise HistoricalPITSourceError("historical source database is empty")
        if len(self.source_schema_checksum) != 64:
            raise HistoricalPITSourceError("source schema checksum is malformed")
        if self.contract_version != HISTORICAL_PIT_SOURCE_INVENTORY_CONTRACT_VERSION:
            raise HistoricalPITSourceError("unsupported PIT source inventory contract")

    @property
    def candidate_game_count(self) -> int:
        return sum(item.game_count for item in self.provider_season_coverage)

    @property
    def final_game_count(self) -> int:
        return sum(item.final_game_count for item in self.final_coverage)

    @property
    def statcast_pitch_game_count(self) -> int:
        return sum(item.game_count for item in self.statcast_pitch_coverage)

    @property
    def feature_snapshot_count(self) -> int:
        return sum(item.snapshot_count for item in self.feature_snapshot_coverage)

    def as_dict(self) -> dict[str, object]:
        return {
            "completeness_watermarks": [
                item.as_dict() for item in self.completeness_watermarks
            ],
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "feature_snapshot_coverage": [
                item.as_dict() for item in self.feature_snapshot_coverage
            ],
            "final_coverage": [item.as_dict() for item in self.final_coverage],
            "optional_tables_present": list(self.optional_tables_present),
            "provider_season_coverage": [
                item.as_dict() for item in self.provider_season_coverage
            ],
            "source_schema_checksum": self.source_schema_checksum,
            "source_size_bytes": self.source_size_bytes,
            "start_date": self.start_date.isoformat(),
            "statcast_pitch_coverage": [
                item.as_dict() for item in self.statcast_pitch_coverage
            ],
        }

    @property
    def checksum(self) -> str:
        return _sha(self.as_dict())

    def summary_as_dict(self) -> dict[str, object]:
        return {
            "candidate_game_count": self.candidate_game_count,
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "feature_snapshot_count": self.feature_snapshot_count,
            "final_game_count": self.final_game_count,
            "inventory_checksum": self.checksum,
            "optional_tables_present": list(self.optional_tables_present),
            "provider_season_coverage": [
                item.as_dict() for item in self.provider_season_coverage
            ],
            "source_schema_checksum": self.source_schema_checksum,
            "source_size_bytes": self.source_size_bytes,
            "start_date": self.start_date.isoformat(),
            "statcast_pitch_coverage": [
                item.as_dict() for item in self.statcast_pitch_coverage
            ],
            "statcast_pitch_game_count": self.statcast_pitch_game_count,
            "watermarks": [item.as_dict() for item in self.completeness_watermarks],
        }


@contextmanager
def open_historical_source_read_only(path: Path | str) -> Iterator[sqlite3.Connection]:
    source = _historical_source_path(path)
    uri_path = quote(source.as_posix(), safe="/:")
    try:
        connection = sqlite3.connect(
            f"file:{uri_path}?mode=ro",
            uri=True,
            timeout=30,
        )
    except sqlite3.Error as exc:
        raise HistoricalPITSourceError(
            "historical source database could not be opened read-only"
        ) from exc
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


def _validate_source_schema(connection: sqlite3.Connection) -> tuple[str, tuple[str, ...]]:
    tables = _table_names(connection)
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise HistoricalPITSourceError(
            "historical source is missing required table(s): "
            + ", ".join(missing_tables)
        )
    schema: dict[str, list[str]] = {}
    for table, required in _REQUIRED_COLUMNS.items():
        columns = _columns(connection, table)
        missing = sorted(required - set(columns))
        if missing:
            raise HistoricalPITSourceError(
                f"historical source table {table} is missing column(s): "
                + ", ".join(missing)
            )
        schema[table] = list(columns)
    optional_present = tuple(sorted(set(_OPTIONAL_SCHEMA_TABLES) & tables))
    for table in optional_present:
        schema[table] = list(_columns(connection, table))
    return _sha(schema), optional_present


def _provider_coverage(
    connection: sqlite3.Connection,
    start_date: date,
    end_date: date,
) -> tuple[HistoricalProviderSeasonCoverageV1, ...]:
    rows = connection.execute(
        """
        SELECT provider,season,COUNT(*) AS game_count,
               MIN(official_date) AS first_game_date,
               MAX(official_date) AS last_game_date,
               SUM(CASE WHEN scheduled_start IS NOT NULL THEN 1 ELSE 0 END)
                   AS scheduled_start_count
        FROM stats_game_identities
        WHERE game_type='R'
          AND official_date BETWEEN ? AND ?
        GROUP BY provider,season
        ORDER BY season,provider
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return tuple(
        HistoricalProviderSeasonCoverageV1(
            provider=str(row["provider"]),
            season=int(row["season"]),
            game_count=int(row["game_count"]),
            first_game_date=date.fromisoformat(str(row["first_game_date"])),
            last_game_date=date.fromisoformat(str(row["last_game_date"])),
            scheduled_start_count=int(row["scheduled_start_count"] or 0),
        )
        for row in rows
    )


def _final_coverage(
    connection: sqlite3.Connection,
    start_date: date,
    end_date: date,
) -> tuple[HistoricalFinalCoverageV1, ...]:
    rows = connection.execute(
        """
        SELECT game.provider,game.season,
               COUNT(DISTINCT status.game_identity_id) AS final_game_count
        FROM stats_game_status_observations AS status
        JOIN stats_game_identities AS game
          ON game.game_identity_id=status.game_identity_id
        WHERE game.game_type='R'
          AND game.official_date BETWEEN ? AND ?
          AND (
              LOWER(COALESCE(status.abstract_state,''))='final'
              OR LOWER(COALESCE(status.status_code,''))='final'
          )
        GROUP BY game.provider,game.season
        ORDER BY game.season,game.provider
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return tuple(
        HistoricalFinalCoverageV1(
            provider=str(row["provider"]),
            season=int(row["season"]),
            final_game_count=int(row["final_game_count"]),
        )
        for row in rows
    )


def _statcast_pitch_coverage(
    connection: sqlite3.Connection,
    tables: set[str],
    start_date: date,
    end_date: date,
) -> tuple[HistoricalStatcastPitchCoverageV1, ...]:
    if "statcast_pitch_identities" not in tables:
        return ()
    columns = set(_columns(connection, "statcast_pitch_identities"))
    if not {"game_identity_id", "pitch_identity_id"} <= columns:
        return ()
    rows = connection.execute(
        """
        SELECT game.season,
               COUNT(DISTINCT pitch.game_identity_id) AS game_count,
               COUNT(*) AS pitch_identity_count,
               MIN(game.official_date) AS first_game_date,
               MAX(game.official_date) AS last_game_date
        FROM statcast_pitch_identities AS pitch
        JOIN stats_game_identities AS game
          ON game.game_identity_id=pitch.game_identity_id
        WHERE game.provider='statcast'
          AND game.game_type='R'
          AND game.official_date BETWEEN ? AND ?
        GROUP BY game.season
        ORDER BY game.season
        """,
        (start_date.isoformat(), end_date.isoformat()),
    ).fetchall()
    return tuple(
        HistoricalStatcastPitchCoverageV1(
            season=int(row["season"]),
            game_count=int(row["game_count"]),
            pitch_identity_count=int(row["pitch_identity_count"]),
            first_game_date=date.fromisoformat(str(row["first_game_date"])),
            last_game_date=date.fromisoformat(str(row["last_game_date"])),
        )
        for row in rows
    )


def _feature_snapshot_coverage(
    connection: sqlite3.Connection,
    tables: set[str],
) -> tuple[HistoricalFeatureSnapshotCoverageV1, ...]:
    if "stats_feature_snapshots" not in tables:
        return ()
    columns = set(_columns(connection, "stats_feature_snapshots"))
    if not {"feature_version", "entity_kind", "feature_as_of"} <= columns:
        return ()
    rows = connection.execute(
        """
        SELECT feature_version,entity_kind,COUNT(*) AS snapshot_count,
               MIN(substr(feature_as_of,1,10)) AS first_feature_date,
               MAX(substr(feature_as_of,1,10)) AS last_feature_date
        FROM stats_feature_snapshots
        GROUP BY feature_version,entity_kind
        ORDER BY feature_version,entity_kind
        """
    ).fetchall()
    result: list[HistoricalFeatureSnapshotCoverageV1] = []
    for row in rows:
        first = _optional_text(row["first_feature_date"])
        last = _optional_text(row["last_feature_date"])
        result.append(
            HistoricalFeatureSnapshotCoverageV1(
                feature_version=str(row["feature_version"]),
                entity_kind=str(row["entity_kind"]),
                snapshot_count=int(row["snapshot_count"]),
                first_feature_date=date.fromisoformat(first) if first else None,
                last_feature_date=date.fromisoformat(last) if last else None,
            )
        )
    return tuple(result)


def _watermarks(
    connection: sqlite3.Connection,
) -> tuple[HistoricalCompletenessWatermarkV1, ...]:
    rows = connection.execute(
        """
        SELECT provider,dataset_key,scope_key,requested_through_date,
               latest_ingested_completed_game_date,
               contiguous_regular_season_complete_through_date,
               partial_date,partial_date_reason,source_stats_run_id,
               revision,source_checksum
        FROM stats_completeness_watermarks
        WHERE dataset_key='regular_season_games'
        ORDER BY provider,scope_key,revision
        """
    ).fetchall()
    result: list[HistoricalCompletenessWatermarkV1] = []
    for row in rows:
        requested = _optional_text(row["requested_through_date"])
        if requested is None:
            raise HistoricalPITSourceError("completeness watermark lacks requested date")
        latest = _optional_text(row["latest_ingested_completed_game_date"])
        contiguous = _optional_text(
            row["contiguous_regular_season_complete_through_date"]
        )
        partial = _optional_text(row["partial_date"])
        result.append(
            HistoricalCompletenessWatermarkV1(
                provider=str(row["provider"]),
                dataset_key=str(row["dataset_key"]),
                scope_key=str(row["scope_key"]),
                requested_through_date=date.fromisoformat(requested),
                latest_ingested_completed_game_date=(
                    date.fromisoformat(latest) if latest else None
                ),
                contiguous_regular_season_complete_through_date=(
                    date.fromisoformat(contiguous) if contiguous else None
                ),
                partial_date=date.fromisoformat(partial) if partial else None,
                partial_date_reason=_optional_text(row["partial_date_reason"]),
                source_stats_run_id=str(row["source_stats_run_id"]),
                revision=int(row["revision"]),
                source_checksum=str(row["source_checksum"]),
            )
        )
    return tuple(result)


def inventory_historical_pit_source(
    database_path: Path | str,
    *,
    start_date: date | str,
    end_date: date | str,
) -> HistoricalPITSourceInventoryV1:
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    if end < start:
        raise HistoricalPITSourceError("end_date must not precede start_date")
    source = _historical_source_path(database_path)
    size = source.stat().st_size
    with open_historical_source_read_only(source) as connection:
        schema_checksum, optional_present = _validate_source_schema(connection)
        tables = _table_names(connection)
        provider_coverage = _provider_coverage(connection, start, end)
        finals = _final_coverage(connection, start, end)
        pitch_coverage = _statcast_pitch_coverage(connection, tables, start, end)
        feature_coverage = _feature_snapshot_coverage(connection, tables)
        watermarks = _watermarks(connection)
    return HistoricalPITSourceInventoryV1(
        start_date=start,
        end_date=end,
        source_size_bytes=size,
        source_schema_checksum=schema_checksum,
        provider_season_coverage=provider_coverage,
        final_coverage=finals,
        statcast_pitch_coverage=pitch_coverage,
        feature_snapshot_coverage=feature_coverage,
        completeness_watermarks=watermarks,
        optional_tables_present=optional_present,
    )
