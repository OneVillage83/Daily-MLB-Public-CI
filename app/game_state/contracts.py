from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from app.daily_slate.contracts import (
    AUTHORITATIVE_MLB_PROVIDER,
    DailySlateGameStatus,
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS

GAME_STATE_CONTRACT_VERSION = "DSE_GAME_STATE_V1"
GAME_STATE_NORMALIZATION_VERSION = "DSE_GAME_STATE_NORMALIZATION_V1"
GAME_STATE_SPORT = "MLB"
GAME_STATE_LEAGUE = "MLB"


class GameStateContractError(ValueError):
    """Raised when canonical GameStateV1 evidence violates its contract."""


class StarterCertainty(StrEnum):
    UNAVAILABLE = "unavailable"
    PROBABLE = "probable"
    ANNOUNCED = "announced"
    CONFIRMED = "confirmed"


class LineupAvailability(StrEnum):
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    POSTED = "posted"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GameStateContractError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _positive_decimal_id(value: object, name: str) -> str:
    if isinstance(value, bool):
        raise GameStateContractError(f"{name} must be a positive decimal identifier")
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, str):
        text = _required_text(value, name)
        if not text.isascii() or not text.isdecimal():
            raise GameStateContractError(f"{name} must be a positive decimal identifier")
        numeric = int(text, 10)
    else:
        raise GameStateContractError(f"{name} must be an integer or decimal string")
    if numeric <= 0:
        raise GameStateContractError(f"{name} must be a positive decimal identifier")
    return str(numeric)


def _aware_utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise GameStateContractError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise GameStateContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


@dataclass(frozen=True, slots=True)
class GameStateProvenanceV1:
    source_provider: str
    source_record_id: str | None
    observed_at: datetime
    source_version: str | None = None
    raw_status: str | None = None
    upstream_checksum: str | None = None
    normalization_version: str = GAME_STATE_NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        provider = _required_text(self.source_provider, "source_provider").casefold()
        if provider != AUTHORITATIVE_MLB_PROVIDER:
            raise GameStateContractError(
                f"source_provider must be authoritative provider {AUTHORITATIVE_MLB_PROVIDER!r}"
            )
        object.__setattr__(self, "source_provider", provider)
        if self.source_record_id is not None:
            object.__setattr__(
                self,
                "source_record_id",
                _positive_decimal_id(self.source_record_id, "source_record_id"),
            )
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "provenance observed_at"),
        )
        object.__setattr__(
            self,
            "source_version",
            _optional_text(self.source_version, "source_version"),
        )
        object.__setattr__(
            self,
            "raw_status",
            _optional_text(self.raw_status, "raw_status"),
        )
        if self.upstream_checksum is not None:
            object.__setattr__(
                self,
                "upstream_checksum",
                _sha256(self.upstream_checksum, "upstream_checksum"),
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
            "source_version": self.source_version,
            "upstream_checksum": self.upstream_checksum,
        }


@dataclass(frozen=True, slots=True)
class GameStatePlayerV1:
    source_player_id: str
    full_name: str
    player_identity_id: str | None = None
    canonical_player_id: str | None = None
    source_position_code: str | None = None
    source_position_name: str | None = None
    source_status_code: str | None = None
    source_status_description: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_player_id",
            _positive_decimal_id(self.source_player_id, "source_player_id"),
        )
        object.__setattr__(self, "full_name", _required_text(self.full_name, "full_name"))
        for name in ("player_identity_id", "canonical_player_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))
        if (self.player_identity_id is None) != (self.canonical_player_id is None):
            raise GameStateContractError(
                "resolved player identity requires both player_identity_id and canonical_player_id"
            )
        for name in (
            "source_position_code",
            "source_position_name",
            "source_status_code",
            "source_status_description",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _required_text(value, name))

    @property
    def resolved(self) -> bool:
        return self.canonical_player_id is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "canonical_player_id": self.canonical_player_id,
            "full_name": self.full_name,
            "player_identity_id": self.player_identity_id,
            "source_player_id": self.source_player_id,
            "source_position_code": self.source_position_code,
            "source_position_name": self.source_position_name,
            "source_status_code": self.source_status_code,
            "source_status_description": self.source_status_description,
        }


@dataclass(frozen=True, slots=True)
class StarterStateV1:
    certainty: StarterCertainty
    player: GameStatePlayerV1 | None = None
    source_designation: str | None = None

    def __post_init__(self) -> None:
        if self.certainty is StarterCertainty.UNAVAILABLE:
            if self.player is not None:
                raise GameStateContractError(
                    "unavailable starter state must not contain a player"
                )
        elif self.player is None:
            raise GameStateContractError(
                "probable, announced, and confirmed starter states require a player"
            )
        object.__setattr__(
            self,
            "source_designation",
            _optional_text(self.source_designation, "source_designation"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "certainty": self.certainty.value,
            "player": None if self.player is None else self.player.as_dict(),
            "source_designation": self.source_designation,
        }


@dataclass(frozen=True, slots=True)
class LineupEntryV1:
    player: GameStatePlayerV1
    batting_order_slot: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.batting_order_slot, bool)
            or not isinstance(self.batting_order_slot, int)
            or not 1 <= self.batting_order_slot <= 9
        ):
            raise GameStateContractError("batting_order_slot must be an integer from 1 through 9")

    def as_dict(self) -> dict[str, object]:
        return {
            "batting_order_slot": self.batting_order_slot,
            "player": self.player.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class LineupStateV1:
    availability: LineupAvailability
    entries: tuple[LineupEntryV1, ...] = ()

    def __post_init__(self) -> None:
        entries = tuple(self.entries)
        if not all(isinstance(entry, LineupEntryV1) for entry in entries):
            raise GameStateContractError("lineup entries must contain LineupEntryV1 values")
        entries = tuple(sorted(entries, key=lambda item: item.batting_order_slot))
        object.__setattr__(self, "entries", entries)
        slots = [entry.batting_order_slot for entry in entries]
        player_ids = [entry.player.source_player_id for entry in entries]
        if len(slots) != len(set(slots)):
            raise GameStateContractError("lineup batting-order slots must be unique")
        if len(player_ids) != len(set(player_ids)):
            raise GameStateContractError("lineup source player IDs must be unique")
        if self.availability is LineupAvailability.UNAVAILABLE and entries:
            raise GameStateContractError("unavailable lineup must not contain entries")
        if self.availability is LineupAvailability.PARTIAL and not 1 <= len(entries) <= 8:
            raise GameStateContractError("partial lineup must contain between one and eight entries")
        if self.availability is LineupAvailability.POSTED:
            if len(entries) != 9 or slots != list(range(1, 10)):
                raise GameStateContractError(
                    "posted lineup requires exactly the nine batting-order slots 1 through 9"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "entries": [entry.as_dict() for entry in self.entries],
        }


def _canonical_player_bucket(
    values: tuple[GameStatePlayerV1, ...],
    name: str,
) -> tuple[GameStatePlayerV1, ...]:
    players = tuple(values)
    if not all(isinstance(player, GameStatePlayerV1) for player in players):
        raise GameStateContractError(f"{name} must contain GameStatePlayerV1 values")
    players = tuple(sorted(players, key=lambda item: int(item.source_player_id)))
    identifiers = [player.source_player_id for player in players]
    if len(identifiers) != len(set(identifiers)):
        raise GameStateContractError(f"{name} source player IDs must be unique")
    return players


@dataclass(frozen=True, slots=True)
class GamedayPersonnelV1:
    available: bool
    batters: tuple[GameStatePlayerV1, ...] = ()
    pitchers: tuple[GameStatePlayerV1, ...] = ()
    bench: tuple[GameStatePlayerV1, ...] = ()
    bullpen: tuple[GameStatePlayerV1, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.available, bool):
            raise GameStateContractError("gameday personnel available must be boolean")
        for name in ("batters", "pitchers", "bench", "bullpen"):
            object.__setattr__(
                self,
                name,
                _canonical_player_bucket(getattr(self, name), name),
            )
        bucket_count = sum(
            len(getattr(self, name)) for name in ("batters", "pitchers", "bench", "bullpen")
        )
        if self.available and bucket_count == 0:
            raise GameStateContractError(
                "available gameday personnel must retain at least one source player"
            )
        if not self.available and bucket_count != 0:
            raise GameStateContractError(
                "unavailable gameday personnel must not contain source players"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "batters": [player.as_dict() for player in self.batters],
            "bench": [player.as_dict() for player in self.bench],
            "bullpen": [player.as_dict() for player in self.bullpen],
            "pitchers": [player.as_dict() for player in self.pitchers],
        }


@dataclass(frozen=True, slots=True)
class TeamGameStateV1:
    team_id: str
    source_team_id: str
    starter: StarterStateV1
    lineup: LineupStateV1
    personnel: GamedayPersonnelV1

    def __post_init__(self) -> None:
        if self.team_id not in CANONICAL_TEAM_KEYS:
            raise GameStateContractError("team_id is not a canonical MLB team ID")
        object.__setattr__(
            self,
            "source_team_id",
            _positive_decimal_id(self.source_team_id, "source_team_id"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "lineup": self.lineup.as_dict(),
            "personnel": self.personnel.as_dict(),
            "source_team_id": self.source_team_id,
            "starter": self.starter.as_dict(),
            "team_id": self.team_id,
        }


@dataclass(frozen=True, slots=True)
class GameStateGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    game_status: DailySlateGameStatus
    away: TeamGameStateV1
    home: TeamGameStateV1
    observed_at: datetime
    provenance: GameStateProvenanceV1

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise GameStateContractError(
                "edge_event_id must derive from the authoritative MLB game identifier"
            )
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise GameStateContractError(
                "daily_mlb_game_id must derive from the authoritative MLB game identifier"
            )
        if self.away_team_id not in CANONICAL_TEAM_KEYS:
            raise GameStateContractError("away_team_id is not a canonical MLB team ID")
        if self.home_team_id not in CANONICAL_TEAM_KEYS:
            raise GameStateContractError("home_team_id is not a canonical MLB team ID")
        if self.away_team_id == self.home_team_id:
            raise GameStateContractError("home and away canonical team IDs must differ")
        if self.away.team_id != self.away_team_id or self.home.team_id != self.home_team_id:
            raise GameStateContractError(
                "nested team game-state identities must match the canonical game teams"
            )
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "game observed_at"),
        )
        if self.provenance.source_record_id != self.source_game_id:
            raise GameStateContractError(
                "game provenance source_record_id must match source_game_id"
            )
        if self.provenance.observed_at != self.observed_at:
            raise GameStateContractError(
                "game provenance observed_at must match game observed_at"
            )
        semantic_payload = self._content_dict()
        if redact_value(semantic_payload) != semantic_payload:
            raise GameStateContractError(
                "GameStateGameV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "away": self.away.as_dict(),
            "away_team_id": self.away_team_id,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "edge_event_id": self.edge_event_id,
            "game_status": self.game_status.value,
            "home": self.home.as_dict(),
            "home_team_id": self.home_team_id,
            "observed_at": self.observed_at.isoformat(),
            "provenance": self.provenance.as_dict(),
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GameStateV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    source_authority: str
    source_version: str | None
    upstream_daily_slate_checksum: str
    games: tuple[GameStateGameV1, ...]
    provenance: GameStateProvenanceV1
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = GAME_STATE_CONTRACT_VERSION
    sport: str = GAME_STATE_SPORT
    league: str = GAME_STATE_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "observed_at"),
        )
        authority = _required_text(self.source_authority, "source_authority").casefold()
        if authority != AUTHORITATIVE_MLB_PROVIDER:
            raise GameStateContractError(
                f"source_authority must be authoritative provider {AUTHORITATIVE_MLB_PROVIDER!r}"
            )
        object.__setattr__(self, "source_authority", authority)
        object.__setattr__(
            self,
            "source_version",
            _optional_text(self.source_version, "source_version"),
        )
        object.__setattr__(
            self,
            "upstream_daily_slate_checksum",
            _sha256(self.upstream_daily_slate_checksum, "upstream_daily_slate_checksum"),
        )
        if self.contract_version != GAME_STATE_CONTRACT_VERSION:
            raise GameStateContractError(
                f"contract_version must be {GAME_STATE_CONTRACT_VERSION}"
            )
        if self.sport != GAME_STATE_SPORT or self.league != GAME_STATE_LEAGUE:
            raise GameStateContractError("GameStateV1 sport and league must both be MLB")
        games = tuple(self.games)
        if not all(isinstance(game, GameStateGameV1) for game in games):
            raise GameStateContractError("games must contain GameStateGameV1 values")
        object.__setattr__(self, "games", games)
        event_ids = [game.edge_event_id for game in games]
        game_ids = [game.daily_mlb_game_id for game in games]
        source_ids = [game.source_game_id for game in games]
        if len(event_ids) != len(set(event_ids)):
            raise GameStateContractError("duplicate edge_event_id in GameStateV1")
        if len(game_ids) != len(set(game_ids)):
            raise GameStateContractError("duplicate daily_mlb_game_id in GameStateV1")
        if len(source_ids) != len(set(source_ids)):
            raise GameStateContractError(
                "duplicate authoritative source game identifier in GameStateV1"
            )
        if any(game.observed_at > self.observed_at for game in games):
            raise GameStateContractError(
                "snapshot observed_at must be at least as recent as every game observation"
            )
        if self.provenance.source_provider != self.source_authority:
            raise GameStateContractError(
                "snapshot provenance provider must match source_authority"
            )
        if self.provenance.observed_at != self.observed_at:
            raise GameStateContractError(
                "snapshot provenance observed_at must match observed_at"
            )
        if self.provenance.upstream_checksum != self.upstream_daily_slate_checksum:
            raise GameStateContractError(
                "snapshot provenance upstream_checksum must match DailySlate checksum"
            )
        semantic_payload = self._content_dict()
        configured_secrets = tuple(str(value) for value in secret_values if str(value))
        if redact_value(semantic_payload, configured_secrets) != semantic_payload:
            raise GameStateContractError("GameStateV1 contains credential-bearing material")

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
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
