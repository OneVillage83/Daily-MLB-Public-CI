from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.contracts import OddsAvailability, OddsSnapshotV1, thaw_mapping
from app.predictions.game_total import GameTotalPredictionV1, GameTotalProjectionV1
from app.value_engine.pricing import ValueMathError, calculate_value_math

GAME_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_VALUE_OUTCOME_V1"
GAME_TOTAL_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_VALUE_GAME_V1"
GAME_TOTAL_VALUE_CALCULATION_VERSION = "DSE_MLB_GAME_TOTAL_VALUE_V1"
MAX_UNRESOLVED_TOTAL_VALUE_TAIL = 1e-6

_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}


class GameTotalValueError(ValueError):
    """Raised when V3B game-total Value evidence is invalid."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GameTotalValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise GameTotalValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GameTotalValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GameTotalValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise GameTotalValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise GameTotalValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GameTotalValueError(f"{name} must be a mapping")
    return value


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _book_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


@dataclass(frozen=True, slots=True)
class GameTotalOutcomeValueV1:
    source_game_id: str
    side: str
    line_key: str
    total_line: float
    american_price: float
    best_price_books: tuple[str, ...]
    bookmaker_count: int
    consensus_confidence: str
    market_retrieved_at: datetime
    effective_provider_timestamps: tuple[datetime, ...]
    freshness_status: str
    complete_two_way_market: bool
    median_market_hold: float | None
    market_evidence_checksum: str
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
    source_prediction_checksum: str
    source_projection_checksum: str
    prediction_recommendation_eligible: bool
    recommendation_gate_input_eligible: bool
    ineligibility_reasons: tuple[str, ...]
    calculation_version: str = GAME_TOTAL_VALUE_CALCULATION_VERSION
    contract_version: str = GAME_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if self.side not in {"over", "under"}:
            raise GameTotalValueError("game-total side must be over or under")
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        total_line = _number(self.total_line, "total_line")
        if total_line < 0.0:
            raise GameTotalValueError("total_line must be nonnegative")
        object.__setattr__(self, "total_line", total_line)
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise GameTotalValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_text(book, "best_price_book") for book in self.best_price_books}))
        if not books:
            raise GameTotalValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise GameTotalValueError("bookmaker_count must be positive")
        object.__setattr__(self, "consensus_confidence", _text(self.consensus_confidence, "consensus_confidence"))
        object.__setattr__(self, "market_retrieved_at", _utc(self.market_retrieved_at, "market_retrieved_at"))
        object.__setattr__(
            self,
            "effective_provider_timestamps",
            tuple(sorted({_utc(item, "effective_provider_timestamp") for item in self.effective_provider_timestamps})),
        )
        if self.freshness_status not in _FRESHNESS_RANK:
            raise GameTotalValueError("freshness_status is invalid")
        if self.median_market_hold is not None:
            object.__setattr__(self, "median_market_hold", _number(self.median_market_hold, "median_market_hold"))
        for name in ("market_evidence_checksum", "source_prediction_checksum", "source_projection_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
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
            raise GameTotalValueError("resolved game-total probabilities must sum to one")
        decisive = self.resolved_model_win_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise GameTotalValueError("conditional game-total model probability mismatch")
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
        if self.no_vig_probability_edge is not None:
            object.__setattr__(self, "no_vig_probability_edge", _number(self.no_vig_probability_edge, "no_vig_probability_edge"))
        if not math.isclose(self.expected_roi_percent, self.expected_value_per_unit * 100.0, abs_tol=1e-10):
            raise GameTotalValueError("expected ROI must equal EV multiplied by 100")
        reasons = tuple(sorted({_text(reason, "ineligibility_reason") for reason in self.ineligibility_reasons}))
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.recommendation_gate_input_eligible != (not reasons):
            raise GameTotalValueError("game-total Gate eligibility must agree with reasons")
        if not self.prediction_recommendation_eligible and "prediction_not_recommendation_eligible" not in reasons:
            raise GameTotalValueError("reference game-total prediction must remain blocked from Gate")
        if self.calculation_version != GAME_TOTAL_VALUE_CALCULATION_VERSION:
            raise GameTotalValueError("unsupported game-total Value calculation version")
        if self.contract_version != GAME_TOTAL_VALUE_OUTCOME_CONTRACT_VERSION:
            raise GameTotalValueError("unsupported game-total Value outcome contract")

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
            "effective_provider_timestamps": [item.isoformat() for item in self.effective_provider_timestamps],
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "fair_american_odds": self.fair_american_odds,
            "fair_decimal_odds": self.fair_decimal_odds,
            "freshness_status": self.freshness_status,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "line_key": self.line_key,
            "market_evidence_checksum": self.market_evidence_checksum,
            "market_retrieved_at": self.market_retrieved_at.isoformat(),
            "median_market_hold": self.median_market_hold,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": self.recommendation_gate_input_eligible,
            "resolved_model_loss_probability": self.resolved_model_loss_probability,
            "resolved_model_push_probability": self.resolved_model_push_probability,
            "resolved_model_win_probability": self.resolved_model_win_probability,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_projection_checksum": self.source_projection_checksum,
            "total_line": self.total_line,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GameTotalGameValueV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    source_prediction_checksum: str
    odds_summary_checksum: str | None
    odds_retrieved_at: datetime | None
    values: tuple[GameTotalOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = GAME_TOTAL_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "away_team_id", _text(self.away_team_id, "away_team_id"))
        object.__setattr__(self, "home_team_id", _text(self.home_team_id, "home_team_id"))
        if self.away_team_id == self.home_team_id:
            raise GameTotalValueError("game-total teams must differ")
        object.__setattr__(self, "source_prediction_checksum", _sha(self.source_prediction_checksum, "source_prediction_checksum"))
        if self.odds_summary_checksum is not None:
            object.__setattr__(self, "odds_summary_checksum", _sha(self.odds_summary_checksum, "odds_summary_checksum"))
        if self.odds_retrieved_at is not None:
            object.__setattr__(self, "odds_retrieved_at", _utc(self.odds_retrieved_at, "odds_retrieved_at"))
        values = tuple(self.values)
        if len({value.checksum for value in values}) != len(values):
            raise GameTotalValueError("game-total game Value contains duplicate rows")
        if any(
            value.source_game_id != self.source_game_id
            or value.source_prediction_checksum != self.source_prediction_checksum
            for value in values
        ):
            raise GameTotalValueError("game-total game Value lineage mismatch")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if not values and not self.warnings:
            raise GameTotalValueError("empty game-total Value requires an explicit warning")
        if self.contract_version != GAME_TOTAL_VALUE_GAME_CONTRACT_VERSION:
            raise GameTotalValueError("unsupported game-total game Value contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
            "odds_retrieved_at": None if self.odds_retrieved_at is None else self.odds_retrieved_at.isoformat(),
            "odds_summary_checksum": self.odds_summary_checksum,
            "source_game_id": self.source_game_id,
            "source_prediction_checksum": self.source_prediction_checksum,
            "values": [value.as_dict() for value in self.values],
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _resolved_projection(projection: GameTotalProjectionV1) -> tuple[float, float, float]:
    resolved_mass = 1.0 - projection.unresolved_probability
    if resolved_mass <= 0.0:
        raise GameTotalValueError("game-total projection contains no resolved mass")
    over = projection.over_probability / resolved_mass
    under = projection.under_probability / resolved_mass
    push = projection.push_probability / resolved_mass
    if not math.isclose(over + under + push, 1.0, abs_tol=1e-9):
        raise GameTotalValueError("resolved game-total probabilities do not sum to one")
    return over, under, push


def _selected_best_offer_evidence(
    outcome: Mapping[str, Any],
    *,
    best_price: float,
    best_books: tuple[str, ...],
) -> tuple[tuple[datetime, ...], str, list[dict[str, object]]]:
    timestamps: set[datetime] = set()
    statuses: list[str] = []
    selected: list[dict[str, object]] = []
    for raw_offer in _sequence(outcome.get("offers")):
        if not isinstance(raw_offer, Mapping):
            continue
        bookmaker = str(raw_offer.get("bookmaker_key") or "")
        price = _optional_number(raw_offer.get("normalized_price"))
        if (
            bookmaker not in best_books
            or price is None
            or not math.isclose(price, best_price, abs_tol=1e-12)
            or raw_offer.get("calculation_eligible", True) is not True
        ):
            continue
        status = str(raw_offer.get("freshness_status") or "unknown")
        if status not in _FRESHNESS_RANK:
            status = "unknown"
        timestamp = _parse_utc(raw_offer.get("effective_provider_timestamp"))
        if timestamp is not None:
            timestamps.add(timestamp)
        statuses.append(status)
        selected.append(
            {
                "bookmaker_key": bookmaker,
                "effective_provider_timestamp": None if timestamp is None else timestamp.isoformat(),
                "freshness_status": status,
                "price": best_price,
            }
        )
    freshness = max(statuses, key=lambda status: _FRESHNESS_RANK[status]) if statuses else "unknown"
    return tuple(sorted(timestamps)), freshness, selected


def _ineligibility_reasons(
    prediction: GameTotalPredictionV1,
    projection: GameTotalProjectionV1,
    *,
    complete_two_way_market: bool,
    no_vig_probability: float | None,
    freshness_status: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not prediction.recommendation_eligible:
        reasons.append("prediction_not_recommendation_eligible")
    if not complete_two_way_market:
        reasons.append("incomplete_two_way_market")
    if no_vig_probability is None:
        reasons.append("missing_no_vig_probability")
    if freshness_status == "stale":
        reasons.append("market_stale")
    elif freshness_status == "unknown":
        reasons.append("market_freshness_unknown")
    if projection.unresolved_probability > MAX_UNRESOLVED_TOTAL_VALUE_TAIL:
        reasons.append("unresolved_tail_exceeds_value_tolerance")
    return tuple(sorted(set(reasons)))


def _outcome_value(
    prediction: GameTotalPredictionV1,
    *,
    odds_summary_checksum: str,
    market_retrieved_at: datetime,
    line_key: str,
    line: Mapping[str, Any],
    total_line: float,
    projection: GameTotalProjectionV1,
    side: str,
    outcome: Mapping[str, Any],
) -> GameTotalOutcomeValueV1 | None:
    price = _optional_number(outcome.get("best_price"))
    raw_implied = _optional_number(outcome.get("best_price_implied_probability"))
    if price is None or raw_implied is None or abs(price) < 100.0:
        return None
    books = tuple(
        sorted(
            {
                str(book)
                for book in _sequence(outcome.get("best_price_books"))
                if isinstance(book, str) and book.strip()
            }
        )
    )
    if not books:
        return None
    timestamps, freshness, selected_offers = _selected_best_offer_evidence(
        outcome,
        best_price=price,
        best_books=books,
    )
    over, under, push = _resolved_projection(projection)
    win, loss = (over, under) if side == "over" else (under, over)
    no_vig = _optional_number(outcome.get("no_vig_probability"))
    try:
        value_math = calculate_value_math(
            win_probability=win,
            loss_probability=loss,
            push_probability=push,
            price=price,
            retained_raw_implied_probability=raw_implied,
            no_vig_probability=no_vig,
        )
    except ValueMathError as exc:
        raise GameTotalValueError(f"invalid game-total Value math for {line_key}/{side}") from exc
    complete_two_way = line.get("complete_two_way_market") is True
    reasons = _ineligibility_reasons(
        prediction,
        projection,
        complete_two_way_market=complete_two_way,
        no_vig_probability=value_math.no_vig_probability,
        freshness_status=freshness,
    )
    market_evidence_checksum = canonical_sha256(
        {
            "line": {
                "complete_two_way_market": complete_two_way,
                "line_key": line_key,
                "median_market_hold": line.get("median_market_hold"),
                "total_line": total_line,
            },
            "odds_summary_checksum": odds_summary_checksum,
            "outcome": {
                "best_price": price,
                "best_price_books": list(books),
                "bookmaker_count": outcome.get("bookmaker_count"),
                "consensus_confidence": outcome.get("consensus_confidence"),
                "no_vig_probability": value_math.no_vig_probability,
                "selected_best_offers": selected_offers,
                "side": side,
            },
        }
    )
    return GameTotalOutcomeValueV1(
        source_game_id=prediction.source_game_id,
        side=side,
        line_key=line_key,
        total_line=total_line,
        american_price=value_math.american_price,
        best_price_books=books,
        bookmaker_count=_book_count(outcome.get("bookmaker_count")),
        consensus_confidence=str(outcome.get("consensus_confidence") or "insufficient"),
        market_retrieved_at=market_retrieved_at,
        effective_provider_timestamps=timestamps,
        freshness_status=freshness,
        complete_two_way_market=complete_two_way,
        median_market_hold=_optional_number(line.get("median_market_hold")),
        market_evidence_checksum=market_evidence_checksum,
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
        source_prediction_checksum=prediction.checksum,
        source_projection_checksum=projection.checksum,
        prediction_recommendation_eligible=prediction.recommendation_eligible,
        recommendation_gate_input_eligible=not reasons,
        ineligibility_reasons=reasons,
    )


def evaluate_game_total_value(
    prediction: GameTotalPredictionV1,
    odds: OddsSnapshotV1,
) -> GameTotalGameValueV1:
    if odds.availability is OddsAvailability.UNAVAILABLE:
        return GameTotalGameValueV1(
            prediction.source_game_id,
            prediction.away_team_id,
            prediction.home_team_id,
            prediction.checksum,
            None,
            None,
            (),
            ("odds_unavailable",),
        )
    if odds.summary is None or odds.summary_checksum is None or odds.retrieved_at is None:
        raise GameTotalValueError("available odds lack canonical summary lineage")
    summary = thaw_mapping(odds.summary)
    markets = _mapping(summary.get("markets"), "odds-summary markets")
    totals_raw = markets.get("totals")
    if not isinstance(totals_raw, Mapping):
        return GameTotalGameValueV1(
            prediction.source_game_id,
            prediction.away_team_id,
            prediction.home_team_id,
            prediction.checksum,
            odds.summary_checksum,
            odds.retrieved_at,
            (),
            ("totals_unavailable",),
        )
    totals = _mapping(totals_raw, "totals market")
    lines = _mapping(totals.get("lines"), "totals lines")
    values: list[GameTotalOutcomeValueV1] = []
    warnings: list[str] = []
    for raw_line_key in sorted(lines):
        line_key = str(raw_line_key)
        line = _mapping(lines[raw_line_key], f"total line {line_key}")
        if line.get("valid_line") is not True:
            continue
        total_line = _optional_number(line.get("line"))
        if total_line is None or total_line < 0.0:
            continue
        projection = prediction.project(total_line)
        outcomes = _mapping(line.get("outcomes"), f"total outcomes {line_key}")
        for side, provider_side in (("over", "Over"), ("under", "Under")):
            raw_outcome = outcomes.get(provider_side)
            if not isinstance(raw_outcome, Mapping):
                warnings.append(f"missing_{side}_outcome:{line_key}")
                continue
            value = _outcome_value(
                prediction,
                odds_summary_checksum=odds.summary_checksum,
                market_retrieved_at=odds.retrieved_at,
                line_key=line_key,
                line=line,
                total_line=total_line,
                projection=projection,
                side=side,
                outcome=_mapping(raw_outcome, f"total outcome {line_key}/{provider_side}"),
            )
            if value is not None:
                values.append(value)
    if not values:
        warnings.append("no_evaluable_game_total_market")
    values.sort(key=lambda value: (value.total_line, 0 if value.side == "over" else 1, value.line_key))
    return GameTotalGameValueV1(
        source_game_id=prediction.source_game_id,
        away_team_id=prediction.away_team_id,
        home_team_id=prediction.home_team_id,
        source_prediction_checksum=prediction.checksum,
        odds_summary_checksum=odds.summary_checksum,
        odds_retrieved_at=odds.retrieved_at,
        values=tuple(values),
        warnings=tuple(warnings),
    )
