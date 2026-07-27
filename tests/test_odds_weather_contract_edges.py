from __future__ import annotations

import hashlib
from datetime import datetime, timezone

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
from app.odds_weather.assembly import assemble_odds_weather
from app.odds_weather.contracts import (
    OddsAvailability,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    WeatherStatus,
)

OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)


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


def _team(team_id: str, source_team_id: str) -> TeamBaseballIntelligenceV1:
    return TeamBaseballIntelligenceV1(
        team_id=team_id,
        source_team_id=source_team_id,
        starter_source_player_id=None,
        lineup_source_player_ids=(),
        bullpen_source_player_ids=(),
        bench_source_player_ids=(),
        batter_source_player_ids=(),
        pitcher_source_player_ids=(),
        players=(),
        coverage=_coverage(),
    )


def _zero_slate() -> DailySlateV1:
    return DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        observed_at=OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=(),
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum="a" * 64,
        ),
    )


def _zero_bia(slate: DailySlateV1) -> BaseballIntelligenceAssemblyV1:
    return BaseballIntelligenceAssemblyV1(
        requested_date=slate.requested_date,
        as_of_time=slate.as_of_time,
        observed_at=OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum="b" * 64,
        source_stats_run_ids=(),
        source_feature_checksums=(),
        games=(),
    )


def test_source_collector_warning_is_preserved_in_canonical_snapshot() -> None:
    slate = _zero_slate()
    warning = OddsWeatherWarningV1(
        code="malformed_market",
        domain=OddsWeatherWarningDomain.ODDS,
        message="Excluded malformed provider market",
        provider="book-a",
        provider_event_id="event-1",
    )

    result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=_zero_bia(slate),
        source_warnings=(warning,),
    )

    assert result.snapshot.warnings == (warning,)


def test_missing_scheduled_start_degrades_game_instead_of_crashing_phase() -> None:
    game = DailySlateGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        official_date="2026-07-27",
        scheduled_start_time=None,
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
    slate = DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        observed_at=OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=(game,),
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum="a" * 64,
        ),
    )
    intelligence_game = BaseballIntelligenceGameV1(
        edge_event_id=game.edge_event_id,
        daily_mlb_game_id=game.daily_mlb_game_id,
        source_game_id=game.source_game_id,
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        game_status=game.game_status,
        away=_team("SF", "137"),
        home=_team("LAD", "119"),
        upstream_daily_slate_game_checksum=game.checksum,
        upstream_game_state_game_checksum=hashlib.sha256(b"game-state").hexdigest(),
    )
    bia = BaseballIntelligenceAssemblyV1(
        requested_date=slate.requested_date,
        as_of_time=slate.as_of_time,
        observed_at=OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum="b" * 64,
        source_stats_run_ids=(),
        source_feature_checksums=(),
        games=(intelligence_game,),
    )

    result = assemble_odds_weather(slate=slate, baseball_intelligence=bia)

    assembled = result.snapshot.games[0]
    assert assembled.scheduled_start_time is None
    assert assembled.odds.availability is OddsAvailability.UNAVAILABLE
    assert assembled.weather.status is WeatherStatus.UNAVAILABLE
    codes = {warning.code for warning in result.snapshot.warnings}
    assert "odds_unavailable_missing_start_time" in codes
    assert "weather_unavailable_missing_start_time" in codes
