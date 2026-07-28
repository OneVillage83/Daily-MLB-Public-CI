from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.baseball_intelligence.assembly import assemble_baseball_intelligence
from app.data_quality.engine import assess_data_quality
from app.matchup_packet.assembly import assemble_matchup_packet
from app.model_feature_set.builder import build_model_feature_set
from app.odds_weather.assembly import assemble_odds_weather
from app.predictions.contracts import PredictionInputState
from app.predictions.runtime import predict_model_feature_set
from tests.test_baseball_intelligence_assembly import (
    _features_for_state,
    _one_game_state,
)

OW_OBSERVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)


def test_real_frozen_v3_lineup_evidence_changes_reference_prediction() -> None:
    slate, state = _one_game_state()
    intelligence = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=_features_for_state(state),
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
    prediction = predict_model_feature_set(feature_set).games[0]
    contributions = {
        item.feature_name: item for item in prediction.contributions
    }
    away_ops = contributions[
        "away.lineup.season_to_date.hitting.ops"
    ]
    home_ops = contributions[
        "home.lineup.season_to_date.hitting.ops"
    ]
    assert away_ops.raw_value == pytest.approx(0.9)
    assert home_ops.raw_value == pytest.approx(0.9)
    assert away_ops.was_imputed is False
    assert home_ops.was_imputed is False
    assert prediction.input_state is PredictionInputState.IMPUTED
    assert prediction.expected_away_runs > 4.35
    assert prediction.expected_home_runs > 4.55
