from __future__ import annotations

import pytest

from app.predictions.production import PREDICTIONS_CONTRACT_VERSION
from app.predictions.run_line import build_run_line_prediction
from app.value_engine.production import VALUE_ENGINE_PRODUCTION_CONTRACT
from app.value_engine.run_line import (
    MAX_UNRESOLVED_VALUE_TAIL,
    RUN_LINE_VALUE_CALCULATION_VERSION,
    RUN_LINE_VALUE_GAME_CONTRACT_VERSION,
    RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION,
    evaluate_run_line_value,
)
from tests.test_value_engine import _packet_predictions


def _run_line_value_fixture():
    packet, predictions = _packet_predictions()
    prediction = build_run_line_prediction(predictions.games[0])
    odds = packet.games[0].odds_weather.odds
    return prediction, odds, evaluate_run_line_value(prediction, odds)


def test_v2b_preserves_frozen_v1_moneyline_contracts() -> None:
    assert PREDICTIONS_CONTRACT_VERSION == "DSE_MLB_ML_PREDICTIONS_V1"
    assert VALUE_ENGINE_PRODUCTION_CONTRACT == "DSE_MLB_ML_VALUE_ENGINE_V1"


def test_v2b_evaluates_only_retained_run_line_market() -> None:
    prediction, odds, result = _run_line_value_fixture()

    assert result.contract_version == RUN_LINE_VALUE_GAME_CONTRACT_VERSION
    assert result.source_game_id == prediction.source_game_id
    assert result.home_team_id == prediction.home_team_id == "LAD"
    assert result.away_team_id == prediction.away_team_id == "SF"
    assert result.source_prediction_checksum == prediction.checksum
    assert result.odds_summary_checksum == odds.summary_checksum
    assert result.odds_retrieved_at == odds.retrieved_at
    assert result.warnings == ()
    assert len(result.values) == 2
    assert {value.side for value in result.values} == {"home", "away"}
    assert {value.outcome_team_id for value in result.values} == {"LAD", "SF"}
    assert all(value.contract_version == RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION for value in result.values)
    assert all(value.calculation_version == RUN_LINE_VALUE_CALCULATION_VERSION for value in result.values)
    assert len(result.checksum) == 64


def test_v2b_binds_canonical_home_spread_prices_and_projection_lineage() -> None:
    prediction, _, result = _run_line_value_fixture()
    home = next(value for value in result.values if value.side == "home")
    away = next(value for value in result.values if value.side == "away")
    projection = prediction.project(-1.0)

    assert home.home_spread == pytest.approx(-1.0)
    assert home.side_spread == pytest.approx(-1.0)
    assert away.home_spread == pytest.approx(-1.0)
    assert away.side_spread == pytest.approx(1.0)
    assert home.american_price == pytest.approx(135.0)
    assert home.best_price_books == ("book_b",)
    assert away.american_price == pytest.approx(-150.0)
    assert away.best_price_books == ("book_a",)
    assert home.bookmaker_count == away.bookmaker_count == 2
    assert home.source_projection_checksum == projection.checksum
    assert away.source_projection_checksum == projection.checksum
    assert home.source_prediction_checksum == prediction.checksum
    assert away.source_prediction_checksum == prediction.checksum


def test_v2b_conditions_explicitly_on_resolved_tail_and_keeps_push_aware_ev() -> None:
    prediction, _, result = _run_line_value_fixture()
    home = next(value for value in result.values if value.side == "home")

    assert 0.0 <= home.unresolved_probability <= MAX_UNRESOLVED_VALUE_TAIL
    assert home.resolved_model_push_probability > 0.0
    assert (
        home.resolved_model_cover_probability
        + home.resolved_model_loss_probability
        + home.resolved_model_push_probability
        == pytest.approx(1.0)
    )
    assert home.conditional_model_probability == pytest.approx(
        home.resolved_model_cover_probability
        / (home.resolved_model_cover_probability + home.resolved_model_loss_probability)
    )
    assert home.expected_value_per_unit == pytest.approx(
        home.resolved_model_cover_probability * 1.35 - home.resolved_model_loss_probability
    )
    assert home.expected_roi_percent == pytest.approx(home.expected_value_per_unit * 100.0)
    assert home.no_vig_probability is not None
    assert home.no_vig_probability_edge is not None
    assert prediction.run_margin_distribution.unresolved_probability == pytest.approx(home.unresolved_probability)


def test_v2b_reference_prediction_cannot_reach_recommendation_gate() -> None:
    _, _, result = _run_line_value_fixture()

    assert result.values
    for value in result.values:
        assert value.prediction_recommendation_eligible is False
        assert value.recommendation_gate_input_eligible is False
        assert "prediction_not_recommendation_eligible" in value.ineligibility_reasons
        assert "market_stale" not in value.ineligibility_reasons
        assert "market_freshness_unknown" not in value.ineligibility_reasons
        assert value.freshness_status == "fresh"
