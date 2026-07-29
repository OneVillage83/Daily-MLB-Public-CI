from __future__ import annotations

from datetime import datetime, timezone

from app.data_quality.contracts import DataQualityDisposition
from app.predictions.contracts import ModelCalibrationStatus, ModelDeploymentStatus
from app.recommendation_gate.contracts import (
    OperationalRiskLevel,
    PolicyGateResultV1,
    RecommendationDecision,
    RecommendationGameV1,
    RecommendationGameWarning,
    RecommendationGateContractError,
    RecommendationGateV1,
    RecommendationReason,
    RecommendationV1,
    confidence_band,
)
from app.recommendation_gate.policy import (
    DEFAULT_RECOMMENDATION_POLICY_V1,
    RecommendationPolicyV1,
)
from app.value_engine.contracts import (
    MarketValueV1,
    ValueCalculationState,
    ValueEngineV1,
    ValueGameV1,
)


def evidence_confidence_score(game: ValueGameV1, value: MarketValueV1) -> int:
    """Measure actionability evidence quality, never predicted win probability."""

    if game.quality_disposition is DataQualityDisposition.INSUFFICIENT:
        return 0
    if value.calculation_state is not ValueCalculationState.COMPLETE:
        return 0
    if not value.complete_two_way_market or value.no_vig_probability is None:
        return 0
    if value.freshness_status in {"stale", "unknown"}:
        return 0

    score = 100
    cap = 100
    if game.quality_disposition is DataQualityDisposition.DEGRADED:
        score -= 20
        cap = min(cap, 79)

    if value.bookmaker_count == 3:
        score -= 10
    elif value.bookmaker_count == 2:
        score -= 20
    elif value.bookmaker_count == 1:
        score -= 40

    consensus = value.consensus_confidence.lower()
    if consensus == "medium":
        score -= 5
    elif consensus == "low":
        score -= 15
    elif consensus not in {"high", "strong"}:
        score -= 25

    if value.freshness_status == "aging":
        score -= 10

    if (
        not game.model_recommendation_eligible
        or game.deployment_status is not ModelDeploymentStatus.PRODUCTION
        or game.calibration_status is not ModelCalibrationStatus.CALIBRATED
    ):
        cap = min(cap, 59)

    return max(0, min(100, score, cap))


def _gate(
    *,
    code: str,
    passed: bool,
    threshold: str,
    observed: str,
    source_checksum: str,
    evaluated_at: datetime,
) -> PolicyGateResultV1:
    return PolicyGateResultV1(
        gate_code=code,
        passed=passed,
        threshold=threshold,
        observed_value=observed,
        reason_code=f"{code}_{'passed' if passed else 'failed'}",
        source_checksum=source_checksum,
        evaluated_at=evaluated_at,
    )


def _gate_results(
    game: ValueGameV1,
    value: MarketValueV1,
    *,
    confidence_score: int,
    policy: RecommendationPolicyV1,
    evaluated_at: datetime,
) -> tuple[PolicyGateResultV1, ...]:
    edge = value.no_vig_probability_edge
    no_vig_available = value.no_vig_probability is not None and edge is not None
    value_source = value.checksum
    game_source = game.checksum
    return (
        _gate(
            code="value_lineage_valid",
            passed=True,
            threshold="canonical ValueEngine row checksum present",
            observed=value.checksum,
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="model_recommendation_eligible",
            passed=game.model_recommendation_eligible,
            threshold="true",
            observed=str(game.model_recommendation_eligible).lower(),
            source_checksum=game_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="model_production",
            passed=game.deployment_status is ModelDeploymentStatus.PRODUCTION,
            threshold=ModelDeploymentStatus.PRODUCTION.value,
            observed=game.deployment_status.value,
            source_checksum=game_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="model_calibrated",
            passed=game.calibration_status is ModelCalibrationStatus.CALIBRATED,
            threshold=ModelCalibrationStatus.CALIBRATED.value,
            observed=game.calibration_status.value,
            source_checksum=game_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="data_quality_clear",
            passed=game.quality_disposition is not DataQualityDisposition.INSUFFICIENT,
            threshold="not insufficient",
            observed=game.quality_disposition.value,
            source_checksum=game_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="calculation_complete",
            passed=value.calculation_state is ValueCalculationState.COMPLETE,
            threshold=ValueCalculationState.COMPLETE.value,
            observed=value.calculation_state.value,
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="complete_two_way_market",
            passed=value.complete_two_way_market,
            threshold="true",
            observed=str(value.complete_two_way_market).lower(),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="no_vig_available",
            passed=no_vig_available,
            threshold="probability and edge present",
            observed=str(no_vig_available).lower(),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="odds_fresh",
            passed=value.freshness_status == "fresh",
            threshold="fresh",
            observed=value.freshness_status,
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_lean_book_count",
            passed=value.bookmaker_count >= policy.minimum_lean_book_count,
            threshold=f">={policy.minimum_lean_book_count}",
            observed=str(value.bookmaker_count),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_bet_book_count",
            passed=value.bookmaker_count >= policy.minimum_bet_book_count,
            threshold=f">={policy.minimum_bet_book_count}",
            observed=str(value.bookmaker_count),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_lean_edge",
            passed=edge is not None and edge >= policy.minimum_lean_no_vig_edge,
            threshold=f">={policy.minimum_lean_no_vig_edge:.6f}",
            observed="missing" if edge is None else f"{edge:.12f}",
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_bet_edge",
            passed=edge is not None and edge >= policy.minimum_bet_no_vig_edge,
            threshold=f">={policy.minimum_bet_no_vig_edge:.6f}",
            observed="missing" if edge is None else f"{edge:.12f}",
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="positive_ev",
            passed=value.expected_value_per_unit > policy.minimum_lean_ev_per_unit,
            threshold=f">{policy.minimum_lean_ev_per_unit:.6f}",
            observed=f"{value.expected_value_per_unit:.12f}",
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_bet_ev",
            passed=value.expected_value_per_unit >= policy.minimum_bet_ev_per_unit,
            threshold=f">={policy.minimum_bet_ev_per_unit:.6f}",
            observed=f"{value.expected_value_per_unit:.12f}",
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_lean_confidence",
            passed=confidence_score >= policy.minimum_lean_confidence_score,
            threshold=f">={policy.minimum_lean_confidence_score}",
            observed=str(confidence_score),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
        _gate(
            code="minimum_bet_confidence",
            passed=confidence_score >= policy.minimum_bet_confidence_score,
            threshold=f">={policy.minimum_bet_confidence_score}",
            observed=str(confidence_score),
            source_checksum=value_source,
            evaluated_at=evaluated_at,
        ),
    )


def _avoid_reasons(game: ValueGameV1, value: MarketValueV1) -> list[RecommendationReason]:
    reasons: list[RecommendationReason] = []
    if game.quality_disposition is DataQualityDisposition.INSUFFICIENT:
        reasons.append(RecommendationReason.DATA_QUALITY_INSUFFICIENT)
    if value.calculation_state is not ValueCalculationState.COMPLETE:
        reasons.append(RecommendationReason.CALCULATION_INCOMPLETE)
    if not value.complete_two_way_market:
        reasons.append(RecommendationReason.INCOMPLETE_TWO_WAY_MARKET)
    if value.no_vig_probability is None or value.no_vig_probability_edge is None:
        reasons.append(RecommendationReason.MISSING_NO_VIG_PROBABILITY)
    if value.freshness_status == "stale":
        reasons.append(RecommendationReason.MARKET_STALE)
    if value.freshness_status == "unknown":
        reasons.append(RecommendationReason.MARKET_FRESHNESS_UNKNOWN)
    if value.bookmaker_count < 2:
        reasons.append(RecommendationReason.INSUFFICIENT_MARKET_COVERAGE)
    return reasons


def _pass_reasons(
    game: ValueGameV1,
    value: MarketValueV1,
    *,
    confidence_score: int,
    policy: RecommendationPolicyV1,
) -> list[RecommendationReason]:
    reasons: list[RecommendationReason] = []
    if not game.model_recommendation_eligible:
        reasons.append(RecommendationReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE)
    if game.deployment_status is not ModelDeploymentStatus.PRODUCTION:
        reasons.append(RecommendationReason.MODEL_NOT_PRODUCTION)
    if game.calibration_status is not ModelCalibrationStatus.CALIBRATED:
        reasons.append(RecommendationReason.MODEL_NOT_CALIBRATED)
    if value.freshness_status == "aging":
        reasons.append(RecommendationReason.MARKET_AGING)
    edge = value.no_vig_probability_edge
    if edge is None or edge < policy.minimum_lean_no_vig_edge:
        reasons.append(
            RecommendationReason.INSUFFICIENT_MODEL_MARKET_DISAGREEMENT
        )
    if value.expected_value_per_unit <= policy.minimum_lean_ev_per_unit:
        reasons.append(RecommendationReason.INSUFFICIENT_EXPECTED_VALUE)
    if confidence_score < policy.minimum_lean_confidence_score:
        reasons.append(RecommendationReason.INSUFFICIENT_EVIDENCE_CONFIDENCE)
    return reasons or [RecommendationReason.BET_FLOOR_NOT_MET]


def evaluate_recommendation(
    game: ValueGameV1,
    value: MarketValueV1,
    *,
    policy: RecommendationPolicyV1 = DEFAULT_RECOMMENDATION_POLICY_V1,
    evaluated_at: datetime,
) -> RecommendationV1:
    score = evidence_confidence_score(game, value)
    gates = _gate_results(
        game,
        value,
        confidence_score=score,
        policy=policy,
        evaluated_at=evaluated_at,
    )
    passed = {gate.gate_code: gate.passed for gate in gates}
    avoid_reasons = _avoid_reasons(game, value)
    bet_codes = (
        "model_recommendation_eligible",
        "model_production",
        "model_calibrated",
        "data_quality_clear",
        "calculation_complete",
        "complete_two_way_market",
        "no_vig_available",
        "odds_fresh",
        "minimum_bet_book_count",
        "minimum_bet_edge",
        "minimum_bet_ev",
        "minimum_bet_confidence",
    )
    lean_codes = (
        "model_recommendation_eligible",
        "model_production",
        "model_calibrated",
        "data_quality_clear",
        "calculation_complete",
        "complete_two_way_market",
        "no_vig_available",
        "odds_fresh",
        "minimum_lean_book_count",
        "minimum_lean_edge",
        "positive_ev",
        "minimum_lean_confidence",
    )

    if avoid_reasons:
        decision = RecommendationDecision.AVOID
        reasons = avoid_reasons
        risk = OperationalRiskLevel.UNACCEPTABLE
    elif all(passed[code] for code in bet_codes):
        decision = RecommendationDecision.BET
        reasons = [RecommendationReason.QUALIFIED_BET]
        risk = OperationalRiskLevel.LOW
    elif all(passed[code] for code in lean_codes):
        decision = RecommendationDecision.LEAN
        reasons = [
            RecommendationReason.QUALIFIED_LEAN,
            RecommendationReason.BET_FLOOR_NOT_MET,
        ]
        risk = (
            OperationalRiskLevel.LOW
            if score >= 80
            else OperationalRiskLevel.MODERATE
        )
    else:
        decision = RecommendationDecision.PASS
        reasons = _pass_reasons(
            game,
            value,
            confidence_score=score,
            policy=policy,
        )
        risk = (
            OperationalRiskLevel.MODERATE
            if score >= 60
            else OperationalRiskLevel.HIGH
        )

    return RecommendationV1(
        upstream_market_value_checksum=value.checksum,
        market=value.market,
        line_key=value.line_key,
        side=value.side,
        market_line=value.market_line,
        american_price=value.american_price,
        bookmaker_count=value.bookmaker_count,
        freshness_status=value.freshness_status,
        conditional_model_probability=value.conditional_model_probability,
        no_vig_probability=value.no_vig_probability,
        no_vig_probability_edge=value.no_vig_probability_edge,
        expected_value_per_unit=value.expected_value_per_unit,
        expected_roi_percent=value.expected_roi_percent,
        decision=decision,
        evidence_confidence_score=score,
        evidence_confidence_band=confidence_band(score),
        operational_risk_score=100 - score,
        operational_risk_level=risk,
        reasons=tuple(reasons),
        gate_results=gates,
        review_required=decision in {
            RecommendationDecision.BET,
            RecommendationDecision.LEAN,
        },
        publication_candidate=decision is RecommendationDecision.BET,
        manual_promotion_permitted=False,
        policy_checksum=policy.checksum,
        evaluated_at=evaluated_at,
    )


def evaluate_recommendation_game(
    game: ValueGameV1,
    *,
    policy: RecommendationPolicyV1 = DEFAULT_RECOMMENDATION_POLICY_V1,
    evaluated_at: datetime,
) -> RecommendationGameV1:
    recommendations = tuple(
        evaluate_recommendation(
            game,
            value,
            policy=policy,
            evaluated_at=evaluated_at,
        )
        for value in game.values
    )
    if len(recommendations) != len(game.values):
        raise RecommendationGateContractError(
            "every Value Engine row must receive one recommendation"
        )
    return RecommendationGameV1(
        edge_event_id=game.edge_event_id,
        daily_mlb_game_id=game.daily_mlb_game_id,
        source_game_id=game.source_game_id,
        away_team_id=game.away_team_id,
        home_team_id=game.home_team_id,
        upstream_value_game_checksum=game.checksum,
        model_manifest_checksum=game.model_manifest_checksum,
        deployment_status=game.deployment_status,
        calibration_status=game.calibration_status,
        model_recommendation_eligible=game.model_recommendation_eligible,
        quality_disposition=game.quality_disposition,
        recommendations=recommendations,
        warnings=(
            () if recommendations else (RecommendationGameWarning.NO_VALUE_ROWS,)
        ),
    )


def evaluate_recommendation_gate(
    value_engine: ValueEngineV1,
    *,
    policy: RecommendationPolicyV1 = DEFAULT_RECOMMENDATION_POLICY_V1,
    evaluated_at: datetime | None = None,
) -> RecommendationGateV1:
    selected = value_engine.observed_at if evaluated_at is None else evaluated_at
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise RecommendationGateContractError("evaluated_at must be timezone-aware")
    selected = selected.astimezone(timezone.utc)
    if selected < value_engine.observed_at:
        raise RecommendationGateContractError(
            "Recommendation Gate cannot precede Value Engine evidence"
        )
    games = tuple(
        evaluate_recommendation_game(
            game,
            policy=policy,
            evaluated_at=selected,
        )
        for game in value_engine.games
    )
    if len(games) != len(value_engine.games):
        raise RecommendationGateContractError(
            "every Value Engine game must remain represented"
        )
    return RecommendationGateV1(
        requested_date=value_engine.requested_date,
        as_of_time=value_engine.as_of_time,
        evaluated_at=selected,
        upstream_value_engine_checksum=value_engine.checksum,
        policy=policy,
        games=games,
    )
