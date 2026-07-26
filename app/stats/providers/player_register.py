from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass

from app.stats.contracts import (
    RawArtifact,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsTransport,
)
from app.stats.providers._common import read_exact_capture, require_content_type


PYBASEBALL_REGISTER_COMMIT_SHA = "7e23e7dfaff51b3ae72c16393703eda7e5ecad27"
PYBASEBALL_REGISTER_URL = (
    "https://codeload.github.com/chadwickbureau/register/zip/"
    f"{PYBASEBALL_REGISTER_COMMIT_SHA}"
)
PYBASEBALL_REGISTER_ENDPOINT_CATEGORY = (
    f"player_register_{PYBASEBALL_REGISTER_COMMIT_SHA}"
)
PYBASEBALL_REGISTER_ADAPTER_VERSION = "DSE_PYBASEBALL_REGISTER_ADAPTER_V1"
_PEOPLE_MEMBER_RE = re.compile(r"^[^/]+/(?:data/)?people-[^/]+\.csv$")
_REQUIRED_COLUMNS = frozenset(
    {
        "key_retro",
        "key_mlbam",
        "key_bbref",
        "key_fangraphs",
        "name_last",
        "name_first",
    }
)


@dataclass(frozen=True, slots=True)
class PlayerRegisterMapping:
    retrosheet_id: str
    mlbam_id: int
    baseball_reference_id: str | None
    fangraphs_id: str | None
    first_name: str | None
    last_name: str | None
    source_member: str
    source_row_number: int


@dataclass(frozen=True, slots=True)
class PlayerRegisterDataset:
    mappings: tuple[PlayerRegisterMapping, ...]
    raw: RawArtifact
    source_member_count: int
    source_row_count: int

    @property
    def by_retrosheet_id(self) -> dict[str, PlayerRegisterMapping]:
        return {mapping.retrosheet_id: mapping for mapping in self.mappings}


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _positive_mlbam(value: object) -> int | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError("key_mlbam must be blank or a positive integer")
    return int(normalized)


class PybaseballPlayerRegisterProvider:
    """Raw-first exact identifier crosswalk used by pybaseball 2.2.7."""

    def __init__(
        self,
        transport: StatsTransport,
    ) -> None:
        self.transport = transport

    def collect(self) -> PlayerRegisterDataset:
        response = self.transport.fetch(
            StatsRequest(
                provider=StatsProvider.PYBASEBALL,
                endpoint_category=PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
                fixture_key="pybaseball_player_identifier_register",
                url=PYBASEBALL_REGISTER_URL,
                headers={"Accept": "application/zip, application/octet-stream"},
                timeout_seconds=60.0,
                max_attempts=3,
            )
        )
        return self.parse_artifact(response.capture)

    def parse_artifact(self, artifact: RawArtifact) -> PlayerRegisterDataset:
        payload = read_exact_capture(artifact)
        require_content_type(
            artifact,
            {
                "application/zip",
                "application/x-zip-compressed",
                "application/octet-stream",
            },
        )
        try:
            archive = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as exc:
            raise StatsProviderPayloadError(
                "pybaseball player register response is not a valid ZIP archive",
                capture=artifact,
            ) from exc

        mappings: dict[str, PlayerRegisterMapping] = {}
        source_rows = 0
        members = sorted(
            (
                member
                for member in archive.infolist()
                if not member.is_dir()
                and _PEOPLE_MEMBER_RE.fullmatch(member.filename)
            ),
            key=lambda member: member.filename,
        )
        if not members:
            raise StatsProviderPayloadError(
                "pybaseball player register contains no people CSV members",
                capture=artifact,
            )
        for member in members:
            try:
                text = archive.read(member).decode("utf-8-sig")
            except (KeyError, UnicodeDecodeError, RuntimeError) as exc:
                raise StatsProviderPayloadError(
                    "pybaseball player register member is unreadable",
                    capture=artifact,
                ) from exc
            reader = csv.DictReader(io.StringIO(text, newline=""))
            columns = tuple(reader.fieldnames or ())
            if len(columns) != len(set(columns)) or not _REQUIRED_COLUMNS.issubset(
                columns
            ):
                raise StatsProviderPayloadError(
                    "pybaseball player register schema drifted",
                    capture=artifact,
                )
            for row_number, row in enumerate(reader, start=2):
                source_rows += 1
                retrosheet_id = str(row.get("key_retro") or "").strip().lower()
                try:
                    mlbam_id = _positive_mlbam(row.get("key_mlbam"))
                except ValueError as exc:
                    raise StatsProviderPayloadError(
                        "pybaseball player register contains an invalid MLBAM identity",
                        capture=artifact,
                    ) from exc
                if not retrosheet_id or mlbam_id is None:
                    continue
                mapping = PlayerRegisterMapping(
                    retrosheet_id=retrosheet_id,
                    mlbam_id=mlbam_id,
                    baseball_reference_id=_optional_text(row.get("key_bbref")),
                    fangraphs_id=_optional_text(row.get("key_fangraphs")),
                    first_name=_optional_text(row.get("name_first")),
                    last_name=_optional_text(row.get("name_last")),
                    source_member=member.filename,
                    source_row_number=row_number,
                )
                prior = mappings.get(retrosheet_id)
                if prior is not None and prior != mapping:
                    raise StatsProviderPayloadError(
                        "pybaseball player register contains a conflicting Retrosheet identity",
                        capture=artifact,
                    )
                mappings[retrosheet_id] = mapping
        if not mappings:
            raise StatsProviderPayloadError(
                "pybaseball player register contained no exact Retrosheet-to-MLBAM mappings",
                capture=artifact,
            )
        return PlayerRegisterDataset(
            mappings=tuple(mappings[key] for key in sorted(mappings)),
            raw=artifact,
            source_member_count=len(members),
            source_row_count=source_rows,
        )
