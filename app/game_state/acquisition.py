from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, TypeAlias

from app.daily_slate.contracts import (
    AUTHORITATIVE_MLB_PROVIDER,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateV1,
)
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateContractError,
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupEntryV1,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)
from app.stats.contracts import StatsProvider, StatsRequest, StatsResponse, StatsTransport
from app.stats.raw_store import RawArtifactStore, RawStoreError

MLB_GAME_FEED_URL_TEMPLATE = "https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
MLB_GAME_FEED_ENDPOINT_CATEGORY = "game_state_feed"
MLB_GAME_FEED_SOURCE_VERSION = "statsapi-game-feed-v1.1"
MLB_GAME_FEED_FIXTURE_PREFIX = "mlb_game_state_feed"

PlayerIdentityResolution: TypeAlias = tuple[str, str]
PlayerIdentityResolver: TypeAlias = Callable[[str], PlayerIdentityResolution | None]


class GameStateAcquisitionError(RuntimeError):
    """Raised when authoritative MLB game-state evidence cannot be acquired safely."""


class GameStateNormalizationError(GameStateAcquisitionError):
    """Raised when retained MLB game-state evidence cannot normalize losslessly."""


class GameStateWarningCode(StrEnum):
    UNKNOWN_GAME_STATUS = "unknown_game_status"
    STARTER_UNAVAILABLE = "starter_unavailable"
    STARTER_PROBABLE_CHANGED = "starter_probable_changed"
    UNRESOLVED_STARTER_IDENTITY = "unresolved_starter_identity"
    LINEUP_UNAVAILABLE = "lineup_unavailable"
    LINEUP_PARTIAL = "lineup_partial"
    UNRESOLVED_LINEUP_IDENTITIES = "unresolved_lineup_identities"
    PERSONNEL_UNAVAILABLE = "personnel_unavailable"


@dataclass(frozen=True, slots=True)
class GameStateWarningV1:
    code: GameStateWarningCode
    message: str
    source_game_id: str
    team_id: str | None = None
    unresolved_count: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "source_game_id": self.source_game_id,
            "team_id": self.team_id,
            "unresolved_count": self.unresolved_count,
        }


@dataclass(frozen=True, slots=True)
class MlbGameFeedEvidenceV1:
    request: StatsRequest
    response: StatsResponse
    payload: Mapping[str, Any]

    @property
    def source_game_id(self) -> str:
        suffix = self.request.fixture_key.removeprefix(f"{MLB_GAME_FEED_FIXTURE_PREFIX}:")
        return _positive_decimal_id(suffix, "fixture source game ID")

    @property
    def observed_at(self) -> datetime:
        return self.response.capture.retrieved_at

    @property
    def raw_checksum(self) -> str:
        return self.response.capture.checksum_sha256


@dataclass(frozen=True, slots=True)
class GameStateNormalizationResultV1:
    state: GameStateV1
    warnings: tuple[GameStateWarningV1, ...]

    def warning_payload(self) -> list[dict[str, object]]:
        return [warning.as_dict() for warning in self.warnings]


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GameStateNormalizationError(f"{field_name} must be an object")
    return value


def _optional_mapping(value: object, field_name: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _mapping(value, field_name)


def _list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise GameStateNormalizationError(f"{field_name} must be a list")
    return value


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GameStateNormalizationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _positive_decimal_id(value: object, field_name: str) -> str:
    if isinstance(value, bool):
        raise GameStateNormalizationError(f"{field_name} must be a positive decimal ID")
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        numeric = int(value, 10)
    else:
        raise GameStateNormalizationError(f"{field_name} must be a positive decimal ID")
    if numeric <= 0:
        raise GameStateNormalizationError(f"{field_name} must be a positive decimal ID")
    return str(numeric)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GameStateNormalizationError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def mlb_game_feed_fixture_key(source_game_id: int | str) -> str:
    game_id = _positive_decimal_id(source_game_id, "source_game_id")
    return f"{MLB_GAME_FEED_FIXTURE_PREFIX}:{game_id}"


def build_mlb_game_feed_request(
    source_game_id: int | str,
    *,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> StatsRequest:
    game_id = _positive_decimal_id(source_game_id, "source_game_id")
    return StatsRequest(
        provider=StatsProvider.MLB,
        endpoint_category=MLB_GAME_FEED_ENDPOINT_CATEGORY,
        url=MLB_GAME_FEED_URL_TEMPLATE.format(game_pk=game_id),
        fixture_key=mlb_game_feed_fixture_key(game_id),
        headers={"Accept": "application/json"},
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        minimum_interval_seconds=0.0,
        persistent_cache=False,
    )


def acquire_mlb_game_feed(
    *,
    transport: StatsTransport,
    raw_store: RawArtifactStore,
    source_game_id: int | str,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> MlbGameFeedEvidenceV1:
    request = build_mlb_game_feed_request(
        source_game_id,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )
    response = transport.fetch(request)
    if response.status_code != 200:
        raise GameStateAcquisitionError(
            f"authoritative MLB game feed returned unexpected HTTP {response.status_code}"
        )
    if response.capture.provider is not StatsProvider.MLB:
        raise GameStateAcquisitionError("retained game-feed evidence has the wrong provider")
    content_type = response.headers.get("content-type", response.capture.content_type)
    if "json" not in content_type.casefold():
        raise GameStateAcquisitionError("authoritative MLB game feed is not JSON content")
    try:
        raw_bytes = raw_store.read_verified(response.capture)
    except RawStoreError as exc:
        raise GameStateAcquisitionError(
            "authoritative MLB game-feed raw evidence failed checksum verification"
        ) from exc
    try:
        decoded = raw_bytes.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GameStateAcquisitionError(
            "authoritative MLB game feed is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise GameStateAcquisitionError(
            "authoritative MLB game-feed response root must be an object"
        )
    return MlbGameFeedEvidenceV1(
        request=request,
        response=response,
        payload=payload,
    )


def _canonical_status(
    status: Mapping[str, Any],
) -> tuple[DailySlateGameStatus, str, bool]:
    detailed = _optional_text(status.get("detailedState"))
    abstract = _optional_text(status.get("abstractGameState"))
    coded = _optional_text(status.get("codedGameState"))
    status_code = _optional_text(status.get("statusCode"))
    raw_status = detailed or abstract or coded or status_code
    if raw_status is None:
        raise GameStateNormalizationError("gameData.status contains no authoritative state")
    detail_key = (detailed or "").casefold()
    abstract_key = (abstract or "").casefold()
    if "postpon" in detail_key:
        return DailySlateGameStatus.POSTPONED, raw_status, False
    if "suspend" in detail_key:
        return DailySlateGameStatus.SUSPENDED, raw_status, False
    if "delay" in detail_key:
        return DailySlateGameStatus.DELAYED, raw_status, False
    if "cancel" in detail_key:
        return DailySlateGameStatus.CANCELLED, raw_status, False
    if "final" in detail_key or "game over" in detail_key or "completed early" in detail_key:
        return DailySlateGameStatus.FINAL, raw_status, False
    if "in progress" in detail_key or "replay review" in detail_key or "manager challenge" in detail_key:
        return DailySlateGameStatus.IN_PROGRESS, raw_status, False
    if "warmup" in detail_key or "pre-game" in detail_key or "pregame" in detail_key:
        return DailySlateGameStatus.PREGAME, raw_status, False
    if "scheduled" in detail_key or "preview" in detail_key:
        return DailySlateGameStatus.SCHEDULED, raw_status, False
    if abstract_key == "final":
        return DailySlateGameStatus.FINAL, raw_status, False
    if abstract_key == "live":
        return DailySlateGameStatus.IN_PROGRESS, raw_status, False
    if abstract_key == "preview":
        return DailySlateGameStatus.SCHEDULED, raw_status, False
    return DailySlateGameStatus.UNKNOWN, raw_status, True


def _source_position(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    position = _optional_mapping(record.get("position"), "boxscore player position")
    if position is None:
        position = _optional_mapping(
            record.get("primaryPosition"),
            "gameData player primaryPosition",
        )
    if position is None:
        return None, None
    return _optional_text(position.get("code")), _optional_text(position.get("name"))


def _source_status(record: Mapping[str, Any]) -> tuple[str | None, str | None]:
    status = _optional_mapping(record.get("status"), "player status")
    if status is None:
        return None, None
    return _optional_text(status.get("code")), _optional_text(status.get("description"))


def _player_from_source_id(
    source_player_id: object,
    *,
    box_players: Mapping[str, Any],
    game_players: Mapping[str, Any],
    player_identity_resolver: PlayerIdentityResolver | None,
) -> GameStatePlayerV1:
    player_id = _positive_decimal_id(source_player_id, "source player ID")
    key = f"ID{player_id}"
    box_record = _optional_mapping(box_players.get(key), f"boxscore players.{key}")
    game_record = _optional_mapping(game_players.get(key), f"gameData players.{key}")
    if box_record is None and game_record is None:
        raise GameStateNormalizationError(
            f"authoritative MLB player {player_id} has no player record"
        )

    full_name: str | None = None
    source_record: Mapping[str, Any]
    if box_record is not None:
        person = _optional_mapping(box_record.get("person"), f"boxscore players.{key}.person")
        if person is not None:
            person_id = _positive_decimal_id(person.get("id"), f"boxscore players.{key}.person.id")
            if person_id != player_id:
                raise GameStateNormalizationError("boxscore player ID disagrees with player bucket")
            full_name = _optional_text(person.get("fullName"))
        source_record = box_record
    else:
        assert game_record is not None
        source_record = game_record

    if game_record is not None:
        game_id = _positive_decimal_id(game_record.get("id"), f"gameData players.{key}.id")
        if game_id != player_id:
            raise GameStateNormalizationError("gameData player ID disagrees with player bucket")
        if full_name is None:
            full_name = _optional_text(game_record.get("fullName"))
    if full_name is None:
        raise GameStateNormalizationError(
            f"authoritative MLB player {player_id} has no fullName"
        )

    position_code, position_name = _source_position(source_record)
    if position_code is None and game_record is not None:
        position_code, position_name = _source_position(game_record)
    status_code, status_description = _source_status(source_record)
    if status_code is None and game_record is not None:
        status_code, status_description = _source_status(game_record)

    resolved = (
        None
        if player_identity_resolver is None
        else player_identity_resolver(player_id)
    )
    player_identity_id = None if resolved is None else resolved[0]
    canonical_player_id = None if resolved is None else resolved[1]
    return GameStatePlayerV1(
        source_player_id=player_id,
        full_name=full_name,
        player_identity_id=player_identity_id,
        canonical_player_id=canonical_player_id,
        source_position_code=position_code,
        source_position_name=position_name,
        source_status_code=status_code,
        source_status_description=status_description,
    )


def _bucket_ids(
    box_team: Mapping[str, Any],
    bucket_name: str,
) -> tuple[str, ...]:
    raw = box_team.get(bucket_name)
    if raw is None:
        return ()
    values = _list(raw, f"boxscore team {bucket_name}")
    return tuple(
        _positive_decimal_id(value, f"boxscore team {bucket_name} player ID")
        for value in values
    )


def _confirmed_starter_id(
    box_team: Mapping[str, Any] | None,
) -> str | None:
    if box_team is None:
        return None
    players = _optional_mapping(box_team.get("players"), "boxscore team players") or {}
    candidates: list[str] = []
    for pitcher_id in _bucket_ids(box_team, "pitchers"):
        record = _optional_mapping(players.get(f"ID{pitcher_id}"), "boxscore pitcher record")
        if record is None:
            continue
        stats = _optional_mapping(record.get("stats"), "boxscore pitcher stats")
        if stats is None:
            continue
        pitching = _optional_mapping(stats.get("pitching"), "boxscore pitcher pitching stats")
        if pitching is None:
            continue
        games_started = pitching.get("gamesStarted")
        if isinstance(games_started, bool):
            raise GameStateNormalizationError("pitching gamesStarted must not be boolean")
        if games_started == 1:
            candidates.append(pitcher_id)
        elif games_started not in {None, 0}:
            raise GameStateNormalizationError(
                "current-game pitching gamesStarted must be zero, one, or absent"
            )
    unique = tuple(dict.fromkeys(candidates))
    if len(unique) > 1:
        raise GameStateNormalizationError(
            "authoritative MLB boxscore identifies multiple game starters for one team"
        )
    return None if not unique else unique[0]


def _starter_state(
    *,
    side: str,
    team_id: str,
    source_game_id: str,
    probable_pitchers: Mapping[str, Any],
    box_team: Mapping[str, Any] | None,
    game_players: Mapping[str, Any],
    player_identity_resolver: PlayerIdentityResolver | None,
    warnings: list[GameStateWarningV1],
) -> StarterStateV1:
    box_players = (
        {}
        if box_team is None
        else (_optional_mapping(box_team.get("players"), "boxscore team players") or {})
    )
    confirmed_id = _confirmed_starter_id(box_team)
    probable_record = _optional_mapping(
        probable_pitchers.get(side),
        f"gameData.probablePitchers.{side}",
    )
    probable_id = (
        None
        if probable_record is None or probable_record.get("id") is None
        else _positive_decimal_id(
            probable_record.get("id"),
            f"gameData.probablePitchers.{side}.id",
        )
    )

    if confirmed_id is not None:
        player = _player_from_source_id(
            confirmed_id,
            box_players=box_players,
            game_players=game_players,
            player_identity_resolver=player_identity_resolver,
        )
        if probable_id is not None and probable_id != confirmed_id:
            warnings.append(
                GameStateWarningV1(
                    code=GameStateWarningCode.STARTER_PROBABLE_CHANGED,
                    message="authoritative confirmed starter differs from retained probable pitcher",
                    source_game_id=source_game_id,
                    team_id=team_id,
                )
            )
        certainty = StarterCertainty.CONFIRMED
        designation = "boxscore.stats.pitching.gamesStarted=1"
    elif probable_id is not None:
        player = _player_from_source_id(
            probable_id,
            box_players=box_players,
            game_players=game_players,
            player_identity_resolver=player_identity_resolver,
        )
        certainty = StarterCertainty.PROBABLE
        designation = "gameData.probablePitchers"
    else:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.STARTER_UNAVAILABLE,
                message="authoritative MLB game feed contains no usable starter evidence",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
        return StarterStateV1(certainty=StarterCertainty.UNAVAILABLE)

    if player_identity_resolver is not None and not player.resolved:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.UNRESOLVED_STARTER_IDENTITY,
                message="starter source ID lacks a verified canonical player mapping",
                source_game_id=source_game_id,
                team_id=team_id,
                unresolved_count=1,
            )
        )
    return StarterStateV1(
        certainty=certainty,
        player=player,
        source_designation=designation,
    )


def _lineup_state(
    *,
    box_team: Mapping[str, Any] | None,
    game_players: Mapping[str, Any],
    source_game_id: str,
    team_id: str,
    player_identity_resolver: PlayerIdentityResolver | None,
    warnings: list[GameStateWarningV1],
) -> LineupStateV1:
    if box_team is None:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.LINEUP_UNAVAILABLE,
                message="authoritative MLB boxscore team lineup is unavailable",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
        return LineupStateV1(availability=LineupAvailability.UNAVAILABLE)
    raw_order = box_team.get("battingOrder")
    if raw_order is None:
        order: list[Any] = []
    else:
        order = _list(raw_order, "boxscore team battingOrder")
    if not order:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.LINEUP_UNAVAILABLE,
                message="authoritative MLB game feed contains no posted batting order",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
        return LineupStateV1(availability=LineupAvailability.UNAVAILABLE)
    if len(order) > 9:
        raise GameStateNormalizationError(
            "authoritative MLB battingOrder contains more than nine current slots"
        )
    box_players = _optional_mapping(box_team.get("players"), "boxscore team players") or {}
    entries = tuple(
        LineupEntryV1(
            player=_player_from_source_id(
                player_id,
                box_players=box_players,
                game_players=game_players,
                player_identity_resolver=player_identity_resolver,
            ),
            batting_order_slot=index,
        )
        for index, player_id in enumerate(order, start=1)
    )
    availability = (
        LineupAvailability.POSTED
        if len(entries) == 9
        else LineupAvailability.PARTIAL
    )
    if availability is LineupAvailability.PARTIAL:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.LINEUP_PARTIAL,
                message="authoritative MLB batting order is present but incomplete",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
    if player_identity_resolver is not None:
        unresolved_count = sum(not entry.player.resolved for entry in entries)
        if unresolved_count:
            warnings.append(
                GameStateWarningV1(
                    code=GameStateWarningCode.UNRESOLVED_LINEUP_IDENTITIES,
                    message="one or more lineup source IDs lack verified canonical player mappings",
                    source_game_id=source_game_id,
                    team_id=team_id,
                    unresolved_count=unresolved_count,
                )
            )
    return LineupStateV1(availability=availability, entries=entries)


def _personnel_state(
    *,
    box_team: Mapping[str, Any] | None,
    game_players: Mapping[str, Any],
    source_game_id: str,
    team_id: str,
    player_identity_resolver: PlayerIdentityResolver | None,
    warnings: list[GameStateWarningV1],
) -> GamedayPersonnelV1:
    if box_team is None:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.PERSONNEL_UNAVAILABLE,
                message="authoritative MLB boxscore team personnel is unavailable",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
        return GamedayPersonnelV1(available=False)
    box_players = _optional_mapping(box_team.get("players"), "boxscore team players") or {}

    def players_for(bucket: str) -> tuple[GameStatePlayerV1, ...]:
        return tuple(
            _player_from_source_id(
                player_id,
                box_players=box_players,
                game_players=game_players,
                player_identity_resolver=player_identity_resolver,
            )
            for player_id in _bucket_ids(box_team, bucket)
        )

    batters = players_for("batters")
    pitchers = players_for("pitchers")
    bench = players_for("bench")
    bullpen = players_for("bullpen")
    if not any((batters, pitchers, bench, bullpen)):
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.PERSONNEL_UNAVAILABLE,
                message="authoritative MLB boxscore contains no gameday personnel buckets",
                source_game_id=source_game_id,
                team_id=team_id,
            )
        )
        return GamedayPersonnelV1(available=False)
    return GamedayPersonnelV1(
        available=True,
        batters=batters,
        pitchers=pitchers,
        bench=bench,
        bullpen=bullpen,
    )


def _source_team_id(
    game_data_teams: Mapping[str, Any],
    *,
    side: str,
    expected: str | None,
) -> str:
    team = _mapping(game_data_teams.get(side), f"gameData.teams.{side}")
    source_id = _positive_decimal_id(team.get("id"), f"gameData.teams.{side}.id")
    if expected is None:
        raise GameStateNormalizationError(
            f"upstream DailySlate lacks authoritative {side} source team ID"
        )
    if source_id != expected:
        raise GameStateNormalizationError(
            f"gameData {side} team ID disagrees with upstream DailySlate"
        )
    return source_id


def _box_team(
    boxscore_teams: Mapping[str, Any],
    *,
    side: str,
    expected_source_team_id: str,
) -> Mapping[str, Any] | None:
    raw = boxscore_teams.get(side)
    if raw is None:
        return None
    team = _mapping(raw, f"liveData.boxscore.teams.{side}")
    team_identity = _optional_mapping(team.get("team"), f"boxscore.teams.{side}.team")
    if team_identity is not None and team_identity.get("id") is not None:
        observed_id = _positive_decimal_id(
            team_identity.get("id"),
            f"boxscore.teams.{side}.team.id",
        )
        if observed_id != expected_source_team_id:
            raise GameStateNormalizationError(
                f"boxscore {side} team ID disagrees with upstream DailySlate"
            )
    return team


def normalize_mlb_game_feed(
    payload: Mapping[str, Any],
    *,
    slate_game: DailySlateGameV1,
    observed_at: datetime,
    upstream_raw_checksum: str,
    player_identity_resolver: PlayerIdentityResolver | None = None,
) -> tuple[GameStateGameV1, tuple[GameStateWarningV1, ...]]:
    source_game_id = _positive_decimal_id(payload.get("gamePk"), "gamePk")
    if source_game_id != slate_game.source_game_id:
        raise GameStateNormalizationError(
            "authoritative MLB game feed gamePk disagrees with upstream DailySlate"
        )
    canonical_observed = _aware_utc(observed_at, "observed_at")
    game_data = _mapping(payload.get("gameData"), "gameData")
    status_record = _mapping(game_data.get("status"), "gameData.status")
    game_status, raw_status, unknown_status = _canonical_status(status_record)
    warnings: list[GameStateWarningV1] = []
    if unknown_status:
        warnings.append(
            GameStateWarningV1(
                code=GameStateWarningCode.UNKNOWN_GAME_STATUS,
                message="MLB game status was not recognized and was retained as UNKNOWN",
                source_game_id=source_game_id,
            )
        )

    game_data_teams = _mapping(game_data.get("teams"), "gameData.teams")
    source_away_team_id = _source_team_id(
        game_data_teams,
        side="away",
        expected=slate_game.source_away_team_id,
    )
    source_home_team_id = _source_team_id(
        game_data_teams,
        side="home",
        expected=slate_game.source_home_team_id,
    )
    game_players = _mapping(game_data.get("players"), "gameData.players")
    probable_pitchers = _optional_mapping(
        game_data.get("probablePitchers"),
        "gameData.probablePitchers",
    ) or {}
    live_data = _optional_mapping(payload.get("liveData"), "liveData") or {}
    boxscore = _optional_mapping(live_data.get("boxscore"), "liveData.boxscore") or {}
    boxscore_teams = _optional_mapping(boxscore.get("teams"), "liveData.boxscore.teams") or {}
    away_box = _box_team(
        boxscore_teams,
        side="away",
        expected_source_team_id=source_away_team_id,
    )
    home_box = _box_team(
        boxscore_teams,
        side="home",
        expected_source_team_id=source_home_team_id,
    )

    away = TeamGameStateV1(
        team_id=slate_game.away_team_id,
        source_team_id=source_away_team_id,
        starter=_starter_state(
            side="away",
            team_id=slate_game.away_team_id,
            source_game_id=source_game_id,
            probable_pitchers=probable_pitchers,
            box_team=away_box,
            game_players=game_players,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
        lineup=_lineup_state(
            box_team=away_box,
            game_players=game_players,
            source_game_id=source_game_id,
            team_id=slate_game.away_team_id,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
        personnel=_personnel_state(
            box_team=away_box,
            game_players=game_players,
            source_game_id=source_game_id,
            team_id=slate_game.away_team_id,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
    )
    home = TeamGameStateV1(
        team_id=slate_game.home_team_id,
        source_team_id=source_home_team_id,
        starter=_starter_state(
            side="home",
            team_id=slate_game.home_team_id,
            source_game_id=source_game_id,
            probable_pitchers=probable_pitchers,
            box_team=home_box,
            game_players=game_players,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
        lineup=_lineup_state(
            box_team=home_box,
            game_players=game_players,
            source_game_id=source_game_id,
            team_id=slate_game.home_team_id,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
        personnel=_personnel_state(
            box_team=home_box,
            game_players=game_players,
            source_game_id=source_game_id,
            team_id=slate_game.home_team_id,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        ),
    )
    provenance = GameStateProvenanceV1(
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        source_record_id=source_game_id,
        observed_at=canonical_observed,
        source_version=MLB_GAME_FEED_SOURCE_VERSION,
        raw_status=raw_status,
        upstream_checksum=upstream_raw_checksum,
    )
    try:
        game = GameStateGameV1(
            edge_event_id=slate_game.edge_event_id,
            daily_mlb_game_id=slate_game.daily_mlb_game_id,
            source_game_id=source_game_id,
            away_team_id=slate_game.away_team_id,
            home_team_id=slate_game.home_team_id,
            game_status=game_status,
            away=away,
            home=home,
            observed_at=canonical_observed,
            provenance=provenance,
        )
    except GameStateContractError as exc:
        raise GameStateNormalizationError(
            f"authoritative MLB game {source_game_id} violates GameStateV1"
        ) from exc
    return game, tuple(warnings)


def normalize_mlb_game_state(
    *,
    slate: DailySlateV1,
    evidence_by_game: Mapping[str, MlbGameFeedEvidenceV1],
    player_identity_resolver: PlayerIdentityResolver | None = None,
) -> GameStateNormalizationResultV1:
    expected_ids = tuple(game.source_game_id for game in slate.games)
    expected_set = set(expected_ids)
    observed_set = set(evidence_by_game)
    if expected_set != observed_set:
        missing = sorted(expected_set - observed_set)
        unexpected = sorted(observed_set - expected_set)
        raise GameStateNormalizationError(
            "game-feed evidence must exactly match DailySlate games; "
            f"missing={missing}, unexpected={unexpected}"
        )
    normalized_games: list[GameStateGameV1] = []
    warnings: list[GameStateWarningV1] = []
    for slate_game in slate.games:
        evidence = evidence_by_game[slate_game.source_game_id]
        if evidence.source_game_id != slate_game.source_game_id:
            raise GameStateNormalizationError(
                "game-feed evidence request identity disagrees with DailySlate"
            )
        game, game_warnings = normalize_mlb_game_feed(
            evidence.payload,
            slate_game=slate_game,
            observed_at=evidence.observed_at,
            upstream_raw_checksum=evidence.raw_checksum,
            player_identity_resolver=player_identity_resolver,
        )
        normalized_games.append(game)
        warnings.extend(game_warnings)

    snapshot_observed_at = (
        max(game.observed_at for game in normalized_games)
        if normalized_games
        else slate.observed_at
    )
    provenance = GameStateProvenanceV1(
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        source_record_id=None,
        observed_at=snapshot_observed_at,
        source_version=MLB_GAME_FEED_SOURCE_VERSION,
        raw_status="game_state_snapshot",
        upstream_checksum=slate.checksum,
    )
    try:
        state = GameStateV1(
            requested_date=slate.requested_date,
            as_of_time=slate.as_of_time,
            observed_at=snapshot_observed_at,
            source_authority=AUTHORITATIVE_MLB_PROVIDER,
            source_version=MLB_GAME_FEED_SOURCE_VERSION,
            upstream_daily_slate_checksum=slate.checksum,
            games=tuple(normalized_games),
            provenance=provenance,
        )
    except GameStateContractError as exc:
        raise GameStateNormalizationError(
            "authoritative MLB game-state snapshot violates GameStateV1"
        ) from exc
    return GameStateNormalizationResultV1(
        state=state,
        warnings=tuple(warnings),
    )
