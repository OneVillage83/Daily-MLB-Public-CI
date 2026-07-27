from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, TypeAlias

from app.daily_slate.contracts import (
    AUTHORITATIVE_MLB_PROVIDER,
    DailySlateContractError,
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    ProbableStarterV1,
    VenueMappingStatus,
    canonical_authoritative_game_id,
    daily_mlb_game_id,
    edge_event_id,
)
from app.daily_slate.identity import (
    resolve_canonical_team_id,
    resolve_canonical_venue_id,
)
from app.database import Database
from app.identifiers import parse_requested_date
from app.stats.contracts import StatsProvider, StatsRequest, StatsResponse, StatsTransport
from app.stats.raw_store import RawArtifactStore, RawStoreError

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_SCHEDULE_ENDPOINT_CATEGORY = "daily_slate_schedule"
MLB_SCHEDULE_SOURCE_VERSION = "statsapi-v1"
MLB_SCHEDULE_FIXTURE_KEY = "mlb_daily_slate_schedule"

PlayerIdentityResolution: TypeAlias = tuple[str, str]
PlayerIdentityResolver: TypeAlias = Callable[[str], PlayerIdentityResolution | None]


class DailySlateAcquisitionError(RuntimeError):
    """Raised when authoritative MLB schedule evidence cannot be acquired safely."""


class DailySlateNormalizationError(DailySlateAcquisitionError):
    """Raised when retained MLB schedule evidence cannot normalize losslessly."""


class DailySlateWarningCode(StrEnum):
    UNRESOLVED_VENUE = "unresolved_venue"
    UNRESOLVED_PROBABLE_STARTER = "unresolved_probable_starter"
    MALFORMED_PROBABLE_STARTER = "malformed_probable_starter"
    UNKNOWN_GAME_STATUS = "unknown_game_status"
    UNKNOWN_DOUBLEHEADER_STATUS = "unknown_doubleheader_status"
    START_TIME_TBD = "start_time_tbd"


@dataclass(frozen=True, slots=True)
class DailySlateWarningV1:
    code: DailySlateWarningCode
    message: str
    source_game_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "code": self.code.value,
            "message": self.message,
            "source_game_id": self.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class MlbScheduleEvidenceV1:
    request: StatsRequest
    response: StatsResponse
    payload: Mapping[str, Any]

    @property
    def observed_at(self) -> datetime:
        return self.response.capture.retrieved_at

    @property
    def raw_checksum(self) -> str:
        return self.response.capture.checksum_sha256


@dataclass(frozen=True, slots=True)
class DailySlateNormalizationResultV1:
    slate: DailySlateV1
    warnings: tuple[DailySlateWarningV1, ...]

    def warning_payload(self) -> list[dict[str, str | None]]:
        return [warning.as_dict() for warning in self.warnings]


def _aware_utc(value: datetime | str, field_name: str) -> datetime:
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise DailySlateNormalizationError(f"{field_name} must not be blank")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise DailySlateNormalizationError(
                f"{field_name} is not a valid ISO datetime"
            ) from exc
    else:
        raise DailySlateNormalizationError(f"{field_name} must be an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailySlateNormalizationError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DailySlateNormalizationError(f"{field_name} must be an object")
    return value


def _list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DailySlateNormalizationError(f"{field_name} must be a list")
    return value


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DailySlateNormalizationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DailySlateNormalizationError(
            f"{field_name} must be a non-negative integer"
        )
    return value


def _positive_decimal(value: object, field_name: str) -> str:
    if isinstance(value, bool):
        raise DailySlateNormalizationError(f"{field_name} must be a positive decimal ID")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        number = int(value, 10)
    else:
        raise DailySlateNormalizationError(f"{field_name} must be a positive decimal ID")
    if number <= 0:
        raise DailySlateNormalizationError(f"{field_name} must be a positive decimal ID")
    return str(number)


def build_mlb_schedule_request(
    requested_date: str,
    *,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> StatsRequest:
    parsed_date = parse_requested_date(requested_date)
    return StatsRequest(
        provider=StatsProvider.MLB,
        endpoint_category=MLB_SCHEDULE_ENDPOINT_CATEGORY,
        url=MLB_SCHEDULE_URL,
        fixture_key=MLB_SCHEDULE_FIXTURE_KEY,
        params={
            "sportId": 1,
            "date": parsed_date.isoformat(),
            "hydrate": "probablePitcher(note)",
        },
        headers={"Accept": "application/json"},
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        minimum_interval_seconds=0.0,
        persistent_cache=False,
    )


def acquire_mlb_schedule(
    *,
    transport: StatsTransport,
    raw_store: RawArtifactStore,
    requested_date: str,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> MlbScheduleEvidenceV1:
    request = build_mlb_schedule_request(
        requested_date,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
    )
    response = transport.fetch(request)
    if response.status_code != 200:
        raise DailySlateAcquisitionError(
            f"authoritative MLB schedule returned unexpected HTTP {response.status_code}"
        )
    if response.capture.provider is not StatsProvider.MLB:
        raise DailySlateAcquisitionError("retained schedule evidence has the wrong provider")
    content_type = response.headers.get("content-type", response.capture.content_type)
    if "json" not in content_type.casefold():
        raise DailySlateAcquisitionError(
            "authoritative MLB schedule response is not JSON content"
        )
    try:
        raw_bytes = raw_store.read_verified(response.capture)
    except RawStoreError as exc:
        raise DailySlateAcquisitionError(
            "authoritative MLB schedule raw evidence failed checksum verification"
        ) from exc
    try:
        decoded = raw_bytes.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DailySlateAcquisitionError(
            "authoritative MLB schedule response is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise DailySlateAcquisitionError(
            "authoritative MLB schedule response root must be an object"
        )
    return MlbScheduleEvidenceV1(
        request=request,
        response=response,
        payload=payload,
    )


def verified_mlb_player_identity_resolver(database: Database) -> PlayerIdentityResolver:
    def resolve(source_player_id: str) -> PlayerIdentityResolution | None:
        canonical_source_id = _positive_decimal(
            source_player_id, "probable starter source player ID"
        )
        with database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT
                    identity.player_identity_id,
                    mapping.canonical_player_id
                FROM stats_player_identities AS identity
                JOIN stats_player_identifier_mappings AS mapping
                  ON mapping.player_identity_id=identity.player_identity_id
                WHERE identity.provider=?
                  AND identity.provider_player_id=?
                  AND mapping.verification_status='verified'
                ORDER BY mapping.canonical_player_id, identity.player_identity_id
                """,
                (AUTHORITATIVE_MLB_PROVIDER, canonical_source_id),
            ).fetchall()
        candidates = {
            (str(row["player_identity_id"]), str(row["canonical_player_id"]))
            for row in rows
        }
        if not candidates:
            return None
        if len(candidates) != 1:
            raise DailySlateNormalizationError(
                "probable starter source identity has conflicting verified canonical mappings"
            )
        return next(iter(candidates))

    return resolve


def _schedule_games(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    total_games = _nonnegative_int(payload.get("totalGames"), "totalGames")
    dates = _list(payload.get("dates"), "dates")
    flattened: list[Mapping[str, Any]] = []
    date_total = 0
    for index, raw_date in enumerate(dates):
        date_record = _mapping(raw_date, f"dates[{index}]")
        if date_record.get("date") is not None:
            parse_requested_date(_required_text(date_record.get("date"), f"dates[{index}].date"))
        games = _list(date_record.get("games"), f"dates[{index}].games")
        expected_for_date = _nonnegative_int(
            date_record.get("totalGames"), f"dates[{index}].totalGames"
        )
        if expected_for_date != len(games):
            raise DailySlateNormalizationError(
                "authoritative MLB schedule date count does not match its games"
            )
        date_total += expected_for_date
        for game_index, raw_game in enumerate(games):
            flattened.append(
                _mapping(raw_game, f"dates[{index}].games[{game_index}]")
            )
    if total_games != date_total or total_games != len(flattened):
        raise DailySlateNormalizationError(
            "authoritative MLB schedule totalGames is internally inconsistent"
        )
    source_ids = [
        canonical_authoritative_game_id(game.get("gamePk")) for game in flattened
    ]
    if len(source_ids) != len(set(source_ids)):
        raise DailySlateNormalizationError(
            "duplicate authoritative MLB game identity in schedule response"
        )
    return tuple(flattened)


def _game_status(
    status: Mapping[str, Any],
) -> tuple[DailySlateGameStatus, str, bool]:
    detailed = _optional_text(status.get("detailedState"))
    abstract = _optional_text(status.get("abstractGameState"))
    coded = _optional_text(status.get("codedGameState"))
    status_code = _optional_text(status.get("statusCode"))
    raw_status = detailed or abstract or coded or status_code
    if raw_status is None:
        raise DailySlateNormalizationError("game status contains no authoritative state")

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
    if (
        "final" in detail_key
        or "game over" in detail_key
        or "completed early" in detail_key
    ):
        return DailySlateGameStatus.FINAL, raw_status, False
    if (
        "in progress" in detail_key
        or "replay review" in detail_key
        or "manager challenge" in detail_key
    ):
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


def _doubleheader_status(
    value: object,
) -> tuple[DailySlateDoubleheaderStatus, bool]:
    if isinstance(value, str):
        code = value.strip().upper()
        if code == "N":
            return DailySlateDoubleheaderStatus.SINGLE, False
        if code in {"Y", "S"}:
            return DailySlateDoubleheaderStatus.DOUBLEHEADER, False
    return DailySlateDoubleheaderStatus.UNKNOWN, True


def _probable_starter(
    value: object,
    *,
    source_game_id: str,
    observed_at: datetime,
    upstream_checksum: str,
    player_identity_resolver: PlayerIdentityResolver | None,
    warnings: list[DailySlateWarningV1],
) -> ProbableStarterV1 | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.MALFORMED_PROBABLE_STARTER,
                message="MLB probable starter evidence was malformed and was omitted",
                source_game_id=source_game_id,
            )
        )
        return None
    try:
        source_player_id = _positive_decimal(
            value.get("id"), "probable starter source player ID"
        )
        full_name = _required_text(value.get("fullName"), "probable starter fullName")
    except DailySlateNormalizationError:
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.MALFORMED_PROBABLE_STARTER,
                message="MLB probable starter evidence was incomplete and was omitted",
                source_game_id=source_game_id,
            )
        )
        return None

    resolved = (
        None
        if player_identity_resolver is None
        else player_identity_resolver(source_player_id)
    )
    if resolved is None:
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.UNRESOLVED_PROBABLE_STARTER,
                message="MLB probable starter lacks a verified canonical player mapping",
                source_game_id=source_game_id,
            )
        )
        player_identity_id = None
        canonical_player_id = None
    else:
        player_identity_id, canonical_player_id = resolved

    provenance = DailySlateProvenanceV1(
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        source_record_id=source_player_id,
        observed_at=observed_at,
        source_version=MLB_SCHEDULE_SOURCE_VERSION,
        raw_status="authoritative_probable",
        upstream_checksum=upstream_checksum,
    )
    return ProbableStarterV1(
        source_player_id=source_player_id,
        full_name=full_name,
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        observed_at=observed_at,
        player_identity_id=player_identity_id,
        canonical_player_id=canonical_player_id,
        provenance=provenance,
    )


def _normalize_game(
    raw_game: Mapping[str, Any],
    *,
    observed_at: datetime,
    upstream_checksum: str,
    player_identity_resolver: PlayerIdentityResolver | None,
    warnings: list[DailySlateWarningV1],
) -> DailySlateGameV1:
    source_game_id = canonical_authoritative_game_id(raw_game.get("gamePk"))
    official_date = _required_text(raw_game.get("officialDate"), "officialDate")
    parse_requested_date(official_date)

    status_record = _mapping(raw_game.get("status"), "status")
    game_status, raw_status, unknown_status = _game_status(status_record)
    if unknown_status:
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.UNKNOWN_GAME_STATUS,
                message="MLB game status was not recognized and was retained as UNKNOWN",
                source_game_id=source_game_id,
            )
        )

    start_time_tbd = status_record.get("startTimeTBD") is True
    if start_time_tbd:
        scheduled_start_time = None
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.START_TIME_TBD,
                message="MLB explicitly marks the scheduled start time as TBD",
                source_game_id=source_game_id,
            )
        )
    else:
        scheduled_start_time = _aware_utc(raw_game.get("gameDate"), "gameDate")

    teams = _mapping(raw_game.get("teams"), "teams")
    away_record = _mapping(teams.get("away"), "teams.away")
    home_record = _mapping(teams.get("home"), "teams.home")
    away_team = _mapping(away_record.get("team"), "teams.away.team")
    home_team = _mapping(home_record.get("team"), "teams.home.team")
    source_away_team_id = _positive_decimal(
        away_team.get("id"), "teams.away.team.id"
    )
    source_home_team_id = _positive_decimal(
        home_team.get("id"), "teams.home.team.id"
    )
    away_team_id = resolve_canonical_team_id(
        _required_text(away_team.get("name"), "teams.away.team.name")
    )
    home_team_id = resolve_canonical_team_id(
        _required_text(home_team.get("name"), "teams.home.team.name")
    )

    venue_record = _mapping(raw_game.get("venue"), "venue")
    source_venue_id = (
        None
        if venue_record.get("id") is None
        else _positive_decimal(venue_record.get("id"), "venue.id")
    )
    source_venue_name = _optional_text(venue_record.get("name"))
    if source_venue_id is None and source_venue_name is None:
        raise DailySlateNormalizationError(
            "authoritative MLB game contains no venue source evidence"
        )
    if source_venue_name is None:
        venue_id = None
        venue_mapping_status = VenueMappingStatus.UNRESOLVED
    else:
        venue_resolution = resolve_canonical_venue_id(
            source_venue_name,
            home_team_id=home_team_id,
        )
        venue_id = venue_resolution.venue_id
        venue_mapping_status = venue_resolution.status
    if venue_mapping_status is VenueMappingStatus.UNRESOLVED:
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.UNRESOLVED_VENUE,
                message="MLB venue evidence could not be resolved to a canonical physical venue",
                source_game_id=source_game_id,
            )
        )

    raw_game_number = raw_game.get("gameNumber")
    if raw_game_number is None:
        game_number = None
    elif isinstance(raw_game_number, bool) or not isinstance(raw_game_number, int) or raw_game_number < 1:
        raise DailySlateNormalizationError("gameNumber must be a positive integer")
    else:
        game_number = raw_game_number

    doubleheader_status, unknown_doubleheader = _doubleheader_status(
        raw_game.get("doubleHeader")
    )
    if unknown_doubleheader:
        warnings.append(
            DailySlateWarningV1(
                code=DailySlateWarningCode.UNKNOWN_DOUBLEHEADER_STATUS,
                message="MLB doubleheader code was not recognized and was retained as UNKNOWN",
                source_game_id=source_game_id,
            )
        )

    away_probable = _probable_starter(
        away_record.get("probablePitcher"),
        source_game_id=source_game_id,
        observed_at=observed_at,
        upstream_checksum=upstream_checksum,
        player_identity_resolver=player_identity_resolver,
        warnings=warnings,
    )
    home_probable = _probable_starter(
        home_record.get("probablePitcher"),
        source_game_id=source_game_id,
        observed_at=observed_at,
        upstream_checksum=upstream_checksum,
        player_identity_resolver=player_identity_resolver,
        warnings=warnings,
    )

    provenance = DailySlateProvenanceV1(
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        source_record_id=source_game_id,
        observed_at=observed_at,
        source_version=MLB_SCHEDULE_SOURCE_VERSION,
        raw_status=raw_status,
        upstream_checksum=upstream_checksum,
    )
    try:
        return DailySlateGameV1(
            edge_event_id=edge_event_id(source_game_id),
            daily_mlb_game_id=daily_mlb_game_id(source_game_id),
            official_date=official_date,
            scheduled_start_time=scheduled_start_time,
            away_team_id=away_team_id,
            home_team_id=home_team_id,
            venue_id=venue_id,
            venue_mapping_status=venue_mapping_status,
            game_number=game_number,
            doubleheader_status=doubleheader_status,
            game_status=game_status,
            source_game_id=source_game_id,
            source_provider=AUTHORITATIVE_MLB_PROVIDER,
            observed_at=observed_at,
            provenance=provenance,
            source_home_team_id=source_home_team_id,
            source_away_team_id=source_away_team_id,
            source_venue_id=source_venue_id,
            source_venue_name=source_venue_name,
            source_updated_at=None,
            away_probable_starter=away_probable,
            home_probable_starter=home_probable,
        )
    except DailySlateContractError as exc:
        raise DailySlateNormalizationError(
            f"authoritative MLB game {source_game_id} violates DailySlateV1"
        ) from exc


def normalize_mlb_schedule(
    payload: Mapping[str, Any],
    *,
    requested_date: str,
    as_of_time: datetime | str,
    observed_at: datetime | str,
    upstream_checksum: str,
    player_identity_resolver: PlayerIdentityResolver | None = None,
) -> DailySlateNormalizationResultV1:
    parsed_requested_date = parse_requested_date(requested_date).isoformat()
    canonical_as_of = _aware_utc(as_of_time, "as_of_time")
    canonical_observed = _aware_utc(observed_at, "observed_at")
    raw_games = _schedule_games(payload)
    warnings: list[DailySlateWarningV1] = []
    games = tuple(
        _normalize_game(
            raw_game,
            observed_at=canonical_observed,
            upstream_checksum=upstream_checksum,
            player_identity_resolver=player_identity_resolver,
            warnings=warnings,
        )
        for raw_game in raw_games
    )
    if len(games) != len(raw_games):
        raise DailySlateNormalizationError(
            "authoritative MLB schedule normalization dropped one or more games"
        )
    provenance = DailySlateProvenanceV1(
        source_provider=AUTHORITATIVE_MLB_PROVIDER,
        source_record_id=None,
        observed_at=canonical_observed,
        source_version=MLB_SCHEDULE_SOURCE_VERSION,
        raw_status="schedule",
        upstream_checksum=upstream_checksum,
    )
    try:
        slate = DailySlateV1(
            requested_date=parsed_requested_date,
            as_of_time=canonical_as_of,
            observed_at=canonical_observed,
            source_authority=AUTHORITATIVE_MLB_PROVIDER,
            source_version=MLB_SCHEDULE_SOURCE_VERSION,
            games=games,
            provenance=provenance,
        )
    except DailySlateContractError as exc:
        raise DailySlateNormalizationError(
            "authoritative MLB schedule violates DailySlateV1"
        ) from exc
    if len(slate.games) != len(raw_games):
        raise DailySlateNormalizationError(
            "authoritative MLB schedule canonical game count changed during normalization"
        )
    return DailySlateNormalizationResultV1(
        slate=slate,
        warnings=tuple(warnings),
    )
