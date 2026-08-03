from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from app.baseball_intelligence.contracts import (
    TeamBaseballIntelligenceV1,
    TeamIntelligenceCoverageV1,
)
from app.data_quality.contracts import DataQualityPolicyV1, QualityIssueSeverity
from app.data_quality.engine import (
    _odds_issues,
    _schedule_issues,
    _team_intelligence_issues,
    _team_state_issues,
    _weather_issues,
)
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    VenueMappingStatus,
)
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateProvenanceV1,
    LineupAvailability,
    LineupEntryV1,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)
from app.odds_weather.contracts import (
    OddsAvailability,
    OddsSnapshotV1,
    OddsWeatherGameV1,
    VenueWeatherContextV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
    WeatherRelevance,
    WeatherSnapshotV1,
    WeatherStatus,
)
from app.processors.odds_processor import CALCULATION_VERSION, ODDS_CONSENSUS_CONTRACT_VERSION

OBSERVED = datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)


def _player(source_id: int = 710001) -> GameStatePlayerV1:
    return GameStatePlayerV1(
        source_player_id=str(source_id),
        full_name=f"Player {source_id}",
        player_identity_id=f"identity:mlb:{source_id}",
        canonical_player_id=f"player:canonical:{source_id}",
    )


def _state_team(
    *,
    team_id: str = "LAD",
    source_team_id: str = "119",
    starter_certainty: StarterCertainty = StarterCertainty.CONFIRMED,
    lineup_availability: LineupAvailability = LineupAvailability.POSTED,
    personnel_available: bool = True,
) -> TeamGameStateV1:
    starter_player = None if starter_certainty is StarterCertainty.UNAVAILABLE else _player()
    lineup_entries: tuple[LineupEntryV1, ...] = ()
    if lineup_availability is LineupAvailability.PARTIAL:
        lineup_entries = (LineupEntryV1(player=_player(720001), batting_order_slot=1),)
    elif lineup_availability is LineupAvailability.POSTED:
        lineup_entries = tuple(
            LineupEntryV1(player=_player(720000 + slot), batting_order_slot=slot)
            for slot in range(1, 10)
        )
    personnel = (
        GamedayPersonnelV1(
            available=True,
            batters=tuple(entry.player for entry in lineup_entries),
            pitchers=(() if starter_player is None else (starter_player,)),
        )
        if personnel_available
        else GamedayPersonnelV1(available=False)
    )
    return TeamGameStateV1(
        team_id=team_id,
        source_team_id=source_team_id,
        starter=StarterStateV1(certainty=starter_certainty, player=starter_player),
        lineup=LineupStateV1(availability=lineup_availability, entries=lineup_entries),
        personnel=personnel,
    )


def _slate_game(start: datetime | None = START) -> DailySlateGameV1:
    return DailySlateGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        official_date="2026-07-27",
        scheduled_start_time=start,
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id="900001",
        source_provider="mlb",
        observed_at=OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id="900001",
            observed_at=OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum="a" * 64,
        ),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
    )


def _state_game(status: DailySlateGameStatus = DailySlateGameStatus.PREGAME) -> GameStateGameV1:
    return GameStateGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        source_game_id="900001",
        away_team_id="SF",
        home_team_id="LAD",
        game_status=status,
        away=_state_team(team_id="SF", source_team_id="137"),
        home=_state_team(),
        observed_at=OBSERVED,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id="900001",
            observed_at=OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status=status.value,
            upstream_checksum="b" * 64,
        ),
    )


def _intelligence_team(
    *,
    gameday: int = 10,
    resolved: int = 10,
    features: int = 10,
    lineup_features: int = 9,
    bullpen_features: int = 1,
) -> TeamBaseballIntelligenceV1:
    return TeamBaseballIntelligenceV1(
        team_id="LAD",
        source_team_id="119",
        starter_source_player_id=None,
        lineup_source_player_ids=(),
        bullpen_source_player_ids=(),
        bench_source_player_ids=(),
        batter_source_player_ids=(),
        pitcher_source_player_ids=(),
        players=(),
        coverage=TeamIntelligenceCoverageV1(
            gameday_player_count=gameday,
            resolved_player_count=resolved,
            player_feature_count=features,
            lineup_player_count=9,
            lineup_feature_count=lineup_features,
            bullpen_player_count=1,
            bullpen_feature_count=bullpen_features,
            bench_player_count=0,
            bench_feature_count=0,
            starter_feature_available=True,
        ),
    )


def _unavailable_weather() -> WeatherSnapshotV1:
    return WeatherSnapshotV1(
        status=WeatherStatus.UNAVAILABLE,
        relevance=WeatherRelevance.UNAVAILABLE,
        venue_context=None,
        primary_source=None,
        nws=None,
        openweather=None,
        comparison={"agreement": "unavailable"},
        baseball_wind_impact={
            "classification": "unknown",
            "outfield_component_mph": None,
            "crosswind_component_mph": None,
            "wind_to_degrees": None,
            "reason_code": "weather_forecast_missing",
        },
    )


def _odds_weather_game(
    *, odds: OddsSnapshotV1 | None = None, weather: WeatherSnapshotV1 | None = None
) -> OddsWeatherGameV1:
    return OddsWeatherGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        source_game_id="900001",
        away_team_id="SF",
        home_team_id="LAD",
        scheduled_start_time=START,
        upstream_daily_slate_game_checksum="a" * 64,
        upstream_baseball_intelligence_game_checksum="b" * 64,
        odds=odds
        or OddsSnapshotV1(
            availability=OddsAvailability.UNAVAILABLE,
            provider_event_id=None,
            retrieved_at=None,
            raw_capture_checksum=None,
            event_match_offset_minutes=None,
            summary=None,
            normalized_market_count=0,
            raw_snapshot_count=0,
            freshness_counts={},
        ),
        weather=weather or _unavailable_weather(),
    )


def _available_odds(*, markets: tuple[str, ...], stale: int = 0) -> OddsSnapshotV1:
    return OddsSnapshotV1(
        availability=OddsAvailability.AVAILABLE,
        provider_event_id="odds-event-1",
        retrieved_at=OBSERVED,
        raw_capture_checksum="c" * 64,
        event_match_offset_minutes=0.0,
        summary={
            "contract_version": ODDS_CONSENSUS_CONTRACT_VERSION,
            "calculation_version": CALCULATION_VERSION,
            "event_id": "odds-event-1",
            "home_team_key": "LAD",
            "away_team_key": "SF",
            "markets": {market: {} for market in markets},
        },
        normalized_market_count=len(markets),
        raw_snapshot_count=max(1, len(markets)),
        freshness_counts={"stale": stale},
    )


def _available_weather(primary: WeatherProvider, agreement: str) -> WeatherSnapshotV1:
    forecast = WeatherForecastEvidenceV1(
        source_game_id="900001",
        provider=primary,
        retrieved_at=OBSERVED,
        raw_capture_checksums=("d" * 64,),
        forecast={
            "forecast_time": START.isoformat(),
            "forecast_offset_minutes": 0.0,
            "temperature_f": 70.0,
            "humidity_pct": 40.0,
            "precipitation_probability_pct": 0.0,
            "wind_speed_mph": 5.0,
            "wind_direction_deg": 270.0,
        },
    )
    context = VenueWeatherContextV1(
        team_id="LAD",
        physical_venue_key="dodger-stadium-los-angeles",
        venue_name="Dodger Stadium",
        latitude=34.0739,
        longitude=-118.24,
        timezone_name="America/Los_Angeles",
        roof_type="open",
        operational_roof_status="open",
        roof_verification_state="VERIFIED",
        outfield_bearing_degrees=None,
        outfield_bearing_verification_state="UNVERIFIED",
        metadata_policy_version="DSE_MLB_STADIUM_METADATA_V1",
        catalog_version=4,
    )
    return WeatherSnapshotV1(
        status=WeatherStatus.AVAILABLE,
        relevance=WeatherRelevance.DIRECT,
        venue_context=context,
        primary_source=primary,
        nws=forecast if primary is WeatherProvider.NWS else None,
        openweather=forecast if primary is WeatherProvider.OPENWEATHER else None,
        comparison={"agreement": agreement},
        baseball_wind_impact={
            "classification": "unknown",
            "outfield_component_mph": None,
            "crosswind_component_mph": None,
            "wind_to_degrees": None,
            "reason_code": "outfield_bearing_unverified",
        },
    )


def _codes(issues: Sequence[object]) -> set[str]:
    return {str(getattr(issue, "code")) for issue in issues}


def test_schedule_missing_start_and_postponed_are_critical() -> None:
    issues = _schedule_issues(_slate_game(None), _state_game(DailySlateGameStatus.POSTPONED))
    assert {"scheduled_start_time_missing", "game_postponed"} <= _codes(issues)
    assert all(issue.severity is QualityIssueSeverity.CRITICAL for issue in issues)


def test_delayed_game_is_warning_not_critical() -> None:
    issues = _schedule_issues(_slate_game(), _state_game(DailySlateGameStatus.DELAYED))
    assert _codes(issues) == {"game_delayed"}
    assert issues[0].severity is QualityIssueSeverity.WARNING


def test_probable_starter_is_informational_only() -> None:
    issues = _team_state_issues(_state_team(starter_certainty=StarterCertainty.PROBABLE))
    starter_issue = next(issue for issue in issues if issue.code.startswith("starter_"))
    assert starter_issue.severity is QualityIssueSeverity.INFO


def test_unavailable_starter_is_critical_and_partial_lineup_is_warning() -> None:
    issues = _team_state_issues(
        _state_team(
            starter_certainty=StarterCertainty.UNAVAILABLE,
            lineup_availability=LineupAvailability.PARTIAL,
        )
    )
    by_code = {issue.code: issue for issue in issues}
    assert by_code["starter_unavailable"].severity is QualityIssueSeverity.CRITICAL
    assert by_code["lineup_partial"].severity is QualityIssueSeverity.WARNING


def test_zero_team_player_features_is_critical() -> None:
    issues = _team_intelligence_issues(
        _state_team(), _intelligence_team(features=0, lineup_features=0, bullpen_features=0)
    )
    by_code = {issue.code: issue for issue in issues}
    assert by_code["team_player_features_unavailable"].severity is QualityIssueSeverity.CRITICAL
    assert by_code["lineup_features_unavailable"].severity is QualityIssueSeverity.CRITICAL


def test_partial_identity_and_feature_coverage_are_warnings() -> None:
    issues = _team_intelligence_issues(
        _state_team(), _intelligence_team(gameday=10, resolved=9, features=8, lineup_features=8)
    )
    by_code = {issue.code: issue for issue in issues}
    assert by_code["player_identity_coverage_incomplete"].severity is QualityIssueSeverity.WARNING
    assert by_code["player_feature_coverage_incomplete"].severity is QualityIssueSeverity.WARNING
    assert by_code["lineup_feature_coverage_incomplete"].severity is QualityIssueSeverity.WARNING


def test_odds_unavailable_is_warning_not_critical() -> None:
    issues = _odds_issues(_odds_weather_game())
    assert _codes(issues) == {"odds_unavailable"}
    assert issues[0].severity is QualityIssueSeverity.WARNING


def test_missing_supported_market_and_stale_market_are_warnings() -> None:
    issues = _odds_issues(
        _odds_weather_game(odds=_available_odds(markets=("h2h", "totals"), stale=2))
    )
    assert {"spreads_market_missing", "stale_odds_markets_present"} <= _codes(issues)
    assert all(issue.severity is QualityIssueSeverity.WARNING for issue in issues)


def test_supported_market_policy_changes_assessment_behavior() -> None:
    game = _odds_weather_game(odds=_available_odds(markets=("h2h",), stale=0))
    default_codes = {issue.code for issue in _odds_issues(game)}
    h2h_only = DataQualityPolicyV1(
        policy_version="DSE_DATA_QUALITY_POLICY_H2H_ONLY_TEST_V1",
        supported_markets=("h2h",),
    )
    selected_codes = {issue.code for issue in _odds_issues(game, h2h_only)}
    assert "spreads_market_missing" in default_codes
    assert "totals_market_missing" in default_codes
    assert "spreads_market_missing" not in selected_codes
    assert "totals_market_missing" not in selected_codes


def test_weather_unavailable_is_warning() -> None:
    issues = _weather_issues(_odds_weather_game())
    assert _codes(issues) == {"weather_unavailable"}
    assert issues[0].severity is QualityIssueSeverity.WARNING


def test_openweather_primary_and_weak_agreement_degrade_weather() -> None:
    weather = _available_weather(WeatherProvider.OPENWEATHER, "weak")
    issues = _weather_issues(_odds_weather_game(weather=weather))
    by_code = {issue.code: issue for issue in issues}
    assert by_code["nws_primary_unavailable"].severity is QualityIssueSeverity.WARNING
    assert by_code["weather_provider_agreement_weak"].severity is QualityIssueSeverity.WARNING


def test_unknown_field_relative_wind_is_informational_only() -> None:
    issues = _weather_issues(
        _odds_weather_game(weather=_available_weather(WeatherProvider.NWS, "strong"))
    )
    wind_issue = next(
        issue for issue in issues if issue.code == "field_relative_wind_unavailable"
    )
    assert wind_issue.severity is QualityIssueSeverity.INFO
