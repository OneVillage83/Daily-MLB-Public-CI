from __future__ import annotations

from datetime import datetime, timezone

from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
)
from app.odds_weather.acquisition import (
    WeatherAcquisitionDisposition,
    plan_odds_weather_acquisition,
)

OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
TEAM_SOURCE_IDS = {
    "LAD": "119",
    "SF": "137",
    "TB": "139",
    "NYY": "147",
    "HOU": "117",
    "TEX": "140",
}
VENUE_NAMES = {
    "LAD": "Dodger Stadium",
    "TB": "Tropicana Field",
    "HOU": "Daikin Park",
}


def _game(
    game_pk: int,
    *,
    away: str = "SF",
    home: str = "LAD",
    start: datetime | None = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc),
    venue_name: str | None = None,
) -> DailySlateGameV1:
    source_id = str(game_pk)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date="2026-07-27",
        scheduled_start_time=start,
        away_team_id=away,
        home_team_id=home,
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id=source_id,
        source_provider="mlb",
        observed_at=OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=source_id,
            observed_at=OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum="a" * 64,
        ),
        source_home_team_id=TEAM_SOURCE_IDS[home],
        source_away_team_id=TEAM_SOURCE_IDS[away],
        source_venue_id="22",
        source_venue_name=venue_name or VENUE_NAMES.get(home),
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    return DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        observed_at=OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=games,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum="a" * 64,
        ),
    )


def test_zero_game_slate_plans_zero_provider_requests() -> None:
    plan = plan_odds_weather_acquisition(_slate())

    assert plan.odds_request_required is False
    assert plan.planned_odds_requests == 0
    assert plan.weather_games == ()
    assert plan.planned_nws_game_requests == 0
    assert plan.planned_openweather_fallback_game_requests == 0


def test_outdoor_game_plans_odds_nws_and_openweather_fallback() -> None:
    plan = plan_odds_weather_acquisition(_slate(_game(900001)))

    assert plan.planned_odds_requests == 1
    assert len(plan.weather_games) == 1
    weather = plan.weather_games[0]
    assert weather.disposition is WeatherAcquisitionDisposition.COLLECT
    assert weather.collect_nws is True
    assert weather.collect_openweather_fallback is True
    assert weather.reason_codes == ()


def test_verified_fixed_roof_game_plans_zero_weather_provider_calls() -> None:
    game = _game(
        900010,
        away="NYY",
        home="TB",
        venue_name="Tropicana Field",
    )
    plan = plan_odds_weather_acquisition(_slate(game))

    assert plan.planned_odds_requests == 1
    assert plan.planned_nws_game_requests == 0
    assert plan.planned_openweather_fallback_game_requests == 0
    weather = plan.weather_games[0]
    assert weather.disposition is WeatherAcquisitionDisposition.SKIP_FIXED_ROOF
    assert weather.reason_codes == ("verified_fixed_closed_roof",)


def test_missing_start_time_plans_no_weather_call_but_keeps_slate_odds_request() -> None:
    game = _game(900001, start=None)
    plan = plan_odds_weather_acquisition(_slate(game))

    assert plan.planned_odds_requests == 1
    weather = plan.weather_games[0]
    assert weather.disposition is WeatherAcquisitionDisposition.SKIP_MISSING_START_TIME
    assert weather.collect_nws is False
    assert weather.collect_openweather_fallback is False


def test_venue_mismatch_plans_no_weather_call_instead_of_guessing() -> None:
    game = _game(900001, venue_name="Wrigley Field")
    plan = plan_odds_weather_acquisition(_slate(game))

    weather = plan.weather_games[0]
    assert weather.disposition is WeatherAcquisitionDisposition.SKIP_INVALID_STADIUM_METADATA
    assert weather.collect_nws is False
    assert weather.collect_openweather_fallback is False
    assert "venue_metadata_invalid:daily_slate_venue_alias" in weather.reason_codes


def test_retractable_roof_game_still_plans_weather_collection() -> None:
    game = _game(
        900020,
        away="TEX",
        home="HOU",
        venue_name="Daikin Park",
    )
    plan = plan_odds_weather_acquisition(_slate(game))

    weather = plan.weather_games[0]
    assert weather.disposition is WeatherAcquisitionDisposition.COLLECT
    assert weather.collect_nws is True
    assert weather.collect_openweather_fallback is True
