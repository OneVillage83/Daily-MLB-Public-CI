from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Any, Mapping

from app.analysis.canonical import CanonicalGame
from app.analysis.models import (
    CandidateDecision,
    DataQualityState,
    GateResult,
    UncertaintyGrade,
    checksum_payload,
    finite_number,
    json_value,
    optional_utc_datetime,
    utc_datetime,
)
from app.analysis.predictions import (
    EvidenceState,
    LineupInformationState,
    REVIEWED_ANALYST_VERSION,
    SealedPrediction,
    verify_prediction_checksum,
)

CANDIDATE_POLICY_VERSION = "DSE_MLB_ML_CANDIDATE_V1"
REQUIRED_CANDIDATE_GATE_CODES = (
    "prediction_valid",
    "market_independent",
    "identity_valid",
    "market_supported",
    "minimum_book_count",
    "odds_fresh",
    "best_price_eligible",
    "data_quality_clear",
    "minimum_edge",
    "minimum_ev",
    "uncertainty_clears_market",
    "minimum_confidence",
    "weather_gate_clear",
    "phase2_live_weather_accepted",
    "event_pregame",
)


@dataclass(frozen=True, slots=True)
class CandidatePolicyThresholds:
    minimum_fresh_books: int = 4
    maximum_odds_age_seconds: int = 120
    maximum_future_skew_seconds: int = 30
    maximum_pair_timestamp_skew_seconds: int = 5
    minimum_edge_percentage_points: float = 3.0
    minimum_expected_value_per_unit_risk: float = 0.02
    minimum_lower_bound_clearance: float = 0.01

    def __post_init__(self) -> None:
        if self.minimum_fresh_books < 1:
            raise ValueError("minimum_fresh_books must be positive")
        if min(
            self.maximum_odds_age_seconds,
            self.maximum_future_skew_seconds,
            self.maximum_pair_timestamp_skew_seconds,
        ) < 0:
            raise ValueError("timestamp thresholds must be nonnegative")


@dataclass(frozen=True, slots=True)
class MoneylineSideEvaluation:
    team_key: str
    prediction_probability: float
    probability_lower: float
    probability_upper: float
    market_no_vig_probability: float | None
    best_price: float | None
    best_price_books: tuple[str, ...]
    break_even_probability: float | None
    edge_percentage_points: float | None
    expected_value_per_unit_risk: float | None


@dataclass(frozen=True, slots=True)
class PolicyEvaluation:
    policy_version: str
    evaluation_id: str
    event_id: str
    prediction_id: str
    prediction_checksum: str
    canonical_game_checksum: str
    evaluated_at: datetime
    selected_team_key: str
    side_evaluations: tuple[MoneylineSideEvaluation, ...]
    prediction_probability: float
    probability_lower: float
    probability_upper: float
    market_no_vig_probability: float | None
    best_price: float | None
    best_price_books: tuple[str, ...]
    break_even_probability: float | None
    edge_percentage_points: float | None
    expected_value_per_unit_risk: float | None
    eligible_bookmaker_count: int
    eligible_bookmakers: tuple[str, ...]
    confidence_grade: UncertaintyGrade
    gate_results: tuple[GateResult, ...]
    decision: CandidateDecision
    checksum: str

    def as_dict(self) -> dict[str, Any]:
        return dict(json_value(self))


@dataclass(frozen=True, slots=True)
class _BookPair:
    bookmaker: str
    home_price: float
    away_price: float
    home_no_vig: float
    away_no_vig: float


def american_to_probability(price: object) -> float | None:
    number = finite_number(price)
    if number is None or abs(number) < 100.0:
        return None
    if number > 0:
        return 100.0 / (number + 100.0)
    return abs(number) / (abs(number) + 100.0)


def expected_value_per_unit_risk(probability: float, price: float) -> float:
    profit_multiplier = price / 100.0 if price > 0 else 100.0 / abs(price)
    return probability * profit_multiplier - (1.0 - probability)


def _h2h_outcomes(game: CanonicalGame) -> Mapping[str, Any] | None:
    markets = game.odds_consensus.get("markets")
    if not isinstance(markets, Mapping):
        return None
    h2h = markets.get("h2h")
    if not isinstance(h2h, Mapping):
        return None
    lines = h2h.get("lines")
    if not isinstance(lines, Mapping):
        return None
    line = lines.get("moneyline")
    if not isinstance(line, Mapping):
        return None
    outcomes = line.get("outcomes")
    return outcomes if isinstance(outcomes, Mapping) else None


def _offers(outcomes: Mapping[str, Any] | None, team_key: str) -> list[Mapping[str, Any]]:
    if outcomes is None:
        return []
    outcome = outcomes.get(team_key)
    if not isinstance(outcome, Mapping) or not isinstance(outcome.get("offers"), list):
        return []
    return [offer for offer in outcome["offers"] if isinstance(offer, Mapping)]


def _eligible_offer(
    offer: Mapping[str, Any],
    *,
    evaluated_at: datetime,
    thresholds: CandidatePolicyThresholds,
) -> tuple[str, float, datetime, datetime] | None:
    bookmaker = str(offer.get("bookmaker_key") or "").strip()
    price = finite_number(offer.get("price"))
    implied = american_to_probability(price)
    effective = optional_utc_datetime(offer.get("effective_provider_timestamp"))
    retrieved = optional_utc_datetime(
        offer.get("provider_retrieved_at") or offer.get("retrieved_at")
    )
    if (
        not bookmaker
        or price is None
        or implied is None
        or effective is None
        or retrieved is None
        or not bool(offer.get("calculation_eligible", True))
    ):
        return None
    age = (evaluated_at - effective).total_seconds()
    if age < -thresholds.maximum_future_skew_seconds or age > thresholds.maximum_odds_age_seconds:
        return None
    if retrieved > evaluated_at:
        return None
    return bookmaker, price, effective, retrieved


def _book_pairs(
    game: CanonicalGame,
    *,
    evaluated_at: datetime,
    thresholds: CandidatePolicyThresholds,
) -> tuple[_BookPair, ...]:
    outcomes = _h2h_outcomes(game)
    home_by_book: dict[str, list[tuple[Mapping[str, Any], float, datetime, datetime]]] = defaultdict(list)
    away_by_book: dict[str, list[tuple[Mapping[str, Any], float, datetime, datetime]]] = defaultdict(list)
    for target, team_key in ((home_by_book, game.home_team_key), (away_by_book, game.away_team_key)):
        for offer in _offers(outcomes, team_key):
            eligible = _eligible_offer(offer, evaluated_at=evaluated_at, thresholds=thresholds)
            if eligible is None:
                continue
            bookmaker, price, effective, retrieved = eligible
            target[bookmaker].append((offer, price, effective, retrieved))

    pairs: list[_BookPair] = []
    for bookmaker in sorted(set(home_by_book) & set(away_by_book)):
        candidates: list[tuple[datetime, datetime, int, float, float]] = []
        for home_offer, home_price, home_effective, home_retrieved in home_by_book[bookmaker]:
            for away_offer, away_price, away_effective, away_retrieved in away_by_book[bookmaker]:
                if abs((home_retrieved - away_retrieved).total_seconds()) > thresholds.maximum_pair_timestamp_skew_seconds:
                    continue
                if abs((home_effective - away_effective).total_seconds()) > thresholds.maximum_pair_timestamp_skew_seconds:
                    continue
                provider_order = min(
                    int(finite_number(home_offer.get("provider_order")) or 0),
                    int(finite_number(away_offer.get("provider_order")) or 0),
                )
                candidates.append(
                    (min(home_retrieved, away_retrieved), min(home_effective, away_effective), provider_order, home_price, away_price)
                )
        if not candidates:
            continue
        _, _, _, home_price, away_price = max(candidates, key=lambda item: item[:3])
        home_raw = american_to_probability(home_price)
        away_raw = american_to_probability(away_price)
        if home_raw is None or away_raw is None or home_raw + away_raw <= 0:
            continue
        total = home_raw + away_raw
        pairs.append(
            _BookPair(
                bookmaker=bookmaker,
                home_price=home_price,
                away_price=away_price,
                home_no_vig=home_raw / total,
                away_no_vig=away_raw / total,
            )
        )
    return tuple(pairs)


def _side_evaluation(
    *,
    team_key: str,
    probability: float,
    lower: float,
    upper: float,
    pairs: tuple[_BookPair, ...],
    home: bool,
) -> MoneylineSideEvaluation:
    no_vig_values = [pair.home_no_vig if home else pair.away_no_vig for pair in pairs]
    prices = [pair.home_price if home else pair.away_price for pair in pairs]
    baseline = float(median(no_vig_values)) if no_vig_values else None
    best = max(prices) if prices else None
    best_books = tuple(
        sorted(
            pair.bookmaker
            for pair in pairs
            if (pair.home_price if home else pair.away_price) == best
        )
    )
    break_even = american_to_probability(best)
    edge = 100.0 * (probability - baseline) if baseline is not None else None
    ev = expected_value_per_unit_risk(probability, best) if best is not None else None
    return MoneylineSideEvaluation(
        team_key=team_key,
        prediction_probability=probability,
        probability_lower=lower,
        probability_upper=upper,
        market_no_vig_probability=baseline,
        best_price=best,
        best_price_books=best_books,
        break_even_probability=break_even,
        edge_percentage_points=edge,
        expected_value_per_unit_risk=ev,
    )


_GRADE_RANK = {
    UncertaintyGrade.A: 0,
    UncertaintyGrade.B: 1,
    UncertaintyGrade.C: 2,
    UncertaintyGrade.D: 3,
}


def _worst_grade(*grades: UncertaintyGrade) -> UncertaintyGrade:
    return max(grades, key=_GRADE_RANK.__getitem__)


def _confidence(game: CanonicalGame, prediction: SealedPrediction) -> UncertaintyGrade:
    width = prediction.home_probability_upper - prediction.home_probability_lower
    interval_grade = (
        UncertaintyGrade.B
        if width <= 0.12 + 1e-12
        else UncertaintyGrade.C
        if width <= 0.18 + 1e-12
        else UncertaintyGrade.D
    )
    quality_grade = (
        UncertaintyGrade.B
        if game.quality_state is DataQualityState.READY
        else UncertaintyGrade.C
        if game.quality_state is DataQualityState.DEGRADED
        else UncertaintyGrade.D
    )
    assessments = (
        prediction.evidence.starting_pitching,
        prediction.evidence.bullpen,
        prediction.evidence.offensive_matchup,
        prediction.evidence.venue_context,
        prediction.evidence.weather_context,
        prediction.evidence.schedule_rest_context,
    )
    incomplete = (
        any(item.state is EvidenceState.UNKNOWN for item in assessments)
        or prediction.evidence.lineup_information_state
        in {LineupInformationState.UNKNOWN, LineupInformationState.UNAVAILABLE}
        or bool(prediction.evidence.material_unknowns)
    )
    evidence_grade = UncertaintyGrade.C if incomplete else UncertaintyGrade.B
    # A is intentionally impossible for the uncalibrated launch provider.
    return _worst_grade(interval_grade, quality_grade, evidence_grade, UncertaintyGrade.B)


def _gate(
    code: str,
    passed: bool,
    *,
    threshold: bool | int | float | str | None,
    observed: bool | int | float | str | None,
    passed_reason: str,
    failed_reason: str,
) -> GateResult:
    return GateResult(code, passed, threshold, observed, passed_reason if passed else failed_reason)


def evaluate_moneyline_candidate(
    game: CanonicalGame,
    prediction: SealedPrediction,
    *,
    evaluated_at: datetime | str,
    thresholds: CandidatePolicyThresholds | None = None,
    phase2_live_weather_accepted: bool = False,
) -> PolicyEvaluation:
    policy = thresholds or CandidatePolicyThresholds()
    evaluated = utc_datetime(evaluated_at, field="evaluated_at")
    valid_checksum = verify_prediction_checksum(prediction)
    # Invalid or tampered authoring evidence must not unlock market-derived values.
    pairs = (
        _book_pairs(game, evaluated_at=evaluated, thresholds=policy)
        if valid_checksum
        else ()
    )
    side_evaluations = (
        _side_evaluation(
            team_key=game.home_team_key,
            probability=prediction.home_probability,
            lower=prediction.home_probability_lower,
            upper=prediction.home_probability_upper,
            pairs=pairs,
            home=True,
        ),
        _side_evaluation(
            team_key=game.away_team_key,
            probability=1.0 - prediction.home_probability,
            lower=1.0 - prediction.home_probability_upper,
            upper=1.0 - prediction.home_probability_lower,
            pairs=pairs,
            home=False,
        ),
    )
    selected = max(
        side_evaluations,
        key=lambda side: (
            side.expected_value_per_unit_risk if side.expected_value_per_unit_risk is not None else -math.inf,
            side.edge_percentage_points if side.edge_percentage_points is not None else -math.inf,
            side.team_key,
        ),
    )
    confidence = _confidence(game, prediction)
    identity_valid = (
        prediction.run_id == game.run_id
        and prediction.event_id == game.event_id
        and prediction.home_team_key == game.home_team_key
        and prediction.away_team_key == game.away_team_key
    )
    market_supported = bool(pairs) and selected.market_no_vig_probability is not None
    selected_fresh = selected.best_price is not None and bool(selected.best_price_books)
    edge_pass = (
        selected.edge_percentage_points is not None
        and selected.edge_percentage_points + 1e-12 >= policy.minimum_edge_percentage_points
    )
    ev_pass = (
        selected.expected_value_per_unit_risk is not None
        and selected.expected_value_per_unit_risk + 1e-12 >= policy.minimum_expected_value_per_unit_risk
    )
    lower_clearance = (
        selected.probability_lower - selected.market_no_vig_probability
        if selected.market_no_vig_probability is not None
        else None
    )
    uncertainty_pass = lower_clearance is not None and lower_clearance + 1e-12 >= policy.minimum_lower_bound_clearance
    confidence_pass = confidence in {UncertaintyGrade.A, UncertaintyGrade.B}
    pregame = evaluated < game.commence_time
    gates = (
        _gate("prediction_valid", valid_checksum and prediction.contract_version == REVIEWED_ANALYST_VERSION, threshold=True, observed=valid_checksum, passed_reason="Sealed prediction checksum and contract are valid", failed_reason="Sealed prediction checksum or contract is invalid"),
        _gate("market_independent", prediction.market_independence_attested, threshold=True, observed=prediction.market_independence_attested, passed_reason="Analyst attested that market consensus was not the prediction source", failed_reason="Market-independence attestation is absent"),
        _gate("identity_valid", identity_valid, threshold=True, observed=identity_valid, passed_reason="Prediction and canonical game identities match", failed_reason="Prediction and canonical game identities do not match"),
        _gate("market_supported", market_supported, threshold="two-way h2h", observed="two-way h2h" if market_supported else None, passed_reason="Eligible same-book two-way h2h pairs are available", failed_reason="No eligible same-book two-way h2h pair is available"),
        _gate("minimum_book_count", len(pairs) >= policy.minimum_fresh_books, threshold=policy.minimum_fresh_books, observed=len(pairs), passed_reason="Fresh eligible bookmaker minimum is satisfied", failed_reason="Fewer than the required fresh eligible bookmakers contributed"),
        _gate("odds_fresh", selected_fresh, threshold=policy.maximum_odds_age_seconds, observed=selected_fresh, passed_reason="Baseline and selected publication price use fresh eligible offers", failed_reason="No fresh eligible publication price is available"),
        _gate("best_price_eligible", selected_fresh, threshold=True, observed=selected_fresh, passed_reason="Best price comes from an eligible fresh bookmaker", failed_reason="Best price does not come from an eligible fresh bookmaker"),
        _gate("data_quality_clear", game.quality_state is not DataQualityState.BLOCKED, threshold="not blocked", observed=game.quality_state.value, passed_reason="Canonical game has no blocking quality issue", failed_reason="Canonical game has a blocking quality issue"),
        _gate("minimum_edge", edge_pass, threshold=policy.minimum_edge_percentage_points, observed=selected.edge_percentage_points, passed_reason="Estimated edge meets the policy minimum", failed_reason="Estimated edge is unavailable or below the policy minimum"),
        _gate("minimum_ev", ev_pass, threshold=policy.minimum_expected_value_per_unit_risk, observed=selected.expected_value_per_unit_risk, passed_reason="Expected value meets the policy minimum", failed_reason="Expected value is unavailable or below the policy minimum"),
        _gate("uncertainty_clears_market", uncertainty_pass, threshold=policy.minimum_lower_bound_clearance, observed=lower_clearance, passed_reason="Prediction lower bound clears the market baseline", failed_reason="Prediction lower bound does not clear the market baseline"),
        _gate("minimum_confidence", confidence_pass, threshold=UncertaintyGrade.B.value, observed=confidence.value, passed_reason="Confidence meets the launch policy minimum", failed_reason="Material uncertainty exceeds the launch policy limit"),
        _gate("weather_gate_clear", game.weather_gate_clear, threshold=True, observed=game.weather_gate_clear, passed_reason="Weather context gate is clear", failed_reason="Required weather context is missing or temporally invalid"),
        _gate("phase2_live_weather_accepted", phase2_live_weather_accepted, threshold=True, observed=phase2_live_weather_accepted, passed_reason="Phase 2 live weather validation release gate is accepted", failed_reason="Phase 2 live weather validation release gate remains pending"),
        _gate("event_pregame", pregame, threshold="before scheduled first pitch", observed=pregame, passed_reason="Evaluation occurred before scheduled first pitch", failed_reason="Evaluation occurred at or after scheduled first pitch"),
    )
    if tuple(gate.code for gate in gates) != REQUIRED_CANDIDATE_GATE_CODES:
        raise RuntimeError("candidate policy gate implementation does not match its versioned contract")
    decision = CandidateDecision.CANDIDATE_REQUIRES_REVIEW if all(gate.passed for gate in gates) else CandidateDecision.PASS
    unsigned = {
        "policy_version": CANDIDATE_POLICY_VERSION,
        "event_id": game.event_id,
        "prediction_id": prediction.prediction_id,
        "prediction_checksum": prediction.checksum,
        "canonical_game_checksum": game.checksum,
        "evaluated_at": evaluated,
        "selected_team_key": selected.team_key,
        "side_evaluations": side_evaluations,
        "prediction_probability": selected.prediction_probability,
        "probability_lower": selected.probability_lower,
        "probability_upper": selected.probability_upper,
        "market_no_vig_probability": selected.market_no_vig_probability,
        "best_price": selected.best_price,
        "best_price_books": selected.best_price_books,
        "break_even_probability": selected.break_even_probability,
        "edge_percentage_points": selected.edge_percentage_points,
        "expected_value_per_unit_risk": selected.expected_value_per_unit_risk,
        "eligible_bookmaker_count": len(pairs),
        "eligible_bookmakers": tuple(pair.bookmaker for pair in pairs),
        "confidence_grade": confidence,
        "gate_results": gates,
        "decision": decision,
    }
    checksum = checksum_payload(unsigned)
    return PolicyEvaluation(
        policy_version=CANDIDATE_POLICY_VERSION,
        evaluation_id=f"eval_{checksum[:24]}",
        event_id=game.event_id,
        prediction_id=prediction.prediction_id,
        prediction_checksum=prediction.checksum,
        canonical_game_checksum=game.checksum,
        evaluated_at=evaluated,
        selected_team_key=selected.team_key,
        side_evaluations=side_evaluations,
        prediction_probability=selected.prediction_probability,
        probability_lower=selected.probability_lower,
        probability_upper=selected.probability_upper,
        market_no_vig_probability=selected.market_no_vig_probability,
        best_price=selected.best_price,
        best_price_books=selected.best_price_books,
        break_even_probability=selected.break_even_probability,
        edge_percentage_points=selected.edge_percentage_points,
        expected_value_per_unit_risk=selected.expected_value_per_unit_risk,
        eligible_bookmaker_count=len(pairs),
        eligible_bookmakers=tuple(pair.bookmaker for pair in pairs),
        confidence_grade=confidence,
        gate_results=gates,
        decision=decision,
        checksum=checksum,
    )
