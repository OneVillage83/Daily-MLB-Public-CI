from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from app.identifiers import parse_requested_date
from app.redaction import redact_value
from app.stadiums import load_stadiums
from app.team_aliases import CANONICAL_TEAM_KEYS


DAILY_SLATE_CONTRACT_VERSION = "DSE_DAILY_SLATE_V1"
DAILY_SLATE_NORMALIZATION_VERSION = "DSE_DAILY_SLATE_NORMALIZATION_V1"
DAILY_SLATE_SPORT = "MLB"
DAILY_SLATE_LEAGUE = "MLB"
AUTHORITATIVE_MLB_PROVIDER = "mlb"

_IDENTIFIER_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class DailySlateContractError(ValueError):
    """Raised when canonical DailySlateV1 evidence violates its contract."""


class DailySlateGameStatus(StrEnum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    IN_PROGRESS = "in_progress"
    DELAYED = "delayed"
    POSTPONED = "postponed"
    SUSPENDED = "suspended"
    FINAL = "final"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class DailySlateDoubleheaderStatus(StrEnum):
    SINGLE = "single"
    DOUBLEHEADER = "doubleheader"
    UNKNOWN = "unknown"


class VenueMappingStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ProbableStarterClassification(StrEnum):
    AUTHORITATIVE_PROBABLE = "authoritative_probable"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DailySlateContractError(f"{name} must be a non-empty trimmed string")
    return value


def _identifier_segment(value: object, name: str) -> str:
    text = _required_text(value, name)
    if _IDENTIFIER_SEGMENT.fullmatch(text) is None:
        raise DailySlateContractError(
            f"{name} must contain only letters, digits, periods, underscores, or hyphens"
        )
    return text


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DailySlateContractError(f"{name} must be a timezone-aware datetime")
    if value.utcoffset() is None:
        raise DailySlateContractError(f"{name} must have a valid UTC offset")
    return value.astimezone(timezone.utc)


def _optional_aware_utc(value: object, name: str) -> datetime | None:
    return None if value is None else _aware_utc(value, name)


def _optional_checksum(value: object, name: str) -> str | None:
    if value is None:
        return None
    text = _required_text(value, name)
    if _SHA256.fullmatch(text) is None:
        raise DailySlateContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


def _iso_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_authoritative_game_id(authoritative_game_id: int | str) -> str:
    if isinstance(authoritative_game_id, bool):
        raise DailySlateContractError(
            "authoritative_game_id must be a positive decimal MLB game identifier"
        )
    if isinstance(authoritative_game_id, int):
        numeric = authoritative_game_id
    elif isinstance(authoritative_game_id, str):
        text = _required_text(authoritative_game_id, "authoritative_game_id")
        if not text.isascii() or not text.isdecimal():
            raise DailySlateContractError(
                "authoritative_game_id must be a positive decimal MLB game identifier"
            )
        numeric = int(text, 10)
    else:
        raise DailySlateContractError(
            "authoritative_game_id must be an integer or decimal string"
        )
    if numeric <= 0:
        raise DailySlateContractError(
            "authoritative_game_id must be a positive decimal MLB game identifier"
        )
    return str(numeric)


def daily_mlb_game_id(authoritative_game_id: int | str) -> str:
    source_id = canonical_authoritative_game_id(authoritative_game_id)
    return f"game:{AUTHORITATIVE_MLB_PROVIDER}:{source_id}"


def edge_event_id(authoritative_game_id: int | str) -> str:
    source_id = canonical_authoritative_game_id(authoritative_game_id)
    return f"edge:mlb:{source_id}"


@dataclass(frozen=True, slots=True)
class DailySlateProvenanceV1:
    source_provider: str
    source_record_id: str | None
    observed_at: datetime
    source_updated_at: datetime | None = None
    source_version: str | None = None
    raw_status: str | None = None
    upstream_checksum: str | None = None
    normalization_version: str = DAILY_SLATE_NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_provider",
            _identifier_segment(self.source_provider, "source_provider").lower(),
        )
        if self.source_record_id is not None:
            object.__setattr__(
                self,
                "source_record_id",
                _identifier_segment(self.source_record_id, "source_record_id"),
            )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self,
            "source_updated_at",
            _optional_aware_utc(self.source_updated_at, "source_updated_at"),
        )
        if self.source_version is not None:
            object.__setattr__(
                self,
                "source_version",
                _required_text(self.source_version, "source_version"),
            )
        if self.raw_status is not None:
            object.__setattr__(
                self, "raw_status", _required_text(self.raw_status, "raw_status")
            )
        object.__setattr__(
            self,
            "upstream_checksum",
            _optional_checksum(self.upstream_checksum, "upstream_checksum"),
        )
        object.__setattr__(
            self,
            "normalization_version",
            _required_text(self.normalization_version, "normalization_version"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "normalization_version": self.normalization_version,
            "observed_at": self.observed_at.isoformat(),
            "raw_status": self.raw_status,
            "source_provider": self.source_provider,
            "source_record_id": self.source_record_id,
            "source_updated_at": _iso_datetime(self.source_updated_at),
            "source_version": self.source_version,
            "upstream_checksum": self.upstream_checksum,
        }


@dataclass(frozen=True, slots=True)
class ProbableStarterV1:
    source_player_id: str
    full_name: str
    source_provider: str
    observed_at: datetime
    player_identity_id: str | None = None
    canonical_player_id: str | None = None
    classification: ProbableStarterClassification = (
        ProbableStarterClassification.AUTHORITATIVE_PROBABLE
    )
    provenance: DailySlateProvenanceV1 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_player_id",
            _identifier_segment(self.source_player_id, "source_player_id"),
        )
        object.__setattr__(self, "full_name", _required_text(self.full_name, "full_name"))
        object.__setattr__(
            self,
            "source_provider",
            _identifier_segment(self.source_provider, "source_provider").lower(),
        )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        for field_name in ("player_identity_id", "canonical_player_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _required_text(value, field_name))
        if (self.player_identity_id is None) != (self.canonical_player_id is None):
            raise DailySlateContractError(
                "resolved probable starter identity requires both player identity IDs"
            )
        if self.provenance is not None:
            if self.provenance.source_provider != self.source_provider:
                raise DailySlateContractError(
                    "probable starter provenance provider must match source_provider"
                )
            if self.provenance.observed_at != self.observed_at:
                raise DailySlateContractError(
                    "probable starter provenance observed_at must match observed_at"
                )

    @property
    def resolved(self) -> bool:
        return self.canonical_player_id is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "classification": self.classification.value,
            "full_name": self.full_name,
            "observed_at": self.observed_at.isoformat(),
            "player_identity_id": self.player_identity_id,
            "provenance": (
                None if self.provenance is None else self.provenance.as_dict()
            ),
            "source_player_id": self.source_player_id,
            "source_provider": self.source_provider,
        }


@dataclass(frozen=True, slots=True)
class DailySlateGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    official_date: str
    scheduled_start_time: datetime | None
    away_team_id: str
    home_team_id: str
    venue_id: str | None
    venue_mapping_status: VenueMappingStatus
    game_number: int | None
    doubleheader_status: DailySlateDoubleheaderStatus
    game_status: DailySlateGameStatus
    source_game_id: str
    source_provider: str
    observed_at: datetime
    provenance: DailySlateProvenanceV1
    source_home_team_id: str | None = None
    source_away_team_id: str | None = None
    source_venue_id: str | None = None
    source_venue_name: str | None = None
    source_updated_at: datetime | None = None
    away_probable_starter: ProbableStarterV1 | None = None
    home_probable_starter: ProbableStarterV1 | None = None

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        expected_event_id = edge_event_id(source_game_id)
        expected_game_id = daily_mlb_game_id(source_game_id)
        if self.edge_event_id != expected_event_id:
            raise DailySlateContractError(
                "edge_event_id must derive from the authoritative MLB game identifier"
            )
        if self.daily_mlb_game_id != expected_game_id:
            raise DailySlateContractError(
                "daily_mlb_game_id must derive from the authoritative MLB game identifier"
            )
        parse_requested_date(self.official_date)
        object.__setattr__(
            self,
            "scheduled_start_time",
            _optional_aware_utc(self.scheduled_start_time, "scheduled_start_time"),
        )
        if self.away_team_id not in CANONICAL_TEAM_KEYS:
            raise DailySlateContractError("away_team_id is not a canonical MLB team ID")
        if self.home_team_id not in CANONICAL_TEAM_KEYS:
            raise DailySlateContractError("home_team_id is not a canonical MLB team ID")
        if self.away_team_id == self.home_team_id:
            raise DailySlateContractError("home and away canonical team IDs must differ")
        if self.venue_mapping_status is VenueMappingStatus.RESOLVED:
            if self.venue_id is None:
                raise DailySlateContractError(
                    "resolved venue mapping requires a canonical venue_id"
                )
            object.__setattr__(
                self, "venue_id", _identifier_segment(self.venue_id, "venue_id")
            )
            known_venue_ids = {
                str(record["physical_venue_key"])
                for record in load_stadiums().values()
            }
            if self.venue_id not in known_venue_ids:
                raise DailySlateContractError(
                    "venue_id is not an existing canonical physical venue identity"
                )
        elif self.venue_id is not None:
            raise DailySlateContractError(
                "unresolved venue mapping must not claim a canonical venue_id"
            )
        if self.venue_mapping_status is VenueMappingStatus.UNRESOLVED and not (
            self.source_venue_id or self.source_venue_name
        ):
            raise DailySlateContractError(
                "unresolved venue mapping must retain source venue evidence"
            )
        if self.game_number is not None and (
            isinstance(self.game_number, bool)
            or not isinstance(self.game_number, int)
            or self.game_number < 1
        ):
            raise DailySlateContractError("game_number must be a positive integer")
        object.__setattr__(
            self,
            "source_provider",
            _identifier_segment(self.source_provider, "source_provider").lower(),
        )
        if self.source_provider != AUTHORITATIVE_MLB_PROVIDER:
            raise DailySlateContractError(
                f"source_provider must be authoritative provider "
                f"{AUTHORITATIVE_MLB_PROVIDER!r}"
            )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self,
            "source_updated_at",
            _optional_aware_utc(self.source_updated_at, "source_updated_at"),
        )
        for field_name in (
            "source_home_team_id",
            "source_away_team_id",
            "source_venue_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, _identifier_segment(value, field_name)
                )
        if self.source_venue_name is not None:
            object.__setattr__(
                self,
                "source_venue_name",
                _required_text(self.source_venue_name, "source_venue_name"),
            )
        if self.provenance.source_provider != self.source_provider:
            raise DailySlateContractError(
                "game provenance provider must match source_provider"
            )
        if self.provenance.source_record_id != self.source_game_id:
            raise DailySlateContractError(
                "game provenance source_record_id must match source_game_id"
            )
        if self.provenance.observed_at != self.observed_at:
            raise DailySlateContractError(
                "game provenance observed_at must match observed_at"
            )
        semantic_payload = self._content_dict()
        if redact_value(semantic_payload) != semantic_payload:
            raise DailySlateContractError(
                "DailySlateGameV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_probable_starter": (
                None
                if self.away_probable_starter is None
                else self.away_probable_starter.as_dict()
            ),
            "away_team_id": self.away_team_id,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "doubleheader_status": self.doubleheader_status.value,
            "edge_event_id": self.edge_event_id,
            "game_number": self.game_number,
            "game_status": self.game_status.value,
            "home_probable_starter": (
                None
                if self.home_probable_starter is None
                else self.home_probable_starter.as_dict()
            ),
            "home_team_id": self.home_team_id,
            "observed_at": self.observed_at.isoformat(),
            "official_date": self.official_date,
            "provenance": self.provenance.as_dict(),
            "scheduled_start_time": _iso_datetime(self.scheduled_start_time),
            "source_away_team_id": self.source_away_team_id,
            "source_game_id": self.source_game_id,
            "source_home_team_id": self.source_home_team_id,
            "source_provider": self.source_provider,
            "source_updated_at": _iso_datetime(self.source_updated_at),
            "source_venue_id": self.source_venue_id,
            "source_venue_name": self.source_venue_name,
            "venue_id": self.venue_id,
            "venue_mapping_status": self.venue_mapping_status.value,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


def _game_order(game: DailySlateGameV1) -> tuple[str, str]:
    start = (
        game.scheduled_start_time.isoformat()
        if game.scheduled_start_time is not None
        else "9999-12-31T23:59:59.999999+00:00"
    )
    return start, game.daily_mlb_game_id


@dataclass(frozen=True, slots=True)
class DailySlateV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    source_authority: str
    source_version: str | None
    games: tuple[DailySlateGameV1, ...]
    provenance: DailySlateProvenanceV1
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = DAILY_SLATE_CONTRACT_VERSION
    sport: str = DAILY_SLATE_SPORT
    league: str = DAILY_SLATE_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(
            self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time")
        )
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        object.__setattr__(
            self,
            "source_authority",
            _identifier_segment(self.source_authority, "source_authority").lower(),
        )
        if self.source_authority != AUTHORITATIVE_MLB_PROVIDER:
            raise DailySlateContractError(
                f"source_authority must be authoritative provider "
                f"{AUTHORITATIVE_MLB_PROVIDER!r}"
            )
        if self.source_version is not None:
            object.__setattr__(
                self,
                "source_version",
                _required_text(self.source_version, "source_version"),
            )
        if self.contract_version != DAILY_SLATE_CONTRACT_VERSION:
            raise DailySlateContractError(
                f"contract_version must be {DAILY_SLATE_CONTRACT_VERSION}"
            )
        if self.sport != DAILY_SLATE_SPORT or self.league != DAILY_SLATE_LEAGUE:
            raise DailySlateContractError("DailySlateV1 sport and league must both be MLB")
        canonical_games = tuple(sorted(tuple(self.games), key=_game_order))
        if not all(isinstance(game, DailySlateGameV1) for game in canonical_games):
            raise DailySlateContractError("games must contain DailySlateGameV1 values")
        object.__setattr__(self, "games", canonical_games)
        event_ids = [game.edge_event_id for game in canonical_games]
        game_ids = [game.daily_mlb_game_id for game in canonical_games]
        source_ids = [game.source_game_id for game in canonical_games]
        if len(event_ids) != len(set(event_ids)):
            raise DailySlateContractError("duplicate edge_event_id in DailySlateV1")
        if len(game_ids) != len(set(game_ids)):
            raise DailySlateContractError("duplicate daily_mlb_game_id in DailySlateV1")
        if len(source_ids) != len(set(source_ids)):
            raise DailySlateContractError(
                "duplicate authoritative source game identifier in DailySlateV1"
            )
        if any(game.source_provider != self.source_authority for game in canonical_games):
            raise DailySlateContractError(
                "every game source_provider must match source_authority"
            )
        if self.provenance.source_provider != self.source_authority:
            raise DailySlateContractError(
                "slate provenance provider must match source_authority"
            )
        if self.provenance.observed_at != self.observed_at:
            raise DailySlateContractError(
                "slate provenance observed_at must match observed_at"
            )
        semantic_payload = self._content_dict()
        configured_secrets = tuple(str(value) for value in secret_values if str(value))
        if redact_value(semantic_payload, configured_secrets) != semantic_payload:
            raise DailySlateContractError(
                "DailySlateV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "provenance": self.provenance.as_dict(),
            "requested_date": self.requested_date,
            "source_authority": self.source_authority,
            "source_version": self.source_version,
            "sport": self.sport,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
