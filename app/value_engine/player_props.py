from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.player_props import (
    PLAYER_PROP_MARKET_TO_STATISTIC,
    PlayerPropsNormalizedOddsV1,
    provider_player_key,
)
from app.predictions.player_props import PlayerPropPredictionV1, PlayerPropProjectionV1
from app.value_engine.pricing import calculate_value_math

PLAYER_PROP_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_VALUE_OUTCOME_V1"
PLAYER_PROP_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_VALUE_GAME_V1"
PLAYER_PROP_VALUE_CALCULATION_VERSION = "DSE_MLB_PLAYER_PROP_VALUE_V1"
PLAYER_PROP_REFERENCE_REASON = "prediction_not_recommendation_eligible"
PLAYER_PROP_EVENT_BINDING_PENDING_REASON = "player_prop_production_event_binding_pending"
PLAYER_PROP_PLAYER_BINDING_PENDING_REASON = "player_prop_production_player_binding_pending"
MAX_UNRESOLVED_PLAYER_PROP_VALUE_TAIL = 1e-6
_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}


class PlayerPropsValueError(ValueError):
    """Raised when V8B player-prop Value evidence violates its contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlayerPropsValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PlayerPropsValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlayerPropsValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlayerPropsValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise PlayerPropsValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlayerPropsValueError(f"{name} must be a mapping")
    return value


def _best_books(outcome: Mapping[str, Any]) -> tuple[str, ...]:
    raw = outcome.get("best_price_books")
    if not isinstance(raw, list):
        raise PlayerPropsValueError("normalized player-prop outcome lacks best_price_books")
    books = tuple(sorted({_text(item, "best_price_book") for item in raw}))
    if not books:
        raise PlayerPropsValueError("normalized player-prop outcome has no best-price bookmaker")
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


def _resolved_projection(
    projection: PlayerPropProjectionV1,
) -> tuple[float, float, float]:
    retained = 1.0 - projection.unresolved_probability
    if retained <= 0.0:
        raise PlayerPropsValueError("player-prop projection contains no resolved mass")
    over = projection.over_probability / retained
    under = projection.under_probability / retained
    push = projection.push_probability / retained
    if not math.isclose(over + under + push, 1.0, abs_tol=1e-9):
        raise PlayerPropsValueError("resolved player-prop probabilities do not sum to one")
    return over, under, push


@dataclass(frozen=True, slots=True)
class PlayerPropOutcomeValueV1:
    source_game_id: str
    provider_event_id: str
    player_id: str
    player_name: str
    provider_player_key: str
    team_id: str
    opponent_team_id: str
    role: str
    statistic: str
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
    calculation_version: str = PLAYER_PROP_VALUE_CALCULATION_VERSION
    contract_version: str = PLAYER_PROP_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "player_id",
            "player_name",
            "provider_player_key",
            "team_id",
            "opponent_team_id",
            "role",
            "statistic",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise PlayerPropsValueError("player-prop Value team and opponent must differ")
        if self.side not in {"over", "under"}:
            raise PlayerPropsValueError("player-prop Value side must be over or under")
        line = _number(self.line, "line")
        if line < 0.0:
            raise PlayerPropsValueError("player-prop Value line must be nonnegative")
        object.__setattr__(self, "line", line)
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise PlayerPropsValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_text(item, "best_price_book") for item in self.best_price_books}))
        if not books:
            raise PlayerPropsValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise PlayerPropsValueError("bookmaker_count must be positive")
        object.__setattr__(self, "consensus_confidence", _text(self.consensus_confidence, "consensus_confidence"))
        if self.freshness_status not in _FRESHNESS_RANK:
            raise PlayerPropsValueError("freshness_status is invalid")
        object.__setattr__(self, "median_market_hold", _optional_number(self.median_market_hold, "median_market_hold"))
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
            raise PlayerPropsValueError("resolved player-prop Value probabilities must sum to one")
        decisive = self.resolved_model_win_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise PlayerPropsValueError("conditional player-prop model probability mismatch")
        object.__setattr__(self, "no_vig_probability", _optional_probability(self.no_vig_probability, "no_vig_probability"))
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
        if not math.isclose(self.expected_roi_percent, self.expected_value_per_unit * 100.0, abs_tol=1e-10):
            raise PlayerPropsValueError("expected ROI must equal EV multiplied by 100")
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
            raise PlayerPropsValueError("V8B cannot consume recommendation-eligible player-prop prediction")
        if self.recommendation_gate_input_eligible:
            raise PlayerPropsValueError("V8B player-prop Value must remain blocked from Recommendation Gate")
        for required in (
            PLAYER_PROP_REFERENCE_REASON,
            PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
            PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
        ):
            if required not in reasons:
                raise PlayerPropsValueError("V8B player-prop Value is missing a required blocker")
        if self.calculation_version != PLAYER_PROP_VALUE_CALCULATION_VERSION:
            raise PlayerPropsValueError("unsupported V8B player-prop Value calculation")
        if self.contract_version != PLAYER_PROP_VALUE_OUTCOME_CONTRACT_VERSION:
            raise PlayerPropsValueError("unsupported V8B player-prop Value outcome contract")

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
            "player_id": self.player_id,
            "player_name": self.player_name,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "provider_event_id": self.provider_event_id,
            "provider_player_key": self.provider_player_key,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": self.recommendation_gate_input_eligible,
            "resolved_model_loss_probability": self.resolved_model_loss_probability,
            "resolved_model_push_probability": self.resolved_model_push_probability,
            "resolved_model_win_probability": self.resolved_model_win_probability,
            "role": self.role,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_projection_checksum": self.source_projection_checksum,
            "statistic": self.statistic,
            "team_id": self.team_id,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())


@dataclass(frozen=True, slots=True)
class PlayerPropsGameValueV1:
    source_game_id: str
    provider_event_id: str
    source_normalized_odds_checksum: str
    source_prediction_checksums: tuple[str, ...]
    outcomes: tuple[PlayerPropOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = PLAYER_PROP_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "source_normalized_odds_checksum",
            _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"),
        )
        checksums = tuple(_sha(item, "source_prediction_checksum") for item in self.source_prediction_checksums)
        if len(checksums) != len(set(checksums)):
            raise PlayerPropsValueError("V8B game Value contains duplicate prediction checksums")
        object.__setattr__(self, "source_prediction_checksums", checksums)
        outcomes = tuple(self.outcomes)
        if len({item.checksum for item in outcomes}) != len(outcomes):
            raise PlayerPropsValueError("V8B game Value contains duplicate outcomes")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.source_normalized_odds_checksum != self.source_normalized_odds_checksum
            or item.source_prediction_checksum not in checksums
            for item in outcomes
        ):
            raise PlayerPropsValueError("V8B game Value outcome lineage mismatch")
        if any(item.recommendation_gate_input_eligible for item in outcomes):
            raise PlayerPropsValueError("reference V8B Value cannot contain Gate-eligible rows")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if not outcomes and not self.warnings:
            raise PlayerPropsValueError("empty V8B game Value requires a warning")
        if self.contract_version != PLAYER_PROP_VALUE_GAME_CONTRACT_VERSION:
            raise PlayerPropsValueError("unsupported V8B player-prop game Value contract")

    @property
    def recommendation_gate_input_count(self) -> int:
        return sum(item.recommendation_gate_input_eligible for item in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "outcomes": [item.identity_dict() | {"checksum": item.checksum} for item in self.outcomes],
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


def _value_row(
    prediction: PlayerPropPredictionV1,
    odds: PlayerPropsNormalizedOddsV1,
    *,
    provider_key: str,
    market_key: str,
    line_key: str,
    line: Mapping[str, Any],
    side: str,
    provider_side: str,
) -> PlayerPropOutcomeValueV1 | None:
    outcomes = _mapping(line.get("outcomes"), "player-prop line outcomes")
    raw_outcome = outcomes.get(provider_side)
    if not isinstance(raw_outcome, Mapping) or raw_outcome.get("best_price") is None:
        return None
    outcome = _mapping(raw_outcome, "player-prop outcome")
    market_line = _number(line.get("line"), "player-prop line")
    projection = prediction.project(market_line)
    over, under, push = _resolved_projection(projection)
    win, loss = (over, under) if side == "over" else (under, over)
    best_price = _number(outcome.get("best_price"), "best_price")
    best_books = _best_books(outcome)
    value_math = calculate_value_math(
        win_probability=win,
        loss_probability=loss,
        push_probability=push,
        price=best_price,
        retained_raw_implied_probability=outcome.get("best_price_implied_probability"),
        no_vig_probability=outcome.get("no_vig_probability"),
    )
    freshness = _best_price_freshness(outcome, best_books)
    reasons = {
        PLAYER_PROP_REFERENCE_REASON,
        PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
        PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
    }
    if line.get("complete_two_way_market") is not True:
        reasons.add("incomplete_two_way_market")
    if value_math.no_vig_probability is None:
        reasons.add("no_vig_probability_unavailable")
    if freshness in {"stale", "unknown"}:
        reasons.add(f"market_freshness_{freshness}")
    if projection.unresolved_probability > MAX_UNRESOLVED_PLAYER_PROP_VALUE_TAIL:
        reasons.add("unresolved_tail_exceeds_value_tolerance")
    market_evidence_checksum = canonical_sha256(
        {
            "line": dict(line),
            "market_key": market_key,
            "outcome": dict(outcome),
            "provider_player_key": provider_key,
            "side": side,
        }
    )
    return PlayerPropOutcomeValueV1(
        source_game_id=prediction.source_game_id,
        provider_event_id=odds.provider_event_id,
        player_id=prediction.player_id,
        player_name=prediction.player_name,
        provider_player_key=provider_key,
        team_id=prediction.team_id,
        opponent_team_id=prediction.opponent_team_id,
        role=prediction.role.value,
        statistic=prediction.statistic.value,
        market_key=market_key,
        side=side,
        line_key=line_key,
        line=market_line,
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
        source_projection_checksum=projection.checksum,
        source_normalized_odds_checksum=odds.checksum,
        prediction_recommendation_eligible=prediction.recommendation_eligible,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=tuple(reasons),
    )


def evaluate_player_props_value(
    predictions: tuple[PlayerPropPredictionV1, ...],
    odds: PlayerPropsNormalizedOddsV1,
) -> PlayerPropsGameValueV1:
    if not predictions:
        raise PlayerPropsValueError("V8B requires at least one player-prop prediction")
    if len({item.source_game_id for item in predictions}) != 1:
        raise PlayerPropsValueError("V8B predictions disagree on source game")
    if any(item.recommendation_eligible for item in predictions):
        raise PlayerPropsValueError("V8B requires reference player-prop predictions")
    summary = odds.summary
    if summary.get("event_id") != odds.provider_event_id:
        raise PlayerPropsValueError("V8B normalized odds event identity mismatch")
    if summary.get("player_binding") != "provider_description_only":
        raise PlayerPropsValueError("V8B requires provisional provider-description player binding")
    event_teams = {summary.get("away_team_key"), summary.get("home_team_key")}
    if None in event_teams or len(event_teams) != 2:
        raise PlayerPropsValueError("V8B normalized odds lack canonical event teams")
    for prediction in predictions:
        if {prediction.team_id, prediction.opponent_team_id} != event_teams:
            raise PlayerPropsValueError("V8B prediction teams do not match provider event")
    players = _mapping(summary.get("players"), "normalized player-prop players")
    outcomes: list[PlayerPropOutcomeValueV1] = []
    warnings: list[str] = []
    for prediction in predictions:
        provider_key = provider_player_key(prediction.player_name)
        raw_player = players.get(provider_key)
        if not isinstance(raw_player, Mapping):
            warnings.append(f"provider_player_unmatched:{prediction.player_id}")
            continue
        player = _mapping(raw_player, "normalized player-prop player")
        markets = _mapping(player.get("markets"), "normalized player-prop markets")
        matching_market_count = 0
        for market_key in sorted(markets):
            if PLAYER_PROP_MARKET_TO_STATISTIC.get(market_key) is not prediction.statistic:
                continue
            matching_market_count += 1
            market = _mapping(markets[market_key], f"player-prop market {market_key}")
            lines = _mapping(market.get("lines"), f"player-prop lines {market_key}")
            for raw_line_key in sorted(lines):
                line_key = str(raw_line_key)
                line = _mapping(lines[raw_line_key], f"player-prop line {line_key}")
                if line.get("valid_line") is not True or line.get("line") is None:
                    continue
                for side, provider_side in (("over", "Over"), ("under", "Under")):
                    value = _value_row(
                        prediction,
                        odds,
                        provider_key=provider_key,
                        market_key=market_key,
                        line_key=line_key,
                        line=line,
                        side=side,
                        provider_side=provider_side,
                    )
                    if value is not None:
                        outcomes.append(value)
        if matching_market_count == 0:
            warnings.append(f"player_stat_market_unavailable:{prediction.player_id}:{prediction.statistic.value}")
    outcomes.sort(
        key=lambda item: (
            item.player_id,
            item.statistic,
            item.line,
            0 if item.side == "over" else 1,
            item.market_key,
        )
    )
    if not outcomes:
        warnings.append("no_evaluable_player_prop_market")
    return PlayerPropsGameValueV1(
        source_game_id=predictions[0].source_game_id,
        provider_event_id=odds.provider_event_id,
        source_normalized_odds_checksum=odds.checksum,
        source_prediction_checksums=tuple(item.checksum for item in predictions),
        outcomes=tuple(outcomes),
        warnings=tuple(warnings),
    )
