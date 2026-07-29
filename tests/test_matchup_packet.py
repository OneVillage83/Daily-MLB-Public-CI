from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.baseball_intelligence.assembly import assemble_baseball_intelligence
from app.baseball_intelligence.contracts import BaseballIntelligenceAssemblyV1
from app.data_quality.contracts import (
    DATA_QUALITY_POLICY_VERSION,
    DataQualityDisposition,
    DataQualityGameV1,
    DataQualityV1,
    QualityDomain,
    QualityIssueSeverity,
    QualityIssueV1,
)
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
from app.matchup_packet.assembly import (
    MatchupPacketAssemblyError,
    assemble_matchup_packet,
)
from app.matchup_packet.contracts import MatchupPacketV1
from app.odds_weather.assembly import assemble_odds_weather
from app.odds_weather.contracts import OddsWeatherV1

REQUESTED_DATE = "2026-07-27"
AS_OF = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
SLATE_OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
STATE_OBSERVED = datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc)
OW_OBSERVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
DQ_OBSERVED = datetime(2026, 7, 27, 14, 20, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)


def _slate_game(game_pk: int = 900001) -> DailySlateGameV1:
    source_id = str(game_pk)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date=REQUESTED_DATE,
        scheduled_start_time=START,
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
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


def _state(slate: DailySlateV1, *games: GameStateGameV1) -> GameStateV1:
    return GameStateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=STATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=slate.checksum,
        games=games,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="game_state_snapshot",
            upstream_checksum=slate.checksum,
        ),
    )


def _quality_issue(severity: QualityIssueSeverity) -> QualityIssueV1:
    return QualityIssueV1(
        code=f"fixture_{severity.value}",
        domain=QualityDomain.GAME_STATE,
        severity=severity,
        message=f"Fixture {severity.value} quality issue",
    )


def _quality_game(
    slate_game: DailySlateGameV1,
    state_game: GameStateGameV1,
    bia_game: object,
    ow_game: object,
    *,
    disposition: DataQualityDisposition,
    issues: tuple[QualityIssueV1, ...],
    away_team_id: str | None = None,
    scheduled_start_time: datetime | None = START,
    daily_slate_checksum: str | None = None,
) -> DataQualityGameV1:
    return DataQualityGameV1(
        edge_event_id=slate_game.edge_event_id,
        daily_mlb_game_id=slate_game.daily_mlb_game_id,
        source_game_id=slate_game.source_game_id,
        away_team_id=away_team_id or slate_game.away_team_id,
        home_team_id=slate_game.home_team_id,
        scheduled_start_time=scheduled_start_time,
        upstream_daily_slate_game_checksum=daily_slate_checksum or slate_game.checksum,
        upstream_game_state_game_checksum=state_game.checksum,
        upstream_baseball_intelligence_game_checksum=str(getattr(bia_game, "checksum")),
        upstream_odds_weather_game_checksum=str(getattr(ow_game, "checksum")),
        disposition=disposition,
        issues=issues,
    )


def _quality_snapshot(
    slate: DailySlateV1,
    state: GameStateV1,
    bia: BaseballIntelligenceAssemblyV1,
    ow: OddsWeatherV1,
    *games: DataQualityGameV1,
    as_of_time: datetime = AS_OF,
    upstream_odds_weather_checksum: str | None = None,
) -> DataQualityV1:
    return DataQualityV1(
        requested_date=REQUESTED_DATE,
        as_of_time=as_of_time,
        observed_at=DQ_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_game_state_checksum=state.checksum,
        upstream_baseball_intelligence_checksum=bia.checksum,
        upstream_odds_weather_checksum=upstream_odds_weather_checksum or ow.checksum,
        games=games,
        policy_version=DATA_QUALITY_POLICY_VERSION,
    )


def _one_game_chain(
    *,
    disposition: DataQualityDisposition = DataQualityDisposition.READY,
) -> tuple[DailySlateV1, GameStateV1, BaseballIntelligenceAssemblyV1, OddsWeatherV1, DataQualityV1]:
    slate_game = _slate_game()
    slate = _slate(slate_game)
    state_game = _state_game(slate_game)
    state = _state(slate, state_game)
    bia = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=(),
        observed_at=STATE_OBSERVED,
    ).assembly
    ow = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=bia,
        odds_events=(),
        weather_evidence=(),
        observed_at=OW_OBSERVED,
    ).snapshot
    issues: tuple[QualityIssueV1, ...]
    if disposition is DataQualityDisposition.READY:
        issues = ()
    elif disposition is DataQualityDisposition.DEGRADED:
        issues = (_quality_issue(QualityIssueSeverity.WARNING),)
    else:
        issues = (_quality_issue(QualityIssueSeverity.CRITICAL),)
    quality_game = _quality_game(
        slate_game,
        state_game,
        bia.games[0],
        ow.games[0],
        disposition=disposition,
        issues=issues,
    )
    quality = _quality_snapshot(slate, state, bia, ow, quality_game)
    return slate, state, bia, ow, quality


def _zero_game_chain() -> tuple[DailySlateV1, GameStateV1, BaseballIntelligenceAssemblyV1, OddsWeatherV1, DataQualityV1]:
    slate = _slate()
    state = _state(slate)
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
    ow = OddsWeatherV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=OW_OBSERVED,
        upstream_daily_slate_checksum=slate.checksum,
        upstream_baseball_intelligence_checksum=bia.checksum,
        source_raw_capture_checksums=(),
        games=(),
        warnings=(),
    )
    quality = _quality_snapshot(slate, state, bia, ow)
    return slate, state, bia, ow, quality


def test_zero_game_packet_is_valid_deterministic_and_retains_lineage() -> None:
    slate, state, bia, ow, quality = _zero_game_chain()

    first = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )
    second = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )

    assert first.games == ()
    assert first.upstream_data_quality_checksum == quality.checksum
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()


def test_ready_game_packages_exact_phase_rows() -> None:
    slate, state, bia, ow, quality = _one_game_chain()

    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )

    assert len(packet.games) == 1
    game = packet.games[0]
    assert game.schedule is slate.games[0]
    assert game.game_state is state.games[0]
    assert game.baseball_intelligence is bia.games[0]
    assert game.odds_weather is ow.games[0]
    assert game.data_quality is quality.games[0]
    assert game.quality_disposition is DataQualityDisposition.READY
    assert packet.ready_game_count == 1
    assert packet.degraded_game_count == 0
    assert packet.insufficient_game_count == 0


def test_degraded_game_is_preserved_without_quality_recomputation() -> None:
    slate, state, bia, ow, quality = _one_game_chain(
        disposition=DataQualityDisposition.DEGRADED
    )

    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )

    assert len(packet.games) == 1
    game = packet.games[0]
    assert game.quality_disposition is DataQualityDisposition.DEGRADED
    assert game.data_quality.issues == quality.games[0].issues
    assert packet.degraded_game_count == 1


def test_insufficient_game_is_preserved_not_filtered() -> None:
    slate, state, bia, ow, quality = _one_game_chain(
        disposition=DataQualityDisposition.INSUFFICIENT
    )

    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )

    assert len(packet.games) == 1
    assert packet.games[0].quality_disposition is DataQualityDisposition.INSUFFICIENT
    assert packet.insufficient_game_count == 1
    assert packet.games[0].data_quality.issues == quality.games[0].issues


def test_packet_observed_at_defaults_to_latest_upstream_observation() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )
    assert packet.observed_at == DQ_OBSERVED


def test_packet_observed_at_cannot_precede_upstream_evidence() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    with pytest.raises(MatchupPacketAssemblyError, match="cannot precede"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=quality,
            observed_at=DQ_OBSERVED - timedelta(minutes=1),
        )


def test_data_quality_as_of_mismatch_fails_closed() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    mismatched = _quality_snapshot(
        slate,
        state,
        bia,
        ow,
        *quality.games,
        as_of_time=AS_OF + timedelta(minutes=1),
    )
    with pytest.raises(MatchupPacketAssemblyError, match="as_of_time"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=mismatched,
        )


def test_top_level_data_quality_lineage_break_fails_closed() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    mismatched = _quality_snapshot(
        slate,
        state,
        bia,
        ow,
        *quality.games,
        upstream_odds_weather_checksum="f" * 64,
    )
    with pytest.raises(MatchupPacketAssemblyError, match="DataQuality upstream OddsWeather"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=mismatched,
        )


def test_per_game_daily_slate_checksum_break_fails_closed() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    bad_game = _quality_game(
        slate.games[0],
        state.games[0],
        bia.games[0],
        ow.games[0],
        disposition=DataQualityDisposition.READY,
        issues=(),
        daily_slate_checksum="f" * 64,
    )
    mismatched = _quality_snapshot(slate, state, bia, ow, bad_game)

    with pytest.raises(MatchupPacketAssemblyError, match="lineage contract"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=mismatched,
        )


def test_per_game_team_identity_mismatch_fails_closed() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    bad_game = _quality_game(
        slate.games[0],
        state.games[0],
        bia.games[0],
        ow.games[0],
        disposition=DataQualityDisposition.READY,
        issues=(),
        away_team_id="NYY",
    )
    mismatched = _quality_snapshot(slate, state, bia, ow, bad_game)

    with pytest.raises(MatchupPacketAssemblyError, match="lineage contract"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=mismatched,
        )


def test_data_quality_start_time_mismatch_fails_closed() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    bad_game = _quality_game(
        slate.games[0],
        state.games[0],
        bia.games[0],
        ow.games[0],
        disposition=DataQualityDisposition.READY,
        issues=(),
        scheduled_start_time=START + timedelta(minutes=5),
    )
    mismatched = _quality_snapshot(slate, state, bia, ow, bad_game)

    with pytest.raises(MatchupPacketAssemblyError, match="lineage contract"):
        assemble_matchup_packet(
            slate=slate,
            game_state=state,
            baseball_intelligence=bia,
            odds_weather=ow,
            data_quality=mismatched,
        )


def test_top_level_packet_retains_all_five_snapshot_checksums() -> None:
    slate, state, bia, ow, quality = _one_game_chain()
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=ow,
        data_quality=quality,
    )
    assert packet.upstream_daily_slate_checksum == slate.checksum
    assert packet.upstream_game_state_checksum == state.checksum
    assert packet.upstream_baseball_intelligence_checksum == bia.checksum
    assert packet.upstream_odds_weather_checksum == ow.checksum
    assert packet.upstream_data_quality_checksum == quality.checksum


def test_packet_contract_rejects_configured_secret_material() -> None:
    with pytest.raises(ValueError, match="credential-bearing"):
        MatchupPacketV1(
            requested_date=REQUESTED_DATE,
            as_of_time=AS_OF,
            observed_at=DQ_OBSERVED,
            upstream_daily_slate_checksum="a" * 64,
            upstream_game_state_checksum="b" * 64,
            upstream_baseball_intelligence_checksum="c" * 64,
            upstream_odds_weather_checksum="d" * 64,
            upstream_data_quality_checksum="e" * 64,
            games=(),
            secret_values=("2026",),
        )
