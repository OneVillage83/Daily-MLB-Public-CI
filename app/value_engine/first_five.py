from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.first_five import (
    FirstFiveNormalizedOddsV1,
)
from app.predictions.first_five import (
    FirstFiveMoneylinePredictionV1,
    FirstFiveRunLinePredictionV1,
    FirstFiveTotalPredictionV1,
)
from app.predictions.market_foundation import PredictionMarketFamily
from app.value_engine.pricing import calculate_value_math

FIRST_FIVE_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_VALUE_OUTCOME_V1"
FIRST_FIVE_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_VALUE_GAME_V1"
FIRST_FIVE_VALUE_CALCULATION_VERSION = "DSE_MLB_FIRST_FIVE_VALUE_V1"
FIRST_FIVE_BINDING_PENDING_REASON = "first_five_production_event_binding_pending"
FIRST_FIVE_REFERENCE_REASON = "prediction_not_recommendation_eligible"

_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}
_SUPPORTED_FAMILIES = {
    PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value,
    PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value,
    PredictionMarketFamily.FIRST_FIVE_TOTAL.value,
}


class FirstFiveValueError(ValueError):
    """Raised when V4B First Five Value evidence violates its reference contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstFiveValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstFiveValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstFiveValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FirstFiveValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise FirstFiveValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _probability(value, name)


def _resolved_probabilities(
    *,
    win: float,
    loss: float,
    push: float,
    unresolved: float,
) -> tuple[float, float, float]:
    unresolved_value = _probability(unresolved, "unresolved_probability")
    retained = 1.0 - unresolved_value
    if retained <= 0.0:
        raise FirstFiveValueError("First Five prediction contains no resolved probability mass")
    resolved = (win / retained, loss / retained, push / retained)
    if not math.isclose(sum(resolved), 1.0, abs_tol=1e-9):
        raise FirstFiveValueError("resolved First Five probabilities do not sum to one")
    return resolved


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FirstFiveValueError(f"{name} must be a mapping")
    return value


def _best_books(outcome: Mapping[str, Any]) -> tuple[str, ...]:
    raw = outcome.get("best_price_books")
    if not isinstance(raw, list):
        raise FirstFiveValueError("normalized outcome lacks best_price_books")
    books = tuple(sorted({_text(item, "best_price_book") for item in raw}))
    if not books:
        raise FirstFiveValueError("normalized outcome has no best-price bookmaker")
    return books


def _best_price_freshness(outcome: Mapping[str, Any], best_books: tuple[str, ...]) -> str:
    best_price = _number(outcome.get("best_price"), "best_price")
    offers = outcome.get("offers")
    if not isinstance(offers, list):
        return "unknown"
    statuses: list[str] = []
    for offer in offers:
        if not isinstance(offer, Mapping):
            continue
        if str(offer.get("bookmaker_key") or "") not in best_books:
            continue
        normalized = offer.get("normalized_price")
        if normalized is None or not math.isclose(
            _number(normalized, "normalized_price"),
            best_price,
            abs_tol=1e-12,
        ):
            continue
        status = str(offer.get("freshness_status") or "unknown")
        statuses.append(status if status in _FRESHNESS_RANK else "unknown")
    if not statuses:
        return "unknown"
    return max(statuses, key=_FRESHNESS_RANK.__getitem__)


@dataclass(frozen=True, slots=True)
class FirstFiveOutcomeValueV1:
    source_game_id: str
    provider_event_id: str
    market_family: str
    market_key: str
    side: str
    line_key: str
    market_line: float | None
    american_price: float
    best_price_books: tuple[str, ...]
    bookmaker_count: int
    consensus_confidence: str
    freshness_status: str
    complete_two_way_market: bool
    median_market_hold: float | None
    resolved_model_win_probability: float
    resolved_model_loss_probability: float
    resolved_model_push_probability: float
    unresolved_probability: float
    conditional_model_probability: float
    raw_implied_probability: float
    no_vig_probability: float | None
    raw_probability_edge: float
    no_vig_probability_edge: float | None
    net_profit_per_unit: float
    expected_value_per_unit: float
    expected_roi_percent: float
    fair_decimal_odds: float
    fair_american_odds: float
    market_evidence_checksum: str
    source_prediction_checksum: str
    source_projection_checksum: str
    source_normalized_odds_checksum: str
    prediction_recommendation_eligible: bool
    recommendation_gate_input_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    calculation_version: str = FIRST_FIVE_VALUE_CALCULATION_VERSION
    contract_version: str = FIRST_FIVE_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        if self.market_family not in _SUPPORTED_FAMILIES:
            raise FirstFiveValueError("unsupported First Five value market family")
        object.__setattr__(self, "market_key", _text(self.market_key, "market_key"))
        object.__setattr__(self, "side", _text(self.side, "side"))
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(self, "market_line", _optional_number(self.market_line, "market_line"))
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise FirstFiveValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_text(item, "best_price_book") for item in self.best_price_books}))
        if not books:
            raise FirstFiveValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise FirstFiveValueError("bookmaker_count must be positive")
        object.__setattr__(self, "consensus_confidence", _text(self.consensus_confidence, "consensus_confidence"))
        if self.freshness_status not in _FRESHNESS_RANK:
            raise FirstFiveValueError("freshness_status is invalid")
        object.__setattr__(
            self,
            "median_market_hold",
            _optional_number(self.median_market_hold, "median_market_hold"),
        )
        for name in (
            "resolved_model_win_probability",
            "resolved_model_loss_probability",
            "resolved_model_push_probability",
            "unresolved_probability",
            "conditional_model_probability",
            "raw_implied_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.resolved_model_win_probability
            + self.resolved_model_loss_probability
            + self.resolved_model_push_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise FirstFiveValueError("resolved First Five value probabilities must sum to one")
        decisive = self.resolved_model_win_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise FirstFiveValueError("conditional First Five model probability mismatch")
        object.__setattr__(
            self,
            "no_vig_probability",
            _optional_probability(self.no_vig_probability, "no_vig_probability"),
        )
        for name in (
            "raw_probability_edge",
            "net_profit_per_unit",
            "expected_value_per_unit",
            "expected_roi_percent",
            "fair_decimal_odds",
            "fair_american_odds",
        ):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        object.__setattr__(
            self,
            "no_vig_probability_edge",
            _optional_number(self.no_vig_probability_edge, "no_vig_probability_edge"),
        )
        if not math.isclose(
            self.expected_roi_percent,
            self.expected_value_per_unit * 100.0,
            abs_tol=1e-10,
        ):
            raise FirstFiveValueError("expected ROI must equal EV multiplied by 100")
        for name in (
            "market_evidence_checksum",
            "source_prediction_checksum",
            "source_projection_checksum",
            "source_normalized_odds_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        reasons = tuple(sorted({_text(item, "ineligibility_reason") for item in self.ineligibility_reasons}))
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.prediction_recommendation_eligible:
            raise FirstFiveValueError("V4B cannot consume recommendation-eligible First Five prediction evidence")
        if self.recommendation_gate_input_eligible:
            raise FirstFiveValueError("V4B First Five Value must remain blocked from Recommendation Gate")
        for required in (FIRST_FIVE_REFERENCE_REASON, FIRST_FIVE_BINDING_PENDING_REASON):
            if required not in reasons:
                raise FirstFiveValueError("V4B reference Value is missing required ineligibility reason")
        if self.calculation_version != FIRST_FIVE_VALUE_CALCULATION_VERSION:
            raise FirstFiveValueError("unsupported First Five value calculation version")
        if self.contract_version != FIRST_FIVE_VALUE_OUTCOME_CONTRACT_VERSION:
            raise FirstFiveValueError("unsupported First Five value outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "best_price_books": list(self.best_price_books),
            "bookmaker_count": self.bookmaker_count,
            "calculation_version": self.calculation_version,
            "complete_two_way_market": self.complete_two_way_market,
            "conditional_model_probability": self.conditional_model_probability,
            "consensus_confidence": self.consensus_confidence,
            "contract_version": self.contract_version,
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "fair_american_odds": self.fair_american_odds,
            "fair_decimal_odds": self.fair_decimal_odds,
            "freshness_status": self.freshness_status,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "line_key": self.line_key,
            "market_evidence_checksum": self.market_evidence_checksum,
            "market_family": self.market_family,
            "market_key": self.market_key,
            "market_line": self.market_line,
            "median_market_hold": self.median_market_hold,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "provider_event_id": self.provider_event_id,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": self.recommendation_gate_input_eligible,
            "resolved_model_loss_probability": self.resolved_model_loss_probability,
            "resolved_model_push_probability": self.resolved_model_push_probability,
            "resolved_model_win_probability": self.resolved_model_win_probability,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_projection_checksum": self.source_projection_checksum,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class FirstFiveGameValueV1:
    source_game_id: str
    provider_event_id: str
    upstream_first_five_scoring_checksum: str
    source_normalized_odds_checksum: str
    outcomes: tuple[FirstFiveOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = FIRST_FIVE_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "upstream_first_five_scoring_checksum",
            _sha(self.upstream_first_five_scoring_checksum, "upstream_first_five_scoring_checksum"),
        )
        object.__setattr__(
            self,
            "source_normalized_odds_checksum",
            _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"),
        )
        outcomes = tuple(self.outcomes)
        if len({item.checksum for item in outcomes}) != len(outcomes):
            raise FirstFiveValueError("First Five game Value contains duplicate outcomes")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.source_normalized_odds_checksum != self.source_normalized_odds_checksum
            for item in outcomes
        ):
            raise FirstFiveValueError("First Five game Value outcome lineage mismatch")
        if any(item.recommendation_gate_input_eligible for item in outcomes):
            raise FirstFiveValueError("reference First Five Value cannot contain Gate-eligible rows")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if self.contract_version != FIRST_FIVE_VALUE_GAME_CONTRACT_VERSION:
            raise FirstFiveValueError("unsupported First Five game Value contract")

    @property
    def recommendation_gate_input_count(self) -> int:
        return sum(item.recommendation_gate_input_eligible for item in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "outcomes": [item.as_dict() for item in self.outcomes],
            "provider_event_id": self.provider_event_id,
            "recommendation_gate_input_count": self.recommendation_gate_input_count,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "upstream_first_five_scoring_checksum": self.upstream_first_five_scoring_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _value_row(
    *,
    source_game_id: str,
    provider_event_id: str,
    market_family: str,
    market_key: str,
    side: str,
    line: Mapping[str, Any],
    outcome: Mapping[str, Any],
    market_line: float | None,
    win: float,
    loss: float,
    push: float,
    unresolved: float,
    source_prediction_checksum: str,
    source_projection_checksum: str,
    source_normalized_odds_checksum: str,
) -> FirstFiveOutcomeValueV1:
    best_price = _number(outcome.get("best_price"), "best_price")
    best_books = _best_books(outcome)
    resolved_win, resolved_loss, resolved_push = _resolved_probabilities(
        win=win,
        loss=loss,
        push=push,
        unresolved=unresolved,
    )
    value_math = calculate_value_math(
        win_probability=resolved_win,
        loss_probability=resolved_loss,
        push_probability=resolved_push,
        price=best_price,
        retained_raw_implied_probability=outcome.get("best_price_implied_probability"),
        no_vig_probability=outcome.get("no_vig_probability"),
    )
    reasons = {FIRST_FIVE_REFERENCE_REASON, FIRST_FIVE_BINDING_PENDING_REASON}
    if not bool(line.get("complete_two_way_market")):
        reasons.add("incomplete_two_way_market")
    if value_math.no_vig_probability is None:
        reasons.add("no_vig_probability_unavailable")
    freshness = _best_price_freshness(outcome, best_books)
    if freshness in {"stale", "unknown"}:
        reasons.add(f"market_freshness_{freshness}")
    market_evidence_checksum = canonical_sha256(
        {
            "line": dict(line),
            "market_key": market_key,
            "outcome": dict(outcome),
            "side": side,
        }
    )
    return FirstFiveOutcomeValueV1(
        source_game_id=source_game_id,
        provider_event_id=provider_event_id,
        market_family=market_family,
        market_key=market_key,
        side=side,
        line_key=_text(line.get("line_key"), "line_key"),
        market_line=market_line,
        american_price=best_price,
        best_price_books=best_books,
        bookmaker_count=int(outcome.get("bookmaker_count") or 0),
        consensus_confidence=_text(outcome.get("consensus_confidence"), "consensus_confidence"),
        freshness_status=freshness,
        complete_two_way_market=bool(line.get("complete_two_way_market")),
        median_market_hold=_optional_number(line.get("median_market_hold"), "median_market_hold"),
        resolved_model_win_probability=value_math.model_win_probability,
        resolved_model_loss_probability=value_math.model_loss_probability,
        resolved_model_push_probability=value_math.model_push_probability,
        unresolved_probability=unresolved,
        conditional_model_probability=value_math.conditional_model_probability,
        raw_implied_probability=value_math.raw_implied_probability,
        no_vig_probability=value_math.no_vig_probability,
        raw_probability_edge=value_math.raw_probability_edge,
        no_vig_probability_edge=value_math.no_vig_probability_edge,
        net_profit_per_unit=value_math.net_profit_per_unit,
        expected_value_per_unit=value_math.expected_value_per_unit,
        expected_roi_percent=value_math.expected_roi_percent,
        fair_decimal_odds=value_math.fair_decimal_odds,
        fair_american_odds=value_math.fair_american_odds,
        market_evidence_checksum=market_evidence_checksum,
        source_prediction_checksum=source_prediction_checksum,
        source_projection_checksum=source_projection_checksum,
        source_normalized_odds_checksum=source_normalized_odds_checksum,
        prediction_recommendation_eligible=False,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=tuple(reasons),
    )


def _line(summary: Mapping[str, Any], market_key: str, line_key: str) -> Mapping[str, Any] | None:
    markets = summary.get("markets")
    if not isinstance(markets, Mapping):
        return None
    market = markets.get(market_key)
    if not isinstance(market, Mapping):
        return None
    lines = market.get("lines")
    if not isinstance(lines, Mapping):
        return None
    value = lines.get(line_key)
    return value if isinstance(value, Mapping) else None


def _all_lines(summary: Mapping[str, Any], market_key: str) -> tuple[Mapping[str, Any], ...]:
    markets = summary.get("markets")
    if not isinstance(markets, Mapping):
        return ()
    market = markets.get(market_key)
    if not isinstance(market, Mapping):
        return ()
    lines = market.get("lines")
    if not isinstance(lines, Mapping):
        return ()
    return tuple(
        value
        for _, value in sorted(lines.items(), key=lambda item: str(item[0]))
        if isinstance(value, Mapping)
        and bool(value.get("valid_line"))
        and value.get("line") is not None
    )


def evaluate_first_five_value(
    moneyline: FirstFiveMoneylinePredictionV1,
    run_line: FirstFiveRunLinePredictionV1,
    total: FirstFiveTotalPredictionV1,
    odds: FirstFiveNormalizedOddsV1,
) -> FirstFiveGameValueV1:
    """Evaluate all retained F5 markets while remaining reference/Gate-ineligible."""

    predictions = (moneyline, run_line, total)
    if len({item.source_game_id for item in predictions}) != 1:
        raise FirstFiveValueError("First Five predictions disagree on source game")
    if len({item.away_team_id for item in predictions}) != 1 or len(
        {item.home_team_id for item in predictions}
    ) != 1:
        raise FirstFiveValueError("First Five predictions disagree on team identity")
    scoring_checksums = {item.upstream_first_five_scoring_checksum for item in predictions}
    if len(scoring_checksums) != 1:
        raise FirstFiveValueError("First Five predictions disagree on scoring source")
    if any(item.recommendation_eligible for item in predictions):
        raise FirstFiveValueError("V4B requires reference First Five predictions")

    summary = odds.summary
    if str(summary.get("event_id") or "") != odds.provider_event_id:
        raise FirstFiveValueError("First Five normalized odds event identity mismatch")
    if summary.get("home_team_key") != moneyline.home_team_id or summary.get(
        "away_team_key"
    ) != moneyline.away_team_id:
        raise FirstFiveValueError("First Five odds and prediction teams do not match")

    source_game_id = moneyline.source_game_id
    provider_event_id = odds.provider_event_id
    normalized_checksum = odds.checksum
    outcomes: list[FirstFiveOutcomeValueV1] = []
    warnings: list[str] = []

    ml_line = _line(summary, "h2h_1st_5_innings", "moneyline")
    if ml_line is None:
        warnings.append("first_five_moneyline_market_unavailable")
    else:
        ml_outcomes = _mapping(ml_line.get("outcomes"), "moneyline outcomes")
        projection_checksum = canonical_sha256(
            {
                "away_win_probability": moneyline.away_win_probability,
                "home_win_probability": moneyline.home_win_probability,
                "tie_probability": moneyline.tie_probability,
                "unresolved_probability": moneyline.unresolved_probability,
            }
        )
        for side, win, loss in (
            (moneyline.home_team_id, moneyline.home_win_probability, moneyline.away_win_probability),
            (moneyline.away_team_id, moneyline.away_win_probability, moneyline.home_win_probability),
        ):
            outcome = ml_outcomes.get(side)
            if not isinstance(outcome, Mapping) or outcome.get("best_price") is None:
                warnings.append(f"first_five_moneyline_price_unavailable:{side}")
                continue
            outcomes.append(
                _value_row(
                    source_game_id=source_game_id,
                    provider_event_id=provider_event_id,
                    market_family=PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value,
                    market_key="h2h_1st_5_innings",
                    side=side,
                    line=ml_line,
                    outcome=outcome,
                    market_line=None,
                    win=win,
                    loss=loss,
                    push=moneyline.tie_probability,
                    unresolved=moneyline.unresolved_probability,
                    source_prediction_checksum=moneyline.checksum,
                    source_projection_checksum=projection_checksum,
                    source_normalized_odds_checksum=normalized_checksum,
                )
            )

    for line in _all_lines(summary, "spreads_1st_5_innings"):
        home_spread = _number(line.get("line"), "home_spread")
        projection = run_line.project(home_spread)
        projection_checksum = canonical_sha256(projection.as_dict())
        line_outcomes = _mapping(line.get("outcomes"), "run-line outcomes")
        for side, win, loss in (
            (run_line.home_team_id, projection.home_cover_probability, projection.away_cover_probability),
            (run_line.away_team_id, projection.away_cover_probability, projection.home_cover_probability),
        ):
            outcome = line_outcomes.get(side)
            if not isinstance(outcome, Mapping) or outcome.get("best_price") is None:
                warnings.append(f"first_five_run_line_price_unavailable:{side}:{home_spread:g}")
                continue
            outcomes.append(
                _value_row(
                    source_game_id=source_game_id,
                    provider_event_id=provider_event_id,
                    market_family=PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value,
                    market_key="spreads_1st_5_innings",
                    side=side,
                    line=line,
                    outcome=outcome,
                    market_line=home_spread,
                    win=win,
                    loss=loss,
                    push=projection.push_probability,
                    unresolved=projection.unresolved_probability,
                    source_prediction_checksum=run_line.checksum,
                    source_projection_checksum=projection_checksum,
                    source_normalized_odds_checksum=normalized_checksum,
                )
            )

    for line in _all_lines(summary, "totals_1st_5_innings"):
        total_line = _number(line.get("line"), "total_line")
        projection = total.project(total_line)
        projection_checksum = canonical_sha256(projection.as_dict())
        line_outcomes = _mapping(line.get("outcomes"), "total outcomes")
        for side, outcome_key, win, loss in (
            ("over", "Over", projection.over_probability, projection.under_probability),
            ("under", "Under", projection.under_probability, projection.over_probability),
        ):
            outcome = line_outcomes.get(outcome_key)
            if not isinstance(outcome, Mapping) or outcome.get("best_price") is None:
                warnings.append(f"first_five_total_price_unavailable:{side}:{total_line:g}")
                continue
            outcomes.append(
                _value_row(
                    source_game_id=source_game_id,
                    provider_event_id=provider_event_id,
                    market_family=PredictionMarketFamily.FIRST_FIVE_TOTAL.value,
                    market_key="totals_1st_5_innings",
                    side=side,
                    line=line,
                    outcome=outcome,
                    market_line=total_line,
                    win=win,
                    loss=loss,
                    push=projection.push_probability,
                    unresolved=projection.unresolved_probability,
                    source_prediction_checksum=total.checksum,
                    source_projection_checksum=projection_checksum,
                    source_normalized_odds_checksum=normalized_checksum,
                )
            )

    return FirstFiveGameValueV1(
        source_game_id=source_game_id,
        provider_event_id=provider_event_id,
        upstream_first_five_scoring_checksum=next(iter(scoring_checksums)),
        source_normalized_odds_checksum=normalized_checksum,
        outcomes=tuple(outcomes),
        warnings=tuple(warnings),
    )
