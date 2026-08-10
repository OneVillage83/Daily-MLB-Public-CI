from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.team_totals import (
    TEAM_TOTAL_NORMALIZED_MARKET_KEY,
    TeamTotalNormalizedTeamOddsV1,
    TeamTotalsNormalizedOddsV1,
)
from app.predictions.team_total import TeamTotalPredictionV1, TeamTotalProjectionV1
from app.value_engine.pricing import ValueMathError, calculate_value_math

TEAM_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_VALUE_OUTCOME_V1"
TEAM_TOTAL_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_VALUE_GAME_V1"
TEAM_TOTAL_VALUE_CALCULATION_VERSION = "DSE_MLB_TEAM_TOTAL_VALUE_V1"
TEAM_TOTAL_REFERENCE_REASON = "prediction_not_recommendation_eligible"
TEAM_TOTAL_BINDING_PENDING_REASON = "team_total_production_event_binding_pending"
MAX_UNRESOLVED_TEAM_TOTAL_VALUE_TAIL = 1e-6

_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}


class TeamTotalsValueError(ValueError):
    """Raised when V6B team-total Value evidence violates its reference contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TeamTotalsValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TeamTotalsValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TeamTotalsValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise TeamTotalsValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise TeamTotalsValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _probability(value, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TeamTotalsValueError(f"{name} must be a mapping")
    return value


def _resolved_projection(
    projection: TeamTotalProjectionV1,
) -> tuple[float, float, float]:
    retained = 1.0 - projection.unresolved_probability
    if retained <= 0.0:
        raise TeamTotalsValueError("team-total projection contains no resolved mass")
    over = projection.over_probability / retained
    under = projection.under_probability / retained
    push = projection.push_probability / retained
    if not math.isclose(over + under + push, 1.0, abs_tol=1e-9):
        raise TeamTotalsValueError("resolved team-total probabilities do not sum to one")
    return over, under, push


def _best_books(outcome: Mapping[str, Any]) -> tuple[str, ...]:
    raw = outcome.get("best_price_books")
    if not isinstance(raw, list):
        raise TeamTotalsValueError("normalized team-total outcome lacks best_price_books")
    books = tuple(sorted({_text(item, "best_price_book") for item in raw}))
    if not books:
        raise TeamTotalsValueError("normalized team-total outcome has no best-price bookmaker")
    return books


def _best_price_freshness(
    outcome: Mapping[str, Any],
    best_books: tuple[str, ...],
) -> str:
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


def _all_lines(team_odds: TeamTotalNormalizedTeamOddsV1) -> tuple[Mapping[str, Any], ...]:
    markets = team_odds.summary.get("markets")
    if not isinstance(markets, Mapping):
        return ()
    market = markets.get(TEAM_TOTAL_NORMALIZED_MARKET_KEY)
    if not isinstance(market, Mapping):
        return ()
    lines = market.get("lines")
    if not isinstance(lines, Mapping):
        return ()
    return tuple(
        value
        for _, value in sorted(lines.items(), key=lambda item: str(item[0]))
        if isinstance(value, Mapping)
        and value.get("valid_line") is True
        and value.get("line") is not None
    )


@dataclass(frozen=True, slots=True)
class TeamTotalOutcomeValueV1:
    source_game_id: str
    provider_event_id: str
    team_id: str
    opponent_team_id: str
    side: str
    line_key: str
    total_line: float
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
    source_normalized_team_odds_checksum: str
    source_normalized_odds_checksum: str
    prediction_recommendation_eligible: bool
    recommendation_gate_input_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    calculation_version: str = TEAM_TOTAL_VALUE_CALCULATION_VERSION
    contract_version: str = TEAM_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "team_id",
            "opponent_team_id",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise TeamTotalsValueError("team-total Value subject and opponent must differ")
        if self.side not in {"over", "under"}:
            raise TeamTotalsValueError("team-total Value side must be over or under")
        total_line = _number(self.total_line, "total_line")
        if total_line < 0.0:
            raise TeamTotalsValueError("team-total line must be nonnegative")
        object.__setattr__(self, "total_line", total_line)
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise TeamTotalsValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_text(item, "best_price_book") for item in self.best_price_books}))
        if not books:
            raise TeamTotalsValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count < 1
        ):
            raise TeamTotalsValueError("bookmaker_count must be positive")
        object.__setattr__(
            self,
            "consensus_confidence",
            _text(self.consensus_confidence, "consensus_confidence"),
        )
        if self.freshness_status not in _FRESHNESS_RANK:
            raise TeamTotalsValueError("freshness_status is invalid")
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
            raise TeamTotalsValueError("resolved team-total Value probabilities must sum to one")
        decisive = self.resolved_model_win_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise TeamTotalsValueError("conditional team-total model probability mismatch")
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
            raise TeamTotalsValueError("expected ROI must equal EV multiplied by 100")
        for name in (
            "market_evidence_checksum",
            "source_prediction_checksum",
            "source_projection_checksum",
            "source_normalized_team_odds_checksum",
            "source_normalized_odds_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        reasons = tuple(
            sorted({_text(item, "ineligibility_reason") for item in self.ineligibility_reasons})
        )
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.prediction_recommendation_eligible:
            raise TeamTotalsValueError("V6B cannot consume recommendation-eligible team-total predictions")
        if self.recommendation_gate_input_eligible:
            raise TeamTotalsValueError("V6B team-total Value must remain blocked from Recommendation Gate")
        for required in (TEAM_TOTAL_REFERENCE_REASON, TEAM_TOTAL_BINDING_PENDING_REASON):
            if required not in reasons:
                raise TeamTotalsValueError("V6B reference Value is missing required blocker")
        if self.calculation_version != TEAM_TOTAL_VALUE_CALCULATION_VERSION:
            raise TeamTotalsValueError("unsupported team-total Value calculation version")
        if self.contract_version != TEAM_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION:
            raise TeamTotalsValueError("unsupported team-total Value outcome contract")

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
            "median_market_hold": self.median_market_hold,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "opponent_team_id": self.opponent_team_id,
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
            "source_normalized_team_odds_checksum": self.source_normalized_team_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_projection_checksum": self.source_projection_checksum,
            "team_id": self.team_id,
            "total_line": self.total_line,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class TeamTotalsGameValueV1:
    source_game_id: str
    provider_event_id: str
    away_team_id: str
    home_team_id: str
    source_prediction_checksums: tuple[str, str]
    source_normalized_odds_checksum: str
    outcomes: tuple[TeamTotalOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = TEAM_TOTAL_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("source_game_id", "provider_event_id", "away_team_id", "home_team_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.away_team_id == self.home_team_id:
            raise TeamTotalsValueError("team-total game teams must differ")
        checksums = tuple(self.source_prediction_checksums)
        if len(checksums) != 2:
            raise TeamTotalsValueError("team-total game Value requires away/home prediction checksums")
        object.__setattr__(
            self,
            "source_prediction_checksums",
            (_sha(checksums[0], "away prediction checksum"), _sha(checksums[1], "home prediction checksum")),
        )
        object.__setattr__(
            self,
            "source_normalized_odds_checksum",
            _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"),
        )
        outcomes = tuple(self.outcomes)
        if len({item.checksum for item in outcomes}) != len(outcomes):
            raise TeamTotalsValueError("team-total game Value contains duplicate outcomes")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.source_normalized_odds_checksum != self.source_normalized_odds_checksum
            or item.team_id not in {self.away_team_id, self.home_team_id}
            for item in outcomes
        ):
            raise TeamTotalsValueError("team-total game Value outcome lineage mismatch")
        if any(item.recommendation_gate_input_eligible for item in outcomes):
            raise TeamTotalsValueError("reference team-total Value cannot contain Gate-eligible rows")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if not outcomes and not self.warnings:
            raise TeamTotalsValueError("empty team-total Value requires an explicit warning")
        if self.contract_version != TEAM_TOTAL_VALUE_GAME_CONTRACT_VERSION:
            raise TeamTotalsValueError("unsupported team-total game Value contract")

    @property
    def recommendation_gate_input_count(self) -> int:
        return sum(item.recommendation_gate_input_eligible for item in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
            "outcomes": [item.as_dict() for item in self.outcomes],
            "provider_event_id": self.provider_event_id,
            "recommendation_gate_input_count": self.recommendation_gate_input_count,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksums": list(self.source_prediction_checksums),
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _value_row(
    *,
    prediction: TeamTotalPredictionV1,
    provider_event_id: str,
    line: Mapping[str, Any],
    outcome: Mapping[str, Any],
    total_line: float,
    projection: TeamTotalProjectionV1,
    side: str,
    source_normalized_team_odds_checksum: str,
    source_normalized_odds_checksum: str,
) -> TeamTotalOutcomeValueV1 | None:
    best_price_raw = outcome.get("best_price")
    if best_price_raw is None:
        return None
    best_price = _number(best_price_raw, "best_price")
    best_books = _best_books(outcome)
    resolved_over, resolved_under, resolved_push = _resolved_projection(projection)
    win, loss = (
        (resolved_over, resolved_under)
        if side == "over"
        else (resolved_under, resolved_over)
    )
    try:
        value_math = calculate_value_math(
            win_probability=win,
            loss_probability=loss,
            push_probability=resolved_push,
            price=best_price,
            retained_raw_implied_probability=outcome.get("best_price_implied_probability"),
            no_vig_probability=outcome.get("no_vig_probability"),
        )
    except ValueMathError as exc:
        raise TeamTotalsValueError(
            f"invalid team-total Value math for {prediction.team_id}/{total_line:g}/{side}"
        ) from exc

    reasons = {TEAM_TOTAL_REFERENCE_REASON, TEAM_TOTAL_BINDING_PENDING_REASON}
    if line.get("complete_two_way_market") is not True:
        reasons.add("incomplete_two_way_market")
    if value_math.no_vig_probability is None:
        reasons.add("no_vig_probability_unavailable")
    freshness = _best_price_freshness(outcome, best_books)
    if freshness in {"stale", "unknown"}:
        reasons.add(f"market_freshness_{freshness}")
    if projection.unresolved_probability > MAX_UNRESOLVED_TEAM_TOTAL_VALUE_TAIL:
        reasons.add("unresolved_tail_exceeds_value_tolerance")

    projection_checksum = projection.checksum
    market_evidence_checksum = canonical_sha256(
        {
            "line": dict(line),
            "outcome": dict(outcome),
            "side": side,
            "source_normalized_team_odds_checksum": source_normalized_team_odds_checksum,
            "team_id": prediction.team_id,
        }
    )
    return TeamTotalOutcomeValueV1(
        source_game_id=prediction.source_game_id,
        provider_event_id=provider_event_id,
        team_id=prediction.team_id,
        opponent_team_id=prediction.opponent_team_id,
        side=side,
        line_key=_text(line.get("line_key"), "line_key"),
        total_line=total_line,
        american_price=value_math.american_price,
        best_price_books=best_books,
        bookmaker_count=int(outcome.get("bookmaker_count") or 0),
        consensus_confidence=_text(outcome.get("consensus_confidence"), "consensus_confidence"),
        freshness_status=freshness,
        complete_two_way_market=line.get("complete_two_way_market") is True,
        median_market_hold=_optional_number(line.get("median_market_hold"), "median_market_hold"),
        resolved_model_win_probability=value_math.model_win_probability,
        resolved_model_loss_probability=value_math.model_loss_probability,
        resolved_model_push_probability=value_math.model_push_probability,
        unresolved_probability=projection.unresolved_probability,
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
        source_prediction_checksum=prediction.checksum,
        source_projection_checksum=projection_checksum,
        source_normalized_team_odds_checksum=source_normalized_team_odds_checksum,
        source_normalized_odds_checksum=source_normalized_odds_checksum,
        prediction_recommendation_eligible=prediction.recommendation_eligible,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=tuple(reasons),
    )


def evaluate_team_totals_value(
    predictions: tuple[TeamTotalPredictionV1, TeamTotalPredictionV1],
    odds: TeamTotalsNormalizedOddsV1,
) -> TeamTotalsGameValueV1:
    """Evaluate every retained team-total line for both clubs without promotion."""

    away_prediction, home_prediction = tuple(predictions)
    if away_prediction.source_game_id != home_prediction.source_game_id:
        raise TeamTotalsValueError("team-total predictions disagree on source game")
    if (
        away_prediction.away_team_id != home_prediction.away_team_id
        or away_prediction.home_team_id != home_prediction.home_team_id
    ):
        raise TeamTotalsValueError("team-total predictions disagree on game teams")
    if (away_prediction.team_id, home_prediction.team_id) != (
        away_prediction.away_team_id,
        away_prediction.home_team_id,
    ):
        raise TeamTotalsValueError("team-total predictions must be deterministic away then home")
    if any(item.recommendation_eligible for item in predictions):
        raise TeamTotalsValueError("V6B requires reference team-total predictions")
    if (odds.away_team_id, odds.home_team_id) != (
        away_prediction.away_team_id,
        away_prediction.home_team_id,
    ):
        raise TeamTotalsValueError("team-total odds and predictions disagree on team identity")

    outcomes: list[TeamTotalOutcomeValueV1] = []
    warnings: list[str] = []
    for prediction in predictions:
        team_odds = odds.for_team(prediction.team_id)
        lines = _all_lines(team_odds)
        if not lines:
            warnings.append(f"team_total_market_unavailable:{prediction.team_id}")
            continue
        for line in lines:
            total_line = _number(line.get("line"), "total_line")
            projection = prediction.project(total_line)
            raw_outcomes = _mapping(line.get("outcomes"), "team-total outcomes")
            for side, provider_side in (("over", "Over"), ("under", "Under")):
                raw_outcome = raw_outcomes.get(provider_side)
                if not isinstance(raw_outcome, Mapping):
                    warnings.append(
                        f"team_total_price_unavailable:{prediction.team_id}:{side}:{total_line:g}"
                    )
                    continue
                value = _value_row(
                    prediction=prediction,
                    provider_event_id=odds.provider_event_id,
                    line=line,
                    outcome=raw_outcome,
                    total_line=total_line,
                    projection=projection,
                    side=side,
                    source_normalized_team_odds_checksum=team_odds.checksum,
                    source_normalized_odds_checksum=odds.checksum,
                )
                if value is not None:
                    outcomes.append(value)

    team_order = {away_prediction.team_id: 0, home_prediction.team_id: 1}
    outcomes.sort(
        key=lambda item: (
            team_order[item.team_id],
            item.total_line,
            0 if item.side == "over" else 1,
            item.line_key,
        )
    )
    if not outcomes and not warnings:
        warnings.append("no_evaluable_team_total_market")
    return TeamTotalsGameValueV1(
        source_game_id=away_prediction.source_game_id,
        provider_event_id=odds.provider_event_id,
        away_team_id=away_prediction.away_team_id,
        home_team_id=away_prediction.home_team_id,
        source_prediction_checksums=(away_prediction.checksum, home_prediction.checksum),
        source_normalized_odds_checksum=odds.checksum,
        outcomes=tuple(outcomes),
        warnings=tuple(warnings),
    )
