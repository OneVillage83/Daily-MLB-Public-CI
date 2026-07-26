from __future__ import annotations

import csv
import re
import zipfile
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.stats.contracts import (
    CsvRow,
    RawArtifact,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsTransport,
)
from app.stats.providers._common import (
    read_capture_prefix,
    reject_block_page,
    require_content_type,
    verify_capture_file,
)
from app.stats.raw_store import RawArtifactStore

RETROSHEET_REGULAR_SEASON_URL = "https://www.retrosheet.org/downloads/regular.zip"
RETROSHEET_ATTRIBUTION_VERSION = "DSE_RETROSHEET_ATTRIBUTION_V1"
RETROSHEET_ATTRIBUTION_TEXT = (
    "The information used here was obtained free of charge from and is copyrighted "
    "by Retrosheet. Interested parties may contact Retrosheet at 20 Sunset Rd., "
    "Newark, DE 19711."
)
RETROSHEET_SEVEN_MEMBERS = (
    "allplayers.csv",
    "gameinfo.csv",
    "teamstats.csv",
    "batting.csv",
    "pitching.csv",
    "fielding.csv",
    "plays.csv",
)
_REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "allplayers.csv": frozenset({"id", "team", "season"}),
        "gameinfo.csv": frozenset({"gid", "gametype"}),
        "teamstats.csv": frozenset({"gid", "team", "gametype"}),
        "batting.csv": frozenset({"gid", "id", "team", "gametype"}),
        "pitching.csv": frozenset({"gid", "id", "team", "gametype"}),
        "fielding.csv": frozenset({"gid", "id", "team", "gametype"}),
        "plays.csv": frozenset({"gid", "gametype"}),
    }
)
_REGULAR_SEASON_GAME_TYPES = frozenset({"regular"})


@dataclass(frozen=True, slots=True)
class RetrosheetSourceScope:
    competition_level: str
    season_type: str
    classification_source: str


MLB_REGULAR_SEASON_SCOPE = RetrosheetSourceScope(
    competition_level="major_league",
    season_type="regular_season",
    classification_source="retrosheet_regular_season_archive",
)


@dataclass(frozen=True, slots=True)
class RetrosheetCsvMember:
    name: str
    columns: tuple[str, ...]
    row_count: int
    normalized_row_count: int
    raw: RawArtifact
    season_row_counts: Mapping[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "season_row_counts",
            MappingProxyType(dict(sorted(self.season_row_counts.items()))),
        )

    def iter_rows(self) -> Iterator[CsvRow]:
        yield from _iter_member_rows(self.raw, self.name)

    def iter_normalized_rows(self) -> Iterator[CsvRow]:
        if self.name == "allplayers.csv":
            return
        yield from (
            row
            for row in self.iter_rows()
            if row.get("gametype", "").strip().casefold()
            in _REGULAR_SEASON_GAME_TYPES
        )

    @property
    def rows(self) -> tuple[CsvRow, ...]:
        return tuple(self.iter_rows())

    @property
    def normalized_rows(self) -> tuple[CsvRow, ...]:
        return tuple(self.iter_normalized_rows())


@dataclass(frozen=True, slots=True)
class RetrosheetDataset:
    scope: RetrosheetSourceScope
    archive: RawArtifact
    members: Mapping[str, RetrosheetCsvMember]
    response_headers: Mapping[str, str]
    attempts: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", MappingProxyType(dict(self.members)))
        object.__setattr__(
            self, "response_headers", MappingProxyType(dict(self.response_headers))
        )


class RetrosheetProvider:
    def __init__(
        self,
        transport: StatsTransport,
        raw_store: RawArtifactStore,
        *,
        url: str = RETROSHEET_REGULAR_SEASON_URL,
    ) -> None:
        self.transport = transport
        self.raw_store = raw_store
        self.url = url

    def collect_regular_season_major_league(self) -> RetrosheetDataset:
        response = self.transport.fetch(
            StatsRequest(
                provider=StatsProvider.RETROSHEET,
                endpoint_category="regular_season_archive",
                fixture_key="retrosheet_regular_season_archive",
                url=self.url,
                headers={
                    "Accept": "application/zip, application/octet-stream;q=0.9",
                },
                timeout_seconds=120.0,
                max_attempts=3,
            )
        )
        archive_prefix = read_capture_prefix(response.capture)
        reject_block_page(archive_prefix, response.capture)
        require_content_type(
            response.capture,
            {
                "application/zip",
                "application/x-zip-compressed",
                "application/octet-stream",
            },
        )

        try:
            with zipfile.ZipFile(response.capture.path) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                names = [item.filename.replace("\\", "/") for item in files]
                if any("/" in name or name in {"", ".", ".."} for name in names):
                    raise StatsProviderPayloadError(
                        "Retrosheet archive members must be root-level files",
                        capture=response.capture,
                    )
                if len(names) != len(set(names)):
                    raise StatsProviderPayloadError(
                        "Retrosheet archive contains duplicate member names",
                        capture=response.capture,
                    )
                expected = set(RETROSHEET_SEVEN_MEMBERS)
                actual = set(names)
                if actual != expected or len(files) != len(RETROSHEET_SEVEN_MEMBERS):
                    missing = sorted(expected - actual)
                    unexpected = sorted(actual - expected)
                    raise StatsProviderPayloadError(
                        "Retrosheet archive does not match the seven-member contract "
                        f"(missing={missing}, unexpected={unexpected})",
                        capture=response.capture,
                    )

                member_artifacts: dict[str, RawArtifact] = {}
                for name in RETROSHEET_SEVEN_MEMBERS:
                    member_info = next(item for item in files if item.filename == name)
                    if member_info.flag_bits & 0x1:
                        raise StatsProviderPayloadError(
                            f"Retrosheet member {name} is encrypted",
                            capture=response.capture,
                        )
                    with archive.open(member_info, "r") as member_handle:
                        member_artifacts[name] = self.raw_store.retain_stream(
                            provider=StatsProvider.RETROSHEET,
                            endpoint_category=f"regular_season_{name.removesuffix('.csv')}",
                            chunks=iter(lambda: member_handle.read(1024 * 1024), b""),
                            retrieved_at=response.capture.retrieved_at,
                            content_type="text/csv",
                            parent_checksum_sha256=response.capture.checksum_sha256,
                        )
        except StatsProviderPayloadError:
            raise
        except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
            raise StatsProviderPayloadError(
                "Retrosheet response is not a readable ZIP archive",
                capture=response.capture,
            ) from exc

        parsed_members: dict[str, RetrosheetCsvMember] = {}
        for name in RETROSHEET_SEVEN_MEMBERS:
            artifact = member_artifacts[name]
            columns = _member_columns(artifact, name)
            missing_columns = _REQUIRED_COLUMNS[name] - set(columns)
            if missing_columns:
                raise StatsProviderPayloadError(
                    f"{name} is missing required columns {sorted(missing_columns)}",
                    capture=artifact,
                )
            row_count = 0
            normalized_row_count = 0
            season_row_counts: Counter[int] = Counter()
            for row in _iter_member_rows(artifact, name):
                row_count += 1
                season = _member_row_season(row, name, capture=artifact)
                season_row_counts[season] += 1
                if (
                    name != "allplayers.csv"
                    and row.get("gametype", "").strip().casefold()
                    in _REGULAR_SEASON_GAME_TYPES
                ):
                    normalized_row_count += 1
            parsed_members[name] = RetrosheetCsvMember(
                name=name,
                columns=columns,
                row_count=row_count,
                normalized_row_count=normalized_row_count,
                raw=artifact,
                season_row_counts=season_row_counts,
            )

        return RetrosheetDataset(
            scope=MLB_REGULAR_SEASON_SCOPE,
            archive=response.capture,
            members=parsed_members,
            response_headers=response.headers,
            attempts=response.attempts,
        )


_GAME_ID_RE = re.compile(r"^[A-Z0-9]{3}(?P<season>[0-9]{4})[0-9]{5}$")


def _member_row_season(
    row: Mapping[str, str], member_name: str, *, capture: RawArtifact
) -> int:
    if member_name == "allplayers.csv":
        raw_season = row.get("season", "").strip()
        if not re.fullmatch(r"[0-9]{4}", raw_season):
            raise StatsProviderPayloadError(
                "allplayers.csv contains an invalid season value", capture=capture
            )
        return int(raw_season)
    raw_game_id = row.get("gid", "").strip().upper()
    match = _GAME_ID_RE.fullmatch(raw_game_id)
    if match is None:
        raise StatsProviderPayloadError(
            f"{member_name} contains an invalid Retrosheet game ID", capture=capture
        )
    return int(match.group("season"))


def _member_columns(artifact: RawArtifact, member_name: str) -> tuple[str, ...]:
    try:
        with artifact.path.open("r", encoding="utf-8-sig", newline="") as handle:
            fieldnames = csv.DictReader(handle, strict=True).fieldnames
    except (UnicodeDecodeError, csv.Error) as exc:
        raise StatsProviderPayloadError(
            f"{member_name} is not valid UTF-8 CSV", capture=artifact
        ) from exc
    if fieldnames is None or not fieldnames:
        raise StatsProviderPayloadError(
            f"{member_name} has no CSV header", capture=artifact
        )
    columns = tuple(value.strip() for value in fieldnames)
    if any(not value for value in columns) or len(columns) != len(set(columns)):
        raise StatsProviderPayloadError(
            f"{member_name} has blank or duplicate CSV columns", capture=artifact
        )
    return columns


def _iter_member_rows(
    artifact: RawArtifact,
    member_name: str,
) -> Iterator[CsvRow]:
    verify_capture_file(artifact)
    columns = _member_columns(artifact, member_name)
    try:
        with artifact.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise StatsProviderPayloadError(
                    f"{member_name} has no CSV header", capture=artifact
                )
            for line_number, row in enumerate(reader, start=2):
                if None in row:
                    raise StatsProviderPayloadError(
                        f"{member_name} row {line_number} has more values than columns",
                        capture=artifact,
                    )
                if any(value is None for value in row.values()):
                    raise StatsProviderPayloadError(
                        f"{member_name} row {line_number} has fewer values than columns",
                        capture=artifact,
                    )
                yield {
                    columns[index]: str(row[fieldnames[index]])
                    for index in range(len(fieldnames))
                }
    except (UnicodeDecodeError, csv.Error) as exc:
        raise StatsProviderPayloadError(
            f"{member_name} is malformed CSV", capture=artifact
        ) from exc
