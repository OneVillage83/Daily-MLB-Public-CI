from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.baseball_intelligence.contracts import (
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceGameV1,
    TeamBaseballIntelligenceV1,
    TeamIntelligenceCoverageV1,
)
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
)
from app.odds_weather.assembly import OddsWeatherAssemblyError, assemble_odds_weather
from app.odds_weather.contracts import (
    OddsAvailability,
    OddsProviderEventV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
    WeatherRelevance,
    WeatherStatus,
)

REQUESTED_DATE = "2026-07-27"
AS_OF = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
SLATE_OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
BIA_OBSERVED = datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc)
ODDS_RETRIEVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
WEATHER_RETRIEVED = datetime(2026, 7, 27, 14, 16, tzinfo=timezone.utc)
RAW_SLATE_CHECKSUM = "a" * 64

TEAM_NAMES = {
    "LAD": "Los Angeles Dodgers",
    "SF": "San Francisco Giants",
    "TB": "Tampa Bay Rays",
    "NYY": "New York Yankees",
    "HOU": "Houston Astros",
    "TEX": "Texas Rangers",
}
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


def _coverage() -> TeamIntelligenceCoverageV1:
    return TeamIntelligenceCoverageV1(
        gameday_player_count=0,
        resolved_player_count=0,
        player_feature_count=0,
        lineup_player_count=0,
        lineup_feature_count=0,
        bullpen_player_count=0,
        bullpen_feature_count=0,
        bench_player_count=0,
        bench_feature_count=0,
        starter_feature_available=False,
    )


def _team_intelligence(team_id: str) -> TeamBaseballIntelligenceV1:
    return TeamBaseballIntelligenceV1(
        team_id=team_id,
        source_team_id=TEAM_SOURCE_IDS[team_id],
        starter_source_player_id=None,
        lineup_source_player_ids=(),
        bullpen_source_player_ids=(),
        bench_source_player_ids=(),
        batter_source_player_ids=(),
        pitcher_source_player_ids=(),
        players=(),
        coverage=_coverage(),
    )


def _slate_game(
    game_pk: int,
    *,
    away: str = "SF",
    home: str = "LAD",
    start: datetime | None = None,
    game_number: int = 1,
    venue_name: str | None = None,
    doubleheader: DailySlateDoubleheaderStatus = DailySlateDoubleheaderStatus.SINGLE,
) -> DailySlateGameV1:
    source_id = str(game_pk)
    scheduled = start or datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date=REQUESTED_DATE,
        scheduled_start_time=scheduled,
        away_team_id=away,
        home_team_id=home,
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=game_number,
        doubleheader_status=doubleheader,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id=source_id,
        source_provider="mlb",
        observed_at=SLATE_OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=source_id,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum=RAW_SLATE_CHECKSUM,
        ),
        source_home_team_id=TEAM_SOURCE_IDS[home],
        source_away_team_id=TEAM_SOURCE_IDS[away],
        source_venue_id="22",
        source_venue_name=venue_name or VENUE_NAMES.get(home),
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    return DailySlateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=games,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum=RAW_SLATE_CHECKSUM,
        ),
    )


def _bia(slate: DailySlateV1) -> BaseballIntelligenceAssemblyV1:
    games = tuple(
        BaseballIntelligenceGameV1(
            edge_event_id=game.edge_event_id,
            daily_mlb_game_id=game.daily_mlb_game_id,
            source_game_id=game.source_game_id,
            away_team_id=game.away_team_id,
            home_team_id=game.home_team_id,
            venue_id=game.venue_id,
            game_status=game.game_status,
            away=_team_intelligence(game.away_team_id),
            home=_team_intelligence(game.home_team_id),
            upstream_daily_slate_game_checksum=game.checksum,
            upstream_game_state_game_checksum=hashlib.sha256(
                f"game-state:{game.source_game_id}".encode()
            ).hexdigest(),
        )
        for game in slate.games
    )
    return BaseballIntelligenceAssemblyV1(
        requested_date=slate.requested_date,
        as_of_time=slate.as_of_time,
        observed_at=BIA_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum="b" * 64,
        source_stats_run_ids=(),
        source_feature_checksums=(),
        games=games,
    )


def _book(
    key: str,
    *,
    home_name: str,
    away_name: str,
    last_update: datetime = ODDS_RETRIEVED - timedelta(seconds=30),
    home_ml: int = -120,
    away_ml: int = 110,
) -> dict[str, object]:
    update = last_update.isoformat().replace("+00:00", "Z")
    return {
        "key": key,
        "title": key.title(),
        "last_update": update,
        "markets": [
            {
                "key": "h2h",
                "last_update": update,
                "outcomes": [
                    {"name": home_name, "price": home_ml},
                    {"name": away_name, "price": away_ml},
                ],
            },
            {
                "key": "spreads",
                "last_update": update,
                "outcomes": [
                    {"name": home_name, "price": -110, "point": -1.5},
                    {"name": away_name, "price": -110, "point": 1.5},
                ],
            },
            {
                "key": "totals",
                "last_update": update,
                "outcomes": [
                    {"name": "Over", "price": -108, "point": 8.5},
                    {"name": "Under", "price": -112, "point": 8.5},
                ],
            },
        ],
    }


def _odds_event(
    provider_event_id: str,
    *,
    away: str = "SF",
    home: str = "LAD",
    commence: datetime | None = None,
    retrieved_at: datetime = ODDS_RETRIEVED,
    variant: int = 0,
    stale: bool = False,
) -> OddsProviderEventV1:
    start = commence or datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)
    home_name = TEAM_NAMES[home]
    away_name = TEAM_NAMES[away]
    last_update = (
        retrieved_at - timedelta(minutes=20)
        if stale
        else retrieved_at - timedelta(seconds=30)
    )
    event = {
        "id": provider_event_id,
        "sport_key": "baseball_mlb",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "home_team": home_name,
        "away_team": away_name,
        "last_update": last_update.isoformat().replace("+00:00", "Z"),
        "bookmakers": [
            _book(
                "book-a",
                home_name=home_name,
                away_name=away_name,
                last_update=last_update,
                home_ml=-120 - variant,
                away_ml=110 + variant,
            ),
            _book(
                "book-b",
                home_name=home_name,
                away_name=away_name,
                last_update=last_update,
                home_ml=-118 - variant,
                away_ml=108 + variant,
            ),
        ],
    }
    checksum = hashlib.sha256(
        f"odds:{provider_event_id}:{retrieved_at.isoformat()}:{variant}".encode()
    ).hexdigest()
    return OddsProviderEventV1(
        provider_event_id=provider_event_id,
        retrieved_at=retrieved_at,
        raw_capture_checksum=checksum,
        event=event,
    )


def _weather(
    game: DailySlateGameV1,
    provider: WeatherProvider,
    *,
    retrieved_at: datetime = WEATHER_RETRIEVED,
    forecast_time: datetime | None = None,
    reported_offset: float = 0.0,
    temperature: float = 72.0,
    wind_speed: float = 8.0,
    wind_direction: float = 270.0,
    precipitation: float = 10.0,
    variant: int = 0,
) -> WeatherForecastEvidenceV1:
    assert game.scheduled_start_time is not None
    target = forecast_time or game.scheduled_start_time
    forecast: dict[str, object] = {
        "forecast_time": target.isoformat(),
        "forecast_offset_minutes": reported_offset,
        "forecast_generated_at": (retrieved_at - timedelta(minutes=5)).isoformat(),
        "temperature_f": temperature,
        "humidity_pct": 45.0,
        "precipitation_probability_pct": precipitation,
        "wind_speed_mph": wind_speed,
        "wind_direction_deg": wind_direction,
        "short_forecast": "Clear",
    }
    if provider is WeatherProvider.OPENWEATHER:
        forecast.update(
            {
                "wind_gust_mph": wind_speed + 3.0,
                "clouds_pct": 10.0,
                "pressure_hpa": 1012.0,
            }
        )
    checksum = hashlib.sha256(
        f"weather:{game.source_game_id}:{provider.value}:{retrieved_at.isoformat()}:{variant}".encode()
    ).hexdigest()
    return WeatherForecastEvidenceV1(
        source_game_id=game.source_game_id,
        provider=provider,
        retrieved_at=retrieved_at,
        raw_capture_checksums=(checksum,),
        forecast=forecast,
    )


def test_zero_game_snapshot_is_valid_and_deterministic() -> None:
    slate = _slate()
    bia = _bia(slate)

    first = assemble_odds_weather(slate=slate, baseball_intelligence=bia)
    second = assemble_odds_weather(slate=slate, baseball_intelligence=bia)

    assert first.snapshot.games == ()
    assert first.snapshot.warnings == ()
    assert first.snapshot.source_raw_capture_checksums == ()
    assert first.snapshot.checksum == second.snapshot.checksum


def test_complete_odds_and_dual_weather_sources_are_canonically_assembled() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    bia = _bia(slate)
    odds = _odds_event("odds-event-1")
    nws = _weather(game, WeatherProvider.NWS)
    owm = _weather(
        game,
        WeatherProvider.OPENWEATHER,
        temperature=74.0,
        wind_speed=10.0,
        precipitation=20.0,
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=bia,
        odds_events=(odds,),
        weather_evidence=(nws, owm),
    )

    assembled = result.snapshot.games[0]
    assert assembled.source_game_id == "900001"
    assert assembled.odds.availability is OddsAvailability.AVAILABLE
    assert assembled.odds.provider_event_id == "odds-event-1"
    assert assembled.odds.normalized_market_count == 3
    assert assembled.odds.summary is not None
    assert set(assembled.odds.summary["markets"]) == {"h2h", "spreads", "totals"}
    h2h = assembled.odds.summary["markets"]["h2h"]["lines"]["moneyline"]
    assert h2h["complete_two_way_market"] is True
    assert h2h["outcomes"]["LAD"]["no_vig_probability"] is not None
    assert assembled.weather.status is WeatherStatus.AVAILABLE
    assert assembled.weather.primary_source is WeatherProvider.NWS
    assert assembled.weather.comparison["agreement"] == "strong"
    assert len(result.snapshot.source_raw_capture_checksums) == 3


def test_doubleheader_single_provider_event_matches_only_nearest_game() -> None:
    first = _slate_game(
        900001,
        start=datetime(2026, 7, 27, 19, 10, tzinfo=timezone.utc),
        game_number=1,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    second = _slate_game(
        900002,
        start=datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc),
        game_number=2,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    slate = _slate(first, second)
    event = _odds_event(
        "doubleheader-event-2",
        commence=datetime(2026, 7, 27, 23, 15, tzinfo=timezone.utc),
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        odds_events=(event,),
    )

    assert result.snapshot.games[0].odds.availability is OddsAvailability.UNAVAILABLE
    assert result.snapshot.games[1].odds.availability is OddsAvailability.AVAILABLE
    assert result.snapshot.games[1].odds.event_match_offset_minutes == pytest.approx(5.0)


def test_equal_distance_doubleheader_match_fails_closed() -> None:
    first = _slate_game(
        900001,
        start=datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc),
        game_number=1,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    second = _slate_game(
        900002,
        start=datetime(2026, 7, 27, 22, 0, tzinfo=timezone.utc),
        game_number=2,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    slate = _slate(first, second)
    event = _odds_event(
        "ambiguous-event",
        commence=datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(OddsWeatherAssemblyError, match="equally close"):
        assemble_odds_weather(
            slate=slate,
            baseball_intelligence=_bia(slate),
            odds_events=(event,),
        )


def test_equal_competing_provider_events_for_one_game_fail_closed() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    first = _odds_event("event-a")
    second = _odds_event("event-b")

    with pytest.raises(OddsWeatherAssemblyError, match="competing provider odds events"):
        assemble_odds_weather(
            slate=slate,
            baseball_intelligence=_bia(slate),
            odds_events=(first, second),
        )


def test_unmatched_provider_event_is_excluded_with_warning() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    unmatched = _odds_event(
        "far-event",
        commence=datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc),
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        odds_events=(unmatched,),
    )

    codes = {warning.code for warning in result.warnings}
    assert "odds_event_unmatched" in codes
    assert "odds_event_missing" in codes
    assert result.snapshot.games[0].odds.availability is OddsAvailability.UNAVAILABLE


def test_latest_odds_revision_at_cutoff_is_selected_and_future_revision_ignored() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    earlier = _odds_event("event-1", variant=0)
    future = _odds_event(
        "event-1",
        retrieved_at=ODDS_RETRIEVED + timedelta(hours=1),
        variant=8,
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        odds_events=(earlier, future),
        observed_at=ODDS_RETRIEVED + timedelta(minutes=10),
    )

    assembled = result.snapshot.games[0].odds
    assert assembled.raw_capture_checksum == earlier.raw_capture_checksum
    assert "future_odds_revision_ignored" in {warning.code for warning in result.warnings}


def test_stale_market_warning_from_frozen_odds_processor_is_preserved() -> None:
    game = _slate_game(900001)
    slate = _slate(game)

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        odds_events=(_odds_event("stale-event", stale=True),),
    )

    assert "stale_market" in {warning.code for warning in result.warnings}
    freshness = result.snapshot.games[0].odds.freshness_counts
    assert int(freshness["stale"]) == 6


def test_single_source_weather_remains_available_with_explicit_warning() -> None:
    game = _slate_game(900001)
    slate = _slate(game)

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        weather_evidence=(_weather(game, WeatherProvider.NWS),),
    )

    weather = result.snapshot.games[0].weather
    assert weather.status is WeatherStatus.AVAILABLE
    assert weather.primary_source is WeatherProvider.NWS
    assert weather.comparison["agreement"] == "single_source"
    assert "weather_secondary_source_missing" in {
        warning.code for warning in result.warnings
    }


def test_future_weather_revision_is_ignored_at_point_in_time_cutoff() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    earlier = _weather(game, WeatherProvider.NWS, temperature=70.0)
    future = _weather(
        game,
        WeatherProvider.NWS,
        retrieved_at=WEATHER_RETRIEVED + timedelta(hours=1),
        temperature=90.0,
        variant=2,
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        weather_evidence=(earlier, future),
        observed_at=WEATHER_RETRIEVED + timedelta(minutes=10),
    )

    selected = result.snapshot.games[0].weather.nws
    assert selected is not None
    assert selected.forecast["temperature_f"] == 70.0
    assert "future_weather_revision_ignored" in {
        warning.code for warning in result.warnings
    }


def test_fixed_roof_indoor_game_suppresses_supplied_weather() -> None:
    game = _slate_game(
        900010,
        away="NYY",
        home="TB",
        venue_name="Tropicana Field",
    )
    slate = _slate(game)

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        weather_evidence=(
            _weather(game, WeatherProvider.NWS),
            _weather(game, WeatherProvider.OPENWEATHER),
        ),
    )

    weather = result.snapshot.games[0].weather
    assert weather.status is WeatherStatus.INDOOR_FIXED_ROOF
    assert weather.relevance is WeatherRelevance.INDOOR_SUPPRESSED
    assert weather.primary_source is None
    assert weather.nws is None
    assert weather.openweather is None
    assert "weather_evidence_ignored_fixed_roof" in {
        warning.code for warning in result.warnings
    }


def test_daily_slate_venue_mismatch_blocks_weather_without_guessing() -> None:
    game = _slate_game(900001, venue_name="Wrigley Field")
    slate = _slate(game)

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        weather_evidence=(_weather(game, WeatherProvider.NWS),),
    )

    weather = result.snapshot.games[0].weather
    assert weather.status is WeatherStatus.UNAVAILABLE
    assert "weather_unavailable_invalid_stadium_metadata" in {
        warning.code for warning in result.warnings
    }


def test_weather_forecast_time_is_revalidated_against_authoritative_first_pitch() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    assert game.scheduled_start_time is not None
    bad = _weather(
        game,
        WeatherProvider.NWS,
        forecast_time=game.scheduled_start_time + timedelta(hours=2),
        reported_offset=30.0,
    )

    with pytest.raises(OddsWeatherAssemblyError, match="does not cover first pitch"):
        assemble_odds_weather(
            slate=slate,
            baseball_intelligence=_bia(slate),
            weather_evidence=(bad,),
        )


def test_retractable_roof_weather_is_contextual_not_suppressed() -> None:
    game = _slate_game(
        900020,
        away="TEX",
        home="HOU",
        venue_name="Daikin Park",
    )
    slate = _slate(game)

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_bia(slate),
        weather_evidence=(_weather(game, WeatherProvider.NWS),),
    )

    weather = result.snapshot.games[0].weather
    assert weather.status is WeatherStatus.AVAILABLE
    assert weather.relevance is WeatherRelevance.CONTEXTUAL_ROOF_STATUS_UNKNOWN


def test_upstream_daily_slate_checksum_mismatch_fails_closed() -> None:
    game = _slate_game(900001)
    slate = _slate(game)
    bia = _bia(slate)
    wrong = BaseballIntelligenceAssemblyV1(
        requested_date=bia.requested_date,
        as_of_time=bia.as_of_time,
        observed_at=bia.observed_at,
        upstream_daily_slate_checksum="f" * 64,
        upstream_game_state_checksum=bia.upstream_game_state_checksum,
        source_stats_run_ids=bia.source_stats_run_ids,
        source_feature_checksums=bia.source_feature_checksums,
        games=bia.games,
    )

    with pytest.raises(OddsWeatherAssemblyError, match="supplied DailySlate"):
        assemble_odds_weather(slate=slate, baseball_intelligence=wrong)
