from __future__ import annotations

import pytest

from app.predictions.game_total import build_game_total_prediction
from app.value_engine.game_total import (
    GAME_TOTAL_VALUE_CALCULATION_VERSION,
    GAME_TOTAL_VALUE_GAME_CONTRACT_VERSION,
    GAME_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION,
    MAX_UNRESOLVED_TOTAL_VALUE_TAIL,
    evaluate_game_total_value,
)
from app.value_engine.production import VALUE_ENGINE_PRODUCTION_CONTRACT
from app.value_engine.run_line import RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION
from tests.test_value_engine import _packet_predictions


def _game_total_value_fixture():
    packet, predictions = _packet_predictions()
    prediction = build_game_total_prediction(predictions.games[0])
    odds = packet.games[0].odds_weather.odds
    return prediction, odds, evaluate_game_total_value(prediction, odds)


def test_v3b_preserves_v1_moneyline_and_v2_run_line_value_contracts() -> None:
    assert VALUE_ENGINE_PRODUCTION_CONTRACT == "DSE_MLB_ML_VALUE_ENGINE_V1"
    assert RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION == "DSE_MLB_RUN_LINE_VALUE_OUTCOME_V1"


def test_v3b_evaluates_retained_game_total_market() -> None:
    prediction, odds, result = _game_total_value_fixture()

    assert result.contract_version == GAME_TOTAL_VALUE_GAME_CONTRACT_VERSION
    assert result.source_game_id == prediction.source_game_id
    assert result.home_team_id == prediction.home_team_id == "LAD"
    assert result.away_team_id == prediction.away_team_id == "SF"
    assert result.source_prediction_checksum == prediction.checksum
    assert result.odds_summary_checksum == odds.summary_checksum
    assert result.odds_retrieved_at == odds.retrieved_at
    assert result.warnings == ()
    assert len(result.values) == 2
    assert {value.side for value in result.values} == {"over", "under"}
    assert {value.total_line for value in result.values} == {9.0}
    assert all(value.contract_version == GAME_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION for value in result.values)
    assert all(value.calculation_version == GAME_TOTAL_VALUE_CALCULATION_VERSION for value in result.values)
    assert len(result.checksum) == 64


def test_v3b_binds_exact_total_prices_and_projection_lineage() -> None:
    prediction, _, result = _game_total_value_fixture()
    over = next(value for value in result.values if value.side == "over")
    under = next(value for value in result.values if value.side == "under")
    projection = prediction.project(9.0)

    assert over.total_line == under.total_line == pytest.approx(9.0)
    assert over.american_price == pytest.approx(-105.0)
    assert over.best_price_books == ("book_a",)
    assert under.american_price == pytest.approx(-110.0)
    assert under.best_price_books == ("book_b",)
    assert over.bookmaker_count == under.bookmaker_count == 2
    assert over.source_projection_checksum == projection.checksum
    assert under.source_projection_checksum == projection.checksum
    assert over.source_prediction_checksum == prediction.checksum
    assert under.source_prediction_checksum == prediction.checksum


def test_v3b_integer_total_keeps_push_aware_ev_and_unresolved_tail() -> None:
    prediction, _, result = _game_total_value_fixture()
    over = next(value for value in result.values if value.side == "over")

    assert 0.0 <= over.unresolved_probability <= MAX_UNRESOLVED_TOTAL_VALUE_TAIL
    assert over.resolved_model_push_probability > 0.0
    assert (
        over.resolved_model_win_probability
        + over.resolved_model_loss_probability
        + over.resolved_model_push_probability
        == pytest.approx(1.0)
    )
    assert over.conditional_model_probability == pytest.approx(
        over.resolved_model_win_probability
        / (over.resolved_model_win_probability + over.resolved_model_loss_probability)
    )
    assert over.expected_value_per_unit == pytest.approx(
        over.resolved_model_win_probability * (100 / 105)
        - over.resolved_model_loss_probability
    )
    assert over.expected_roi_percent == pytest.approx(over.expected_value_per_unit * 100.0)
    assert over.no_vig_probability is not None
    assert over.no_vig_probability_edge is not None
    assert prediction.total_runs_distribution.unresolved_probability == pytest.approx(over.unresolved_probability)


def test_v3b_reference_prediction_cannot_reach_recommendation_gate() -> None:
    _, _, result = _game_total_value_fixture()

    assert result.values
    for value in result.values:
        assert value.prediction_recommendation_eligible is False
        assert value.recommendation_gate_input_eligible is False
        assert "prediction_not_recommendation_eligible" in value.ineligibility_reasons
        assert "market_stale" not in value.ineligibility_reasons
        assert "market_freshness_unknown" not in value.ineligibility_reasons
        assert value.freshness_status == "fresh"
