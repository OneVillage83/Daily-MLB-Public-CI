from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.daily_slate.contracts import DailySlateGameV1, DailySlateV1
from app.stadiums import (
    VerificationState,
    field_verification,
    material_weather_metadata_errors,
    resolve_venue_alias,
    roof_operational_status,
    stadium_for_team,
)

_ASSOCIATION_FIELDS = frozenset(
    {"physical_venue_key", "active_club_association", "timezone"}
)
_COORDINATE_FIELDS = frozenset({"latitude", "longitude"})


class WeatherAcquisitionDisposition(StrEnum):
    COLLECT = "collect"
    SKIP_FIXED_ROOF = "skip_fixed_roof"
    SKIP_MISSING_START_TIME = "skip_missing_start_time"
    SKIP_INVALID_STADIUM_METADATA = "skip_invalid_stadium_metadata"


@dataclass(frozen=True, slots=True)
class WeatherAcquisitionPlanV1:
    source_game_id: str
    disposition: WeatherAcquisitionDisposition
    collect_nws: bool
    collect_openweather_fallback: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OddsWeatherAcquisitionPlanV1:
    odds_request_required: bool
    weather_games: tuple[WeatherAcquisitionPlanV1, ...]

    @property
    def planned_odds_requests(self) -> int:
        return 1 if self.odds_request_required else 0

    @property
    def planned_nws_game_requests(self) -> int:
        return sum(item.collect_nws for item in self.weather_games)

    @property
    def planned_openweather_fallback_game_requests(self) -> int:
        return sum(item.collect_openweather_fallback for item in self.weather_games)


def _errors_for_fields(
    stadium: dict[str, object],
    fields: frozenset[str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            error
            for error in material_weather_metadata_errors(stadium)
            if error.rsplit(":", 1)[-1] in fields
        )
    )


def _weather_plan_for_game(game: DailySlateGameV1) -> WeatherAcquisitionPlanV1:
    if game.scheduled_start_time is None:
        return WeatherAcquisitionPlanV1(
            source_game_id=game.source_game_id,
            disposition=WeatherAcquisitionDisposition.SKIP_MISSING_START_TIME,
            collect_nws=False,
            collect_openweather_fallback=False,
            reason_codes=("missing_scheduled_start_time",),
        )

    stadium = stadium_for_team(game.home_team_id)
    if stadium is None:
        return WeatherAcquisitionPlanV1(
            source_game_id=game.source_game_id,
            disposition=WeatherAcquisitionDisposition.SKIP_INVALID_STADIUM_METADATA,
            collect_nws=False,
            collect_openweather_fallback=False,
            reason_codes=("stadium_metadata_missing",),
        )

    reasons = list(_errors_for_fields(stadium, _ASSOCIATION_FIELDS))
    reasons.extend(_errors_for_fields(stadium, _COORDINATE_FIELDS))
    physical_key = str(stadium.get("physical_venue_key") or "")
    if str(stadium.get("team_key") or "") != game.home_team_id:
        reasons.append("venue_metadata_invalid:active_club_team_key")
    if game.venue_id is not None and game.venue_id != physical_key:
        reasons.append("venue_metadata_invalid:physical_venue_identity")
    if game.source_venue_name:
        resolution = resolve_venue_alias(
            game.source_venue_name,
            team_key=game.home_team_id,
        )
        if resolution.get("status") not in {"current", "historical_same_venue"}:
            reasons.append("venue_metadata_invalid:daily_slate_venue_alias")
        elif resolution.get("physical_venue_key") != stadium.get(
            "physical_venue_key"
        ):
            reasons.append("venue_metadata_invalid:physical_venue_identity")
    if reasons:
        return WeatherAcquisitionPlanV1(
            source_game_id=game.source_game_id,
            disposition=WeatherAcquisitionDisposition.SKIP_INVALID_STADIUM_METADATA,
            collect_nws=False,
            collect_openweather_fallback=False,
            reason_codes=tuple(sorted(set(reasons))),
        )

    roof_verification = str(
        field_verification(stadium, "roof_type").get(
            "status", VerificationState.UNKNOWN.value
        )
    )
    roof_status_value = roof_operational_status(stadium)
    roof_status = str(getattr(roof_status_value, "value", roof_status_value))
    if (
        stadium.get("roof_type") == "fixed"
        and roof_verification == VerificationState.VERIFIED.value
        and roof_status == "closed"
    ):
        return WeatherAcquisitionPlanV1(
            source_game_id=game.source_game_id,
            disposition=WeatherAcquisitionDisposition.SKIP_FIXED_ROOF,
            collect_nws=False,
            collect_openweather_fallback=False,
            reason_codes=("verified_fixed_closed_roof",),
        )

    return WeatherAcquisitionPlanV1(
        source_game_id=game.source_game_id,
        disposition=WeatherAcquisitionDisposition.COLLECT,
        collect_nws=True,
        collect_openweather_fallback=True,
        reason_codes=(),
    )


def plan_odds_weather_acquisition(slate: DailySlateV1) -> OddsWeatherAcquisitionPlanV1:
    """Plan provider calls without making any network request.

    The Odds API is slate-scoped, so a non-empty slate requires one planned odds request.
    Weather is game-scoped. Fixed-roof, missing-start, and invalid-stadium games are
    deliberately skipped before a provider client can consume quota.
    """

    return OddsWeatherAcquisitionPlanV1(
        odds_request_required=bool(slate.games),
        weather_games=tuple(_weather_plan_for_game(game) for game in slate.games),
    )
