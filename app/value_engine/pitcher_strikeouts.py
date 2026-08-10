from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_MARKETS,
    PitcherStrikeoutNormalizedOddsV1,
    provider_pitcher_key,
)
from app.predictions.pitcher_strikeouts import (
    PitcherStarterBindingState,
    PitcherStrikeoutPredictionV1,
    PitcherStrikeoutProjectionV1,
)
from app.value_engine.pricing import calculate_value_math

PITCHER_STRIKEOUT_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_VALUE_OUTCOME_V1"
PITCHER_STRIKEOUT_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_VALUE_GAME_V1"
PITCHER_STRIKEOUT_VALUE_CALCULATION_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_VALUE_V1"
PITCHER_STRIKEOUT_REFERENCE_REASON = "prediction_not_recommendation_eligible"
PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON = "pitcher_strikeout_production_event_binding_pending"
PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON = "pitcher_strikeout_production_player_binding_pending"
PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON = "pitcher_strikeout_starter_confirmation_pending"
MAX_UNRESOLVED_PITCHER_STRIKEOUT_VALUE_TAIL = 1e-6
_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}


class PitcherStrikeoutValueError(ValueError):
    """Raised when V7B pitcher-strikeout Value evidence violates its contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PitcherStrikeoutValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PitcherStrikeoutValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PitcherStrikeoutValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PitcherStrikeoutValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise PitcherStrikeoutValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PitcherStrikeoutValueError(f"{name} must be a mapping")
    return value


def _best_books(outcome: Mapping[str, Any]) -> tuple[str, ...]:
    raw = outcome.get("best_price_books")
    if not isinstance(raw, list):
        raise PitcherStrikeoutValueError("normalized V7 outcome lacks best_price_books")
    books = tuple(sorted({_text(item, "best_price_book") for item in raw}))
    if not books:
        raise PitcherStrikeoutValueError("normalized V7 outcome has no best-price bookmaker")
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
            _number(normalized, "normalized_price"), best_price, abs_tol=1e-12
        ):
            continue
        status = str(offer.get("freshness_status") or "unknown")
        statuses.append(status if status in _FRESHNESS_RANK else "unknown")
    if not statuses:
        return "unknown"
    return max(statuses, key=_FRESHNESS_RANK.__getitem__)


def _resolved_projection(
    projection: PitcherStrikeoutProjectionV1,
) -> tuple[float, float, float]:
    retained = 1.0 - projection.unresolved_probability
    if retained <= 0.0:
        raise PitcherStrikeoutValueError("V7 projection contains no resolved mass")
    over = projection.over_probability / retained
    under = projection.under_probability / retained
    push = projection.push_probability / retained
    if not math.isclose(over + under + push, 1.0, abs_tol=1e-9):
        raise PitcherStrikeoutValueError("resolved V7 probabilities do not sum to one")
    return over, under, push


def _all_lines(pitcher: Mapping[str, Any]) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    markets = pitcher.get("markets")
    if not isinstance(markets, Mapping):
        return ()
    for market_key in PITCHER_STRIKEOUT_MARKETS:
        market = markets.get(market_key)
        if not isinstance(market, Mapping):
            continue
        lines = market.get("lines")
        if not isinstance(lines, Mapping):
            continue
        for _, line in sorted(lines.items(), key=lambda item: str(item[0])):
            if (
                isinstance(line, Mapping)
                and bool(line.get("valid_line"))
                and line.get("line") is not None
            ):
                rows.append((market_key, line))
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutOutcomeValueV1:
    source_game_id: str
    provider_event_id: str
    pitcher_id: str
    pitcher_name: str
    provider_pitcher_key: str
    team_id: str
    opponent_team_id: str
    starter_binding_state: str
    starter_binding_checksum: str
    market_key: str
    side: str
    line_key: str
    line: float
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
    calculation_version: str = PITCHER_STRIKEOUT_VALUE_CALCULATION_VERSION
    contract_version: str = PITCHER_STRIKEOUT_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "pitcher_id",
            "pitcher_name",
            "provider_pitcher_key",
            "team_id",
            "opponent_team_id",
            "starter_binding_state",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise PitcherStrikeoutValueError("V7 Value team and opponent must differ")
        if self.starter_binding_state not in {"expected", "confirmed"}:
            raise PitcherStrikeoutValueError("V7 Value starter state must be expected or confirmed")
        object.__setattr__(
            self,
            "starter_binding_checksum",
            _sha(self.starter_binding_checksum, "starter_binding_checksum"),
        )
        if self.market_key not in PITCHER_STRIKEOUT_MARKETS:
            raise PitcherStrikeoutValueError("unsupported V7 Value market key")
        if self.side not in {"over", "under"}:
            raise PitcherStrikeoutValueError("V7 Value side must be over or under")
        line = _number(self.line, "line")
        if line < 0.0:
            raise PitcherStrikeoutValueError("V7 Value line must be nonnegative")
        object.__setattr__(self, "line", line)
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise PitcherStrikeoutValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_text(item, "best_price_book") for item in self.best_price_books}))
        if not books:
            raise PitcherStrikeoutValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise PitcherStrikeoutValueError("bookmaker_count must be positive")
        object.__setattr__(self, "consensus_confidence", _text(self.consensus_confidence, "consensus_confidence"))
        if self.freshness_status not in _FRESHNESS_RANK:
            raise PitcherStrikeoutValueError("freshness_status is invalid")
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
            raise PitcherStrikeoutValueError("resolved V7 Value probabilities must sum to one")
        decisive = self.resolved_model_win_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise PitcherStrikeoutValueError("conditional V7 model probability mismatch")
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
            raise PitcherStrikeoutValueError("expected ROI must equal EV multiplied by 100")
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
            raise PitcherStrikeoutValueError("V7B cannot consume recommendation-eligible prediction evidence")
        if self.recommendation_gate_input_eligible:
            raise PitcherStrikeoutValueError("V7B Value must remain blocked from Recommendation Gate")
        for required in (
            PITCHER_STRIKEOUT_REFERENCE_REASON,
            PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
            PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
        ):
            if required not in reasons:
                raise PitcherStrikeoutValueError("V7B Value is missing a required reference blocker")
        if (
            self.starter_binding_state == PitcherStarterBindingState.EXPECTED.value
            and PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON not in reasons
        ):
            raise PitcherStrikeoutValueError("expected starter Value must remain confirmation-blocked")
        if self.calculation_version != PITCHER_STRIKEOUT_VALUE_CALCULATION_VERSION:
            raise PitcherStrikeoutValueError("unsupported V7B Value calculation")
        if self.contract_version != PITCHER_STRIKEOUT_VALUE_OUTCOME_CONTRACT_VERSION:
            raise PitcherStrikeoutValueError("unsupported V7B Value outcome contract")

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
            "line": self.line,
            "line_key": self.line_key,
            "market_evidence_checksum": self.market_evidence_checksum,
            "market_key": self.market_key,
            "median_market_hold": self.median_market_hold,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "opponent_team_id": self.opponent_team_id,
            "pitcher_id": self.pitcher_id,
            "pitcher_name": self.pitcher_name,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "provider_event_id": self.provider_event_id,
            "provider_pitcher_key": self.provider_pitcher_key,
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
            "starter_binding_checksum": self.starter_binding_checksum,
            "starter_binding_state": self.starter_binding_state,
            "team_id": self.team_id,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutGameValueV1:
    source_game_id: str
    provider_event_id: str
    source_normalized_odds_checksum: str
    source_prediction_checksum: str
    outcomes: tuple[PitcherStrikeoutOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = PITCHER_STRIKEOUT_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "source_normalized_odds_checksum",
            _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"),
        )
        object.__setattr__(
            self,
            "source_prediction_checksum",
            _sha(self.source_prediction_checksum, "source_prediction_checksum"),
        )
        outcomes = tuple(self.outcomes)
        if len({item.checksum for item in outcomes}) != len(outcomes):
            raise PitcherStrikeoutValueError("V7B game Value contains duplicate outcomes")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.source_normalized_odds_checksum != self.source_normalized_odds_checksum
            or item.source_prediction_checksum != self.source_prediction_checksum
            for item in outcomes
        ):
            raise PitcherStrikeoutValueError("V7B game Value outcome lineage mismatch")
        if any(item.recommendation_gate_input_eligible for item in outcomes):
            raise PitcherStrikeoutValueError("reference V7B Value cannot contain Gate-eligible rows")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if not outcomes and not self.warnings:
            raise PitcherStrikeoutValueError("empty V7B game Value requires a warning")
        if self.contract_version != PITCHER_STRIKEOUT_VALUE_GAME_CONTRACT_VERSION:
            raise PitcherStrikeoutValueError("unsupported V7B game Value contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "outcomes": [item.as_dict() for item in self.outcomes],
            "provider_event_id": self.provider_event_id,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _value_row(
    *,
    prediction: PitcherStrikeoutPredictionV1,
    provider_event_id: str,
    pitcher_key: str,
    market_key: str,
    line: Mapping[str, Any],
    outcome: Mapping[str, Any],
    side: str,
    projection: PitcherStrikeoutProjectionV1,
    normalized_checksum: str,
) -> PitcherStrikeoutOutcomeValueV1:
    best_price = _number(outcome.get("best_price"), "best_price")
    best_books = _best_books(outcome)
    over, under, push = _resolved_projection(projection)
    win, loss = (over, under) if side == "over" else (under, over)
    value_math = calculate_value_math(
        win_probability=win,
        loss_probability=loss,
        push_probability=push,
        price=best_price,
        retained_raw_implied_probability=outcome.get("best_price_implied_probability"),
        no_vig_probability=outcome.get("no_vig_probability"),
    )
    reasons = {
        PITCHER_STRIKEOUT_REFERENCE_REASON,
        PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
        PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
    }
    if prediction.starter_binding_state is PitcherStarterBindingState.EXPECTED:
        reasons.add(PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON)
    if projection.unresolved_probability > MAX_UNRESOLVED_PITCHER_STRIKEOUT_VALUE_TAIL:
        reasons.add("pitcher_strikeout_unresolved_tail_above_tolerance")
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
            "pitcher_key": pitcher_key,
            "side": side,
        }
    )
    return PitcherStrikeoutOutcomeValueV1(
        source_game_id=prediction.source_game_id,
        provider_event_id=provider_event_id,
        pitcher_id=prediction.pitcher_id,
        pitcher_name=prediction.pitcher_name,
        provider_pitcher_key=pitcher_key,
        team_id=prediction.team_id,
        opponent_team_id=prediction.opponent_team_id,
        starter_binding_state=prediction.starter_binding_state.value,
        starter_binding_checksum=prediction.starter_binding_checksum,
        market_key=market_key,
        side=side,
        line_key=_text(line.get("line_key"), "line_key"),
        line=projection.line,
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
        source_projection_checksum=projection.checksum,
        source_normalized_odds_checksum=normalized_checksum,
        prediction_recommendation_eligible=False,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=tuple(reasons),
    )


def evaluate_pitcher_strikeout_value(
    prediction: PitcherStrikeoutPredictionV1,
    odds: PitcherStrikeoutNormalizedOddsV1,
) -> PitcherStrikeoutGameValueV1:
    if prediction.recommendation_eligible:
        raise PitcherStrikeoutValueError("V7B requires reference prediction evidence")
    summary = odds.summary
    teams = {str(summary.get("home_team_key") or ""), str(summary.get("away_team_key") or "")}
    if teams != {prediction.team_id, prediction.opponent_team_id}:
        raise PitcherStrikeoutValueError("V7 odds and prediction teams do not match")
    pitchers = _mapping(summary.get("pitchers"), "pitchers")
    pitcher_key = provider_pitcher_key(prediction.pitcher_name)
    pitcher = pitchers.get(pitcher_key)
    if not isinstance(pitcher, Mapping):
        return PitcherStrikeoutGameValueV1(
            source_game_id=prediction.source_game_id,
            provider_event_id=odds.provider_event_id,
            source_normalized_odds_checksum=odds.checksum,
            source_prediction_checksum=prediction.checksum,
            outcomes=(),
            warnings=("pitcher_strikeout_provider_pitcher_unavailable",),
        )
    if pitcher.get("binding_state") != "provider_description_only":
        raise PitcherStrikeoutValueError("V7 normalized pitcher binding state is invalid")

    outcomes: list[PitcherStrikeoutOutcomeValueV1] = []
    warnings: list[str] = []
    for market_key, line in _all_lines(pitcher):
        selected_line = _number(line.get("line"), "line")
        projection = prediction.project(selected_line)
        line_outcomes = _mapping(line.get("outcomes"), "line outcomes")
        for side, outcome_key in (("over", "Over"), ("under", "Under")):
            outcome = line_outcomes.get(outcome_key)
            if not isinstance(outcome, Mapping) or outcome.get("best_price") is None:
                warnings.append(f"pitcher_strikeout_price_unavailable:{market_key}:{side}:{selected_line:g}")
                continue
            outcomes.append(
                _value_row(
                    prediction=prediction,
                    provider_event_id=odds.provider_event_id,
                    pitcher_key=pitcher_key,
                    market_key=market_key,
                    line=line,
                    outcome=outcome,
                    side=side,
                    projection=projection,
                    normalized_checksum=odds.checksum,
                )
            )

    if not outcomes and not warnings:
        warnings.append("pitcher_strikeout_market_unavailable")
    return PitcherStrikeoutGameValueV1(
        source_game_id=prediction.source_game_id,
        provider_event_id=odds.provider_event_id,
        source_normalized_odds_checksum=odds.checksum,
        source_prediction_checksum=prediction.checksum,
        outcomes=tuple(outcomes),
        warnings=tuple(warnings),
    )
