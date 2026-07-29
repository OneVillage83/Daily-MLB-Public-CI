from __future__ import annotations

from dataclasses import replace

import pytest

from app.data_quality.contracts import DataQualityDisposition
from app.predictions.contracts import ModelCalibrationStatus, ModelDeploymentStatus
from app.recommendation_gate.contracts import (
    EvidenceConfidenceBand,
    OperationalRiskLevel,
    RecommendationDecision,
    RecommendationGameWarning,
    RecommendationReason,
)
from app.recommendation_gate.engine import evaluate_recommendation_gate
from app.recommendation_gate.policy import (
    RecommendationPolicyError,
    RecommendationPolicyV1,
)
from app.value_engine.contracts import (
    ValueCalculationState,
    ValueGameWarning,
    ValueIneligibilityReason,
)
from app.value_engine.engine import evaluate_value_engine
from tests.test_value_engine import _packet_predictions


def _base_value_engine():
    packet, predictions = _packet_predictions()
    return evaluate_value_engine(predictions=predictions, matchup_packet=packet)


def _eligible_value_engine(
    *,
    edge: float = 0.04,
    ev: float = 0.03,
    books: int = 4,
    freshness: str = "fresh",
    quality: DataQualityDisposition = DataQualityDisposition.READY,
    consensus: str = "high",
):
    value_engine = _base_value_engine()
    game = value_engine.games[0]
    value = game.values[0]
    no_vig = max(0.0, min(1.0, value.conditional_model_probability - edge))
    changed_value = replace(
        value,
        bookmaker_count=books,
        consensus_confidence=consensus,
        freshness_status=freshness,
        complete_two_way_market=True,
        no_vig_probability=no_vig,
        no_vig_probability_edge=edge,
        expected_value_per_unit=ev,
        expected_roi_percent=ev * 100.0,
        calculation_state=ValueCalculationState.COMPLETE,
        recommendation_gate_input_eligible=True,
        ineligibility_reasons=(),
    )
    changed_game = replace(
        game,
        deployment_status=ModelDeploymentStatus.PRODUCTION,
        calibration_status=ModelCalibrationStatus.CALIBRATED,
        model_recommendation_eligible=True,
        quality_disposition=quality,
        values=(changed_value,),
    )
    return replace(value_engine, games=(changed_game,))


def test_reference_model_rows_are_pass_not_hidden_or_promoted() -> None:
    value_engine = _base_value_engine()
    ready_game = replace(
        value_engine.games[0],
        quality_disposition=DataQualityDisposition.READY,
    )
    value_engine = replace(value_engine, games=(ready_game,))
    result = evaluate_recommendation_gate(value_engine)
    assert len(result.games) == 1
    assert len(result.games[0].recommendations) == len(value_engine.games[0].values)
    assert result.decision_count(RecommendationDecision.PASS) == len(
        value_engine.games[0].values
    )
    assert result.decision_count(RecommendationDecision.BET) == 0
    assert all(
        RecommendationReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE in item.reasons
        for item in result.games[0].recommendations
    )
    assert all(
        item.manual_promotion_permitted is False
        for item in result.games[0].recommendations
    )


def test_strict_qualifying_row_is_bet_candidate_requiring_review() -> None:
    result = evaluate_recommendation_gate(_eligible_value_engine())
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.BET
    assert item.reasons == (RecommendationReason.QUALIFIED_BET,)
    assert item.evidence_confidence_score == 100
    assert item.evidence_confidence_band is EvidenceConfidenceBand.ELITE
    assert item.operational_risk_level is OperationalRiskLevel.LOW
    assert item.review_required is True
    assert item.publication_candidate is True
    assert len(item.gate_results) == 17


def test_positive_sub_bet_value_becomes_lean() -> None:
    result = evaluate_recommendation_gate(
        _eligible_value_engine(edge=0.02, ev=0.01, books=3)
    )
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.LEAN
    assert RecommendationReason.QUALIFIED_LEAN in item.reasons
    assert RecommendationReason.BET_FLOOR_NOT_MET in item.reasons
    assert item.review_required is True
    assert item.publication_candidate is False


def test_market_efficient_row_is_pass_with_explicit_reason() -> None:
    result = evaluate_recommendation_gate(
        _eligible_value_engine(edge=0.005, ev=0.005)
    )
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.PASS
    assert (
        RecommendationReason.INSUFFICIENT_MODEL_MARKET_DISAGREEMENT
        in item.reasons
    )
    assert item.review_required is False
    assert item.publication_candidate is False


def test_stale_market_is_avoid_even_with_large_edge() -> None:
    value_engine = _eligible_value_engine(edge=0.10, ev=0.20, freshness="stale")
    game = value_engine.games[0]
    value = replace(
        game.values[0],
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=(ValueIneligibilityReason.MARKET_STALE,),
    )
    value_engine = replace(value_engine, games=(replace(game, values=(value,)),))
    result = evaluate_recommendation_gate(value_engine)
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.AVOID
    assert RecommendationReason.MARKET_STALE in item.reasons
    assert item.operational_risk_level is OperationalRiskLevel.UNACCEPTABLE
    assert item.evidence_confidence_score == 0


def test_insufficient_data_quality_is_avoid() -> None:
    value_engine = _eligible_value_engine(quality=DataQualityDisposition.INSUFFICIENT)
    result = evaluate_recommendation_gate(value_engine)
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.AVOID
    assert RecommendationReason.DATA_QUALITY_INSUFFICIENT in item.reasons


def test_one_book_market_is_avoid_as_thin_market_proxy() -> None:
    result = evaluate_recommendation_gate(_eligible_value_engine(books=1))
    item = result.games[0].recommendations[0]
    assert item.decision is RecommendationDecision.AVOID
    assert RecommendationReason.INSUFFICIENT_MARKET_COVERAGE in item.reasons


def test_degraded_high_edge_row_is_capped_below_bet_and_becomes_lean() -> None:
    result = evaluate_recommendation_gate(
        _eligible_value_engine(
            edge=0.08,
            ev=0.10,
            books=4,
            quality=DataQualityDisposition.DEGRADED,
        )
    )
    item = result.games[0].recommendations[0]
    assert item.evidence_confidence_score == 79
    assert item.evidence_confidence_band is EvidenceConfidenceBand.STRONG
    assert item.decision is RecommendationDecision.LEAN


def test_policy_rejects_runtime_threshold_weakening() -> None:
    with pytest.raises(RecommendationPolicyError, match="cannot be weakened"):
        RecommendationPolicyV1(minimum_bet_no_vig_edge=0.02)
    with pytest.raises(RecommendationPolicyError, match="cannot be weakened"):
        RecommendationPolicyV1(minimum_bet_book_count=3)
    with pytest.raises(RecommendationPolicyError, match="cannot be weakened"):
        RecommendationPolicyV1(minimum_bet_ev_per_unit=0.01)


def test_zero_value_game_remains_represented() -> None:
    value_engine = _base_value_engine()
    empty_game = replace(
        value_engine.games[0],
        values=(),
        warnings=(ValueGameWarning.NO_EVALUABLE_MARKETS,),
    )
    value_engine = replace(value_engine, games=(empty_game,))
    result = evaluate_recommendation_gate(value_engine)
    assert len(result.games) == 1
    assert result.games[0].recommendations == ()
    assert result.games[0].warnings == (RecommendationGameWarning.NO_VALUE_ROWS,)
    assert result.recommendation_count == 0


def test_recommendation_gate_is_deterministic() -> None:
    value_engine = _eligible_value_engine()
    first = evaluate_recommendation_gate(value_engine)
    second = evaluate_recommendation_gate(value_engine)
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
