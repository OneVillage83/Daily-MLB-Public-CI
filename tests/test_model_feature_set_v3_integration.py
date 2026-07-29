from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.baseball_intelligence.assembly import assemble_baseball_intelligence
from app.data_quality.engine import assess_data_quality
from app.matchup_packet.assembly import assemble_matchup_packet
from app.model_feature_set.builder import build_model_feature_set
from app.odds_weather.assembly import assemble_odds_weather
from tests.test_baseball_intelligence_assembly import (
    _features_for_state,
    _one_game_state,
)

OW_OBSERVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)


def test_real_frozen_v3_lineup_payload_reaches_fixed_model_schema() -> None:
    slate, state = _one_game_state()
    feature_snapshots = _features_for_state(state)
    intelligence = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=feature_snapshots,
        observed_at=state.observed_at,
    ).assembly
    odds_weather = assemble_odds_weather(
        slate=slate,
        baseball_intelligence=intelligence,
        odds_events=(),
        weather_evidence=(),
        observed_at=OW_OBSERVED,
    ).snapshot
    quality = assess_data_quality(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        observed_at=OW_OBSERVED,
    ).snapshot
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        data_quality=quality,
    )

    feature_set = build_model_feature_set(packet)
    features = feature_set.games[0].feature_map()

    # The BIA fixture has two expected away hitters. Each frozen V3 feature
    # contains a season-to-date batting aggregate with PA=20, AB=18, H=6,
    # HR=1, BB=2, and SO=4. ModelFeatureSet must aggregate the canonical
    # counts and then recompute rates, not average player rates blindly.
    assert features["away.lineup.season_to_date.available_player_count"] == 2.0
    assert features["away.lineup.season_to_date.hitting.pa"] == 40.0
    assert features["away.lineup.season_to_date.hitting.avg"] == pytest.approx(12 / 36)
    assert features["away.lineup.season_to_date.hitting.obp"] == pytest.approx(16 / 40)
    assert features["away.lineup.season_to_date.hitting.slg"] == pytest.approx(18 / 36)
    assert features["away.lineup.season_to_date.hitting.ops"] == pytest.approx(0.9)
    assert features["away.lineup.season_to_date.hitting.k_rate"] == pytest.approx(8 / 40)
    assert features["away.lineup.season_to_date.hitting.bb_rate"] == pytest.approx(4 / 40)

    # The same frozen fixture intentionally contains no pitching aggregates.
    # Starter pitching values therefore stay missing instead of being imputed.
    assert features["away.starter.season_to_date.pitching.era"] is None
    assert "away.starter.season_to_date.pitching.era" in feature_set.games[0].missing_feature_names

    # Odds are unavailable in this fixture. No sportsbook value can leak into
    # the predictive vector, and the non-predictive market lineage is null.
    assert feature_set.games[0].market_reference_checksum is None
