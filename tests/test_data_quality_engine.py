from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.baseball_intelligence.assembly import assemble_baseball_intelligence
from app.baseball_intelligence.contracts import BaseballIntelligenceAssemblyV1
from app.data_quality.contracts import DataQualityDisposition
from app.data_quality.engine import DataQualityAssessmentError, assess_data_quality
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
)
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateGameV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)
from app.odds_weather.assembly import assemble_odds_weather
from app.odds_weather.contracts import OddsWeatherV1

REQUESTED_DATE = "2026-07-27"
AS_OF = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
SLATE_OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
STATE_OBSERVED = datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc)
OW_OBSERVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)


def _slate_game() -> DailySlateGameV1:
    return DailySlateGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        official_date=REQUESTED_DATE,
        scheduled_start_time=START,
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id="900001",
        source_provider="mlb",
        observed_at=SLATE_OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id="900001",
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum="a" * 64,
        ),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
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
            upstream_checksum="a" * 64,
        ),
    )


def _unavailable_team(team_id: str, source_team_id: str) -> TeamGameStateV1:
    return TeamGameStateV1(
        team_id=team_id,
        source_team_id=source_team_id,
        starter=StarterStateV1(certainty=StarterCertainty.UNAVAILABLE),
        lineup=LineupStateV1(availability=LineupAvailability.UNAVAILABLE),
        personnel=GamedayPersonnelV1(available=False),
    )


def _state(
    slate: DailySlateV1,
    *games: GameStateGameV1,
    upstream_checksum: str | None = None,
) -> GameStateV1:
    selected_upstream = upstream_checksum or slate.checksum
    return GameStateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=STATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=selected_upstream,
        games=games,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="game_state_snapshot",
            upstream_checksum=selected_upstream,
        ),
    )


def _state_game(slate_game: DailySlateGameV1) -> GameStateGameV1:
    return GameStateGameV1(
        edge_event_id=slate_game.edge_event_id,
        daily_mlb_game_id=slate_game.daily_mlb_game_id,
        source_game_id=slate_game.source_game_id,
        away_team_id=slate_game.away_team_id,
        home_team_id=slate_game.home_team_id,
        game_status=DailySlateGameStatus.PREGAME,
        away=_unavailable_team("SF", "137"),
        home=_unavailable_team("LAD", "119"),
        observed_at=STATE_OBSERVED,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=slate_game.source_game_id,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="Pre-Game",
            upstream_checksum="b" * 64,
        ),
    )


def _empty_bia(slate: DailySlateV1, state: GameStateV1) -> BaseballIntelligenceAssemblyV1:
    return BaseballIntelligenceAssemblyV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=STATE_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum=state.checksum,
        source_stats_run_ids=(),
        source_feature_checksums=(),
        games=(),
    )


def _empty_ow(slate: DailySlateV1, bia: BaseballIntelligenceAssemblyV1) -> OddsWeatherV1:
    return OddsWeatherV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=OW_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_baseball_intelligence_checksum=bia.checksum,
        source_raw_capture_checksums=(),
        games=(),
        warnings=(),
    )


def test_zero_game_data_quality_is_valid_and_deterministic() -> None:
    slate = _slate()
    state = _state(slate)
    bia = _empty_bia(slate, state)
    odds_weather = _empty_ow(slate, bia)
    first = assess_data_quality(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=odds_weather,
    )
    second = assess_data_quality(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=odds_weather,
    )
    assert first.snapshot.games == ()
    assert first.snapshot.checksum == second.snapshot.checksum
    assert first.snapshot.canonical_json_bytes() == second.snapshot.canonical_json_bytes()


def test_insufficient_game_is_preserved_not_filtered() -> None:
    slate_game = _slate_game()
    slate = _slate(slate_game)
    state_game = _state_game(slate_game)
    state = _state(slate, state_game)
    bia_result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=(),
        observed_at=STATE_OBSERVED,
    )
    odds_weather_result = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=bia_result.assembly,
        odds_events=(),
        weather_evidence=(),
        observed_at=OW_OBSERVED,
    )
    result = assess_data_quality(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia_result.assembly,
        odds_weather=odds_weather_result.snapshot,
    )
    assert len(result.snapshot.games) == 1
    quality_game = result.snapshot.games[0]
    assert quality_game.source_game_id == slate_game.source_game_id
    assert quality_game.disposition is DataQualityDisposition.INSUFFICIENT
    codes = {issue.code for issue in quality_game.issues}
    assert "starter_unavailable" in codes
    assert "team_player_intelligence_unavailable" in codes
    assert "odds_unavailable" in codes
    assert "weather_unavailable" in codes


def test_upstream_snapshot_checksum_break_fails_closed() -> None:
    slate = _slate()
    state = _state(slate, upstream_checksum="e" * 64)
    bia = BaseballIntelligenceAssemblyV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=STATE_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum=state.checksum,
        source_stats_run_ids=(),
        source_feature_checksums=(),
        games=(),
    )
    odds_weather = _empty_ow(slate, bia)
    with pytest.raises(DataQualityAssessmentError, match="GameState"):
        assess_data_quality(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=odds_weather,
        )


def test_observed_at_cannot_precede_upstream_evidence() -> None:
    slate = _slate()
    state = _state(slate)
    bia = _empty_bia(slate, state)
    odds_weather = _empty_ow(slate, bia)
    with pytest.raises(DataQualityAssessmentError, match="cannot precede"):
        assess_data_quality(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=odds_weather,
            observed_at=STATE_OBSERVED,
        )
