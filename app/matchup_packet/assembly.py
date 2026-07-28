from __future__ import annotations

from datetime import datetime, timezone

from app.baseball_intelligence.contracts import BaseballIntelligenceAssemblyV1
from app.data_quality.contracts import DataQualityV1
from app.daily_slate.contracts import DailySlateV1
from app.game_state.contracts import GameStateV1
from app.matchup_packet.contracts import (
    MatchupPacketContractError,
    MatchupPacketGameV1,
    MatchupPacketV1,
)
from app.odds_weather.contracts import OddsWeatherV1


class MatchupPacketAssemblyError(RuntimeError):
    """Fail-closed MatchupPacket V1 assembly error."""


def _require_common_snapshot_identity(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_weather: OddsWeatherV1,
    data_quality: DataQualityV1,
) -> None:
    snapshots = (
        ("GameState", game_state),
        ("Baseball Intelligence", baseball_intelligence),
        ("OddsWeather", odds_weather),
        ("DataQuality", data_quality),
    )
    for name, snapshot in snapshots:
        if snapshot.requested_date != slate.requested_date:
            raise MatchupPacketAssemblyError(
                f"{name} requested_date does not match DailySlate"
            )
        if snapshot.as_of_time != slate.as_of_time:
            raise MatchupPacketAssemblyError(
                f"{name} as_of_time does not match DailySlate"
            )
        if getattr(snapshot, "sport", None) != "MLB" or getattr(
            snapshot, "league", None
        ) != "MLB":
            raise MatchupPacketAssemblyError(f"{name} sport/league must be MLB")
    if getattr(slate, "sport", None) != "MLB" or getattr(
        slate, "league", None
    ) != "MLB":
        raise MatchupPacketAssemblyError("DailySlate sport/league must be MLB")


def _require_top_level_lineage(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_weather: OddsWeatherV1,
    data_quality: DataQualityV1,
) -> None:
    if game_state.upstream_daily_slate_checksum != slate.checksum:
        raise MatchupPacketAssemblyError(
            "GameState upstream DailySlate checksum mismatch"
        )
    if baseball_intelligence.upstream_daily_slate_checksum != slate.checksum:
        raise MatchupPacketAssemblyError(
            "Baseball Intelligence upstream DailySlate checksum mismatch"
        )
    if baseball_intelligence.upstream_game_state_checksum != game_state.checksum:
        raise MatchupPacketAssemblyError(
            "Baseball Intelligence upstream GameState checksum mismatch"
        )
    if odds_weather.upstream_daily_slate_checksum != slate.checksum:
        raise MatchupPacketAssemblyError(
            "OddsWeather upstream DailySlate checksum mismatch"
        )
    if (
        odds_weather.upstream_baseball_intelligence_checksum
        != baseball_intelligence.checksum
    ):
        raise MatchupPacketAssemblyError(
            "OddsWeather upstream Baseball Intelligence checksum mismatch"
        )
    if data_quality.upstream_daily_slate_checksum != slate.checksum:
        raise MatchupPacketAssemblyError(
            "DataQuality upstream DailySlate checksum mismatch"
        )
    if data_quality.upstream_game_state_checksum != game_state.checksum:
        raise MatchupPacketAssemblyError(
            "DataQuality upstream GameState checksum mismatch"
        )
    if (
        data_quality.upstream_baseball_intelligence_checksum
        != baseball_intelligence.checksum
    ):
        raise MatchupPacketAssemblyError(
            "DataQuality upstream Baseball Intelligence checksum mismatch"
        )
    if data_quality.upstream_odds_weather_checksum != odds_weather.checksum:
        raise MatchupPacketAssemblyError(
            "DataQuality upstream OddsWeather checksum mismatch"
        )


def _source_game_ids(snapshot: object) -> tuple[str, ...]:
    games = getattr(snapshot, "games")
    return tuple(str(game.source_game_id) for game in games)


def assemble_matchup_packet(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_weather: OddsWeatherV1,
    data_quality: DataQualityV1,
    observed_at: datetime | None = None,
) -> MatchupPacketV1:
    """Package exact canonical phase 1-5 evidence into MatchupPacketV1."""

    _require_common_snapshot_identity(
        slate=slate,
        game_state=game_state,
        baseball_intelligence=baseball_intelligence,
        odds_weather=odds_weather,
        data_quality=data_quality,
    )
    _require_top_level_lineage(
        slate=slate,
        game_state=game_state,
        baseball_intelligence=baseball_intelligence,
        odds_weather=odds_weather,
        data_quality=data_quality,
    )

    expected_order = _source_game_ids(slate)
    for name, snapshot in (
        ("GameState", game_state),
        ("Baseball Intelligence", baseball_intelligence),
        ("OddsWeather", odds_weather),
        ("DataQuality", data_quality),
    ):
        if _source_game_ids(snapshot) != expected_order:
            raise MatchupPacketAssemblyError(
                f"{name} game ordering/set does not match DailySlate"
            )

    latest_upstream_observation = max(
        slate.observed_at,
        game_state.observed_at,
        baseball_intelligence.observed_at,
        odds_weather.observed_at,
        data_quality.observed_at,
    )
    packet_observed_at = observed_at or latest_upstream_observation
    if packet_observed_at.tzinfo is None or packet_observed_at.utcoffset() is None:
        raise MatchupPacketAssemblyError("MatchupPacket observed_at must be timezone-aware")
    packet_observed_at = packet_observed_at.astimezone(timezone.utc)
    if packet_observed_at < latest_upstream_observation:
        raise MatchupPacketAssemblyError(
            "MatchupPacket observed_at cannot precede upstream evidence"
        )

    try:
        games = tuple(
            MatchupPacketGameV1(
                schedule=slate_game,
                game_state=state_game,
                baseball_intelligence=intelligence_game,
                odds_weather=odds_weather_game,
                data_quality=quality_game,
            )
            for (
                slate_game,
                state_game,
                intelligence_game,
                odds_weather_game,
                quality_game,
            ) in zip(
                slate.games,
                game_state.games,
                baseball_intelligence.games,
                odds_weather.games,
                data_quality.games,
                strict=True,
            )
        )
        return MatchupPacketV1(
            requested_date=slate.requested_date,
            as_of_time=slate.as_of_time,
            observed_at=packet_observed_at,
            upstream_daily_slate_checksum=slate.checksum,
            upstream_game_state_checksum=game_state.checksum,
            upstream_baseball_intelligence_checksum=baseball_intelligence.checksum,
            upstream_odds_weather_checksum=odds_weather.checksum,
            upstream_data_quality_checksum=data_quality.checksum,
            games=games,
        )
    except MatchupPacketContractError as exc:
        raise MatchupPacketAssemblyError(
            "phase 1-5 evidence violates MatchupPacket V1 lineage contract"
        ) from exc
