from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.baseball_intelligence.contracts import BaseballIntelligenceGameV1
from app.data_quality.contracts import DataQualityDisposition, DataQualityGameV1
from app.daily_slate.contracts import (
    DailySlateGameV1,
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.game_state.contracts import GameStateGameV1
from app.identifiers import parse_requested_date
from app.odds_weather.contracts import OddsWeatherGameV1
from app.redaction import redact_value

MATCHUP_PACKET_CONTRACT_VERSION = "DSE_MATCHUP_PACKET_V1"
MATCHUP_PACKET_SPORT = "MLB"
MATCHUP_PACKET_LEAGUE = "MLB"


class MatchupPacketContractError(ValueError):
    """Raised when canonical MatchupPacket V1 evidence violates its contract."""


def _aware_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MatchupPacketContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise MatchupPacketContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return value


def _identity(game: object) -> tuple[str, str, str, str, str]:
    return (
        str(getattr(game, "edge_event_id")),
        str(getattr(game, "daily_mlb_game_id")),
        str(getattr(game, "source_game_id")),
        str(getattr(game, "away_team_id")),
        str(getattr(game, "home_team_id")),
    )


@dataclass(frozen=True, slots=True)
class MatchupPacketGameV1:
    schedule: DailySlateGameV1
    game_state: GameStateGameV1
    baseball_intelligence: BaseballIntelligenceGameV1
    odds_weather: OddsWeatherGameV1
    data_quality: DataQualityGameV1

    def __post_init__(self) -> None:
        if not isinstance(self.schedule, DailySlateGameV1):
            raise MatchupPacketContractError("schedule must be DailySlateGameV1")
        if not isinstance(self.game_state, GameStateGameV1):
            raise MatchupPacketContractError("game_state must be GameStateGameV1")
        if not isinstance(self.baseball_intelligence, BaseballIntelligenceGameV1):
            raise MatchupPacketContractError(
                "baseball_intelligence must be BaseballIntelligenceGameV1"
            )
        if not isinstance(self.odds_weather, OddsWeatherGameV1):
            raise MatchupPacketContractError("odds_weather must be OddsWeatherGameV1")
        if not isinstance(self.data_quality, DataQualityGameV1):
            raise MatchupPacketContractError("data_quality must be DataQualityGameV1")

        expected_identity = _identity(self.schedule)
        for name, game in (
            ("game_state", self.game_state),
            ("baseball_intelligence", self.baseball_intelligence),
            ("odds_weather", self.odds_weather),
            ("data_quality", self.data_quality),
        ):
            if _identity(game) != expected_identity:
                raise MatchupPacketContractError(
                    f"{name} identity/team tuple does not match DailySlate"
                )

        source_game_id = canonical_authoritative_game_id(self.schedule.source_game_id)
        if self.schedule.edge_event_id != edge_event_id(source_game_id):
            raise MatchupPacketContractError("schedule edge_event_id identity mismatch")
        if self.schedule.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise MatchupPacketContractError("schedule daily_mlb_game_id identity mismatch")

        if (
            self.baseball_intelligence.upstream_daily_slate_game_checksum
            != self.schedule.checksum
        ):
            raise MatchupPacketContractError(
                "Baseball Intelligence DailySlate row checksum mismatch"
            )
        if (
            self.baseball_intelligence.upstream_game_state_game_checksum
            != self.game_state.checksum
        ):
            raise MatchupPacketContractError(
                "Baseball Intelligence GameState row checksum mismatch"
            )
        if self.odds_weather.upstream_daily_slate_game_checksum != self.schedule.checksum:
            raise MatchupPacketContractError(
                "OddsWeather DailySlate row checksum mismatch"
            )
        if (
            self.odds_weather.upstream_baseball_intelligence_game_checksum
            != self.baseball_intelligence.checksum
        ):
            raise MatchupPacketContractError(
                "OddsWeather Baseball Intelligence row checksum mismatch"
            )
        if self.data_quality.upstream_daily_slate_game_checksum != self.schedule.checksum:
            raise MatchupPacketContractError(
                "DataQuality DailySlate row checksum mismatch"
            )
        if (
            self.data_quality.upstream_game_state_game_checksum
            != self.game_state.checksum
        ):
            raise MatchupPacketContractError(
                "DataQuality GameState row checksum mismatch"
            )
        if (
            self.data_quality.upstream_baseball_intelligence_game_checksum
            != self.baseball_intelligence.checksum
        ):
            raise MatchupPacketContractError(
                "DataQuality Baseball Intelligence row checksum mismatch"
            )
        if (
            self.data_quality.upstream_odds_weather_game_checksum
            != self.odds_weather.checksum
        ):
            raise MatchupPacketContractError(
                "DataQuality OddsWeather row checksum mismatch"
            )

        if self.data_quality.scheduled_start_time != self.schedule.scheduled_start_time:
            raise MatchupPacketContractError(
                "DataQuality scheduled_start_time does not match DailySlate"
            )
        if self.odds_weather.scheduled_start_time != self.schedule.scheduled_start_time:
            raise MatchupPacketContractError(
                "OddsWeather scheduled_start_time does not match DailySlate"
            )

    @property
    def edge_event_id(self) -> str:
        return self.schedule.edge_event_id

    @property
    def daily_mlb_game_id(self) -> str:
        return self.schedule.daily_mlb_game_id

    @property
    def source_game_id(self) -> str:
        return self.schedule.source_game_id

    @property
    def away_team_id(self) -> str:
        return self.schedule.away_team_id

    @property
    def home_team_id(self) -> str:
        return self.schedule.home_team_id

    @property
    def quality_disposition(self) -> DataQualityDisposition:
        return self.data_quality.disposition

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "baseball_intelligence": self.baseball_intelligence.as_dict(),
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "data_quality": self.data_quality.as_dict(),
            "edge_event_id": self.edge_event_id,
            "game_state": self.game_state.as_dict(),
            "home_team_id": self.home_team_id,
            "odds_weather": self.odds_weather.as_dict(),
            "quality_disposition": self.quality_disposition.value,
            "schedule": self.schedule.as_dict(),
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class MatchupPacketV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_daily_slate_checksum: str
    upstream_game_state_checksum: str
    upstream_baseball_intelligence_checksum: str
    upstream_odds_weather_checksum: str
    upstream_data_quality_checksum: str
    games: tuple[MatchupPacketGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = MATCHUP_PACKET_CONTRACT_VERSION
    sport: str = MATCHUP_PACKET_SPORT
    league: str = MATCHUP_PACKET_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "observed_at"),
        )
        for name in (
            "upstream_daily_slate_checksum",
            "upstream_game_state_checksum",
            "upstream_baseball_intelligence_checksum",
            "upstream_odds_weather_checksum",
            "upstream_data_quality_checksum",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.contract_version != MATCHUP_PACKET_CONTRACT_VERSION:
            raise MatchupPacketContractError("unsupported MatchupPacket contract_version")
        if self.sport != "MLB" or self.league != "MLB":
            raise MatchupPacketContractError("sport and league must both be MLB")

        games = tuple(self.games)
        if not all(isinstance(game, MatchupPacketGameV1) for game in games):
            raise MatchupPacketContractError(
                "games must contain MatchupPacketGameV1 values"
            )
        if len({game.source_game_id for game in games}) != len(games):
            raise MatchupPacketContractError("MatchupPacketV1 contains duplicate games")
        object.__setattr__(self, "games", games)

        payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(
            payload,
            configured,
            preserve_field_names=("key",),
        ) != payload:
            raise MatchupPacketContractError(
                "MatchupPacketV1 contains credential-bearing material"
            )

    @property
    def ready_game_count(self) -> int:
        return sum(
            game.quality_disposition is DataQualityDisposition.READY
            for game in self.games
        )

    @property
    def degraded_game_count(self) -> int:
        return sum(
            game.quality_disposition is DataQualityDisposition.DEGRADED
            for game in self.games
        )

    @property
    def insufficient_game_count(self) -> int:
        return sum(
            game.quality_disposition is DataQualityDisposition.INSUFFICIENT
            for game in self.games
        )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "degraded_game_count": self.degraded_game_count,
            "games": [game.as_dict() for game in self.games],
            "insufficient_game_count": self.insufficient_game_count,
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "ready_game_count": self.ready_game_count,
            "requested_date": self.requested_date,
            "sport": self.sport,
            "upstream_baseball_intelligence_checksum": self.upstream_baseball_intelligence_checksum,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_data_quality_checksum": self.upstream_data_quality_checksum,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
            "upstream_odds_weather_checksum": self.upstream_odds_weather_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
