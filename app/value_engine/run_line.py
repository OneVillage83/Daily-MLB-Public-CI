from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.contracts import OddsAvailability, OddsSnapshotV1, thaw_mapping
from app.predictions.run_line import RunLinePredictionV1, RunLineProjectionV1
from app.value_engine.pricing import ValueMathError, calculate_value_math

RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_VALUE_OUTCOME_V1"
RUN_LINE_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_VALUE_GAME_V1"
RUN_LINE_VALUE_CALCULATION_VERSION = "DSE_MLB_RUN_LINE_VALUE_V1"
MAX_UNRESOLVED_VALUE_TAIL = 1e-6

_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}


class RunLineValueError(ValueError):
    """Raised when V2B run-line value evidence is invalid."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RunLineValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RunLineValueError(f"{name} must be lowercase SHA-256")
    return text


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RunLineValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RunLineValueError(f"{name} must be finite numeric")
    return result


def _optional_finite(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _probability(value: object, name: str) -> float:
    result = _finite(value, name)
    if not 0.0 <= result <= 1.0:
        raise RunLineValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    if value is None:
        return None
    return _probability(value, name)


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunLineValueError(f"{name} must be timezone-aware")
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
        raise RunLineValueError(f"{name} must be a mapping")
    return value


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _book_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


@dataclass(frozen=True, slots=True)
class RunLineOutcomeValueV1:
    source_game_id: str
    outcome_team_id: str
    side: str
    line_key: str
    home_spread: float
    side_spread: float
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
    resolved_model_cover_probability: float
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
    calculation_version: str = RUN_LINE_VALUE_CALCULATION_VERSION
    contract_version: str = RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "outcome_team_id", _required_text(self.outcome_team_id, "outcome_team_id"))
        if self.side not in {"home", "away"}:
            raise RunLineValueError("run-line value side must be home or away")
        object.__setattr__(self, "line_key", _required_text(self.line_key, "line_key"))
        home_spread = _finite(self.home_spread, "home_spread")
        side_spread = _finite(self.side_spread, "side_spread")
        expected_side_spread = home_spread if self.side == "home" else -home_spread
        if not math.isclose(side_spread, expected_side_spread, abs_tol=1e-12):
            raise RunLineValueError("side spread does not reconcile to canonical home spread")
        object.__setattr__(self, "home_spread", home_spread)
        object.__setattr__(self, "side_spread", side_spread)
        price = _finite(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise RunLineValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(sorted({_required_text(item, "best_price_book") for item in self.best_price_books}))
        if not books:
            raise RunLineValueError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise RunLineValueError("bookmaker_count must be positive")
        object.__setattr__(
            self,
            "consensus_confidence",
            _required_text(self.consensus_confidence, "consensus_confidence"),
        )
        object.__setattr__(self, "market_retrieved_at", _utc(self.market_retrieved_at, "market_retrieved_at"))
        timestamps = tuple(sorted({_utc(item, "effective_provider_timestamp") for item in self.effective_provider_timestamps}))
        object.__setattr__(self, "effective_provider_timestamps", timestamps)
        if self.freshness_status not in _FRESHNESS_RANK:
            raise RunLineValueError("freshness_status is invalid")
        if self.median_market_hold is not None:
            object.__setattr__(self, "median_market_hold", _finite(self.median_market_hold, "median_market_hold"))
        for name in ("market_evidence_checksum", "source_prediction_checksum", "source_projection_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in (
            "resolved_model_cover_probability",
            "resolved_model_loss_probability",
            "resolved_model_push_probability",
            "unresolved_probability",
            "conditional_model_probability",
            "raw_implied_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.resolved_model_cover_probability
            + self.resolved_model_loss_probability
            + self.resolved_model_push_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise RunLineValueError("resolved run-line model probabilities must sum to one")
        decisive = self.resolved_model_cover_probability + self.resolved_model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.resolved_model_cover_probability / decisive,
            abs_tol=1e-10,
        ):
            raise RunLineValueError("conditional run-line model probability mismatch")
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
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if self.no_vig_probability_edge is not None:
            object.__setattr__(
                self,
                "no_vig_probability_edge",
                _finite(self.no_vig_probability_edge, "no_vig_probability_edge"),
            )
        if not math.isclose(self.expected_roi_percent, self.expected_value_per_unit * 100.0, abs_tol=1e-10):
            raise RunLineValueError("expected ROI must equal EV multiplied by 100")
        reasons = tuple(sorted({_required_text(reason, "ineligibility_reason") for reason in self.ineligibility_reasons}))
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.recommendation_gate_input_eligible != (not reasons):
            raise RunLineValueError("run-line gate eligibility must agree with ineligibility reasons")
        if not self.prediction_recommendation_eligible and "prediction_not_recommendation_eligible" not in reasons:
            raise RunLineValueError("reference/shadow prediction must remain blocked from Recommendation Gate")
        if self.calculation_version != RUN_LINE_VALUE_CALCULATION_VERSION:
            raise RunLineValueError("unsupported run-line value calculation version")
        if self.contract_version != RUN_LINE_VALUE_OUTCOME_CONTRACT_VERSION:
            raise RunLineValueError("unsupported run-line outcome value contract")

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
            "home_spread": self.home_spread,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "line_key": self.line_key,
            "market_evidence_checksum": self.market_evidence_checksum,
            "market_retrieved_at": self.market_retrieved_at.isoformat(),
            "median_market_hold": self.median_market_hold,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "outcome_team_id": self.outcome_team_id,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": self.recommendation_gate_input_eligible,
            "resolved_model_cover_probability": self.resolved_model_cover_probability,
            "resolved_model_loss_probability": self.resolved_model_loss_probability,
            "resolved_model_push_probability": self.resolved_model_push_probability,
            "side": self.side,
            "side_spread": self.side_spread,
            "source_game_id": self.source_game_id,
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
class RunLineGameValueV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    source_prediction_checksum: str
    odds_summary_checksum: str | None
    odds_retrieved_at: datetime | None
    values: tuple[RunLineOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = RUN_LINE_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "away_team_id", _required_text(self.away_team_id, "away_team_id"))
        object.__setattr__(self, "home_team_id", _required_text(self.home_team_id, "home_team_id"))
        if self.away_team_id == self.home_team_id:
            raise RunLineValueError("run-line game teams must differ")
        object.__setattr__(self, "source_prediction_checksum", _sha(self.source_prediction_checksum, "source_prediction_checksum"))
        if self.odds_summary_checksum is not None:
            object.__setattr__(self, "odds_summary_checksum", _sha(self.odds_summary_checksum, "odds_summary_checksum"))
        if self.odds_retrieved_at is not None:
            object.__setattr__(self, "odds_retrieved_at", _utc(self.odds_retrieved_at, "odds_retrieved_at"))
        values = tuple(self.values)
        if len({value.checksum for value in values}) != len(values):
            raise RunLineValueError("run-line game value contains duplicate rows")
        if any(
            value.source_game_id != self.source_game_id
            or value.source_prediction_checksum != self.source_prediction_checksum
            or value.outcome_team_id not in {self.home_team_id, self.away_team_id}
            for value in values
        ):
            raise RunLineValueError("run-line game value lineage mismatch")
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "warnings", tuple(sorted({_required_text(item, "warning") for item in self.warnings})))
        if not values and not self.warnings:
            raise RunLineValueError("empty run-line value requires an explicit warning")
        if self.contract_version != RUN_LINE_VALUE_GAME_CONTRACT_VERSION:
            raise RunLineValueError("unsupported run-line game value contract")

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


def _resolved_projection(projection: RunLineProjectionV1) -> tuple[float, float, float]:
    resolved_mass = 1.0 - projection.unresolved_probability
    if resolved_mass <= 0.0:
        raise RunLineValueError("run-line projection contains no resolved probability mass")
    home = projection.home_cover_probability / resolved_mass
    away = projection.away_cover_probability / resolved_mass
    push = projection.push_probability / resolved_mass
    if not math.isclose(home + away + push, 1.0, abs_tol=1e-9):
        raise RunLineValueError("resolved run-line probabilities do not sum to one")
    return home, away, push


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
        normalized_price = _optional_finite(raw_offer.get("normalized_price"))
        if (
            bookmaker not in best_books
            or normalized_price is None
            or not math.isclose(normalized_price, best_price, abs_tol=1e-12)
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
    prediction: RunLinePredictionV1,
    projection: RunLineProjectionV1,
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
    if projection.unresolved_probability > MAX_UNRESOLVED_VALUE_TAIL:
        reasons.append("unresolved_tail_exceeds_value_tolerance")
    return tuple(sorted(set(reasons)))


def _outcome_value(
    prediction: RunLinePredictionV1,
    *,
    odds_summary_checksum: str,
    market_retrieved_at: datetime,
    line_key: str,
    line: Mapping[str, Any],
    home_spread: float,
    projection: RunLineProjectionV1,
    side: str,
    outcome_team_id: str,
    outcome: Mapping[str, Any],
) -> RunLineOutcomeValueV1 | None:
    price = _optional_finite(outcome.get("best_price"))
    raw_implied = _optional_finite(outcome.get("best_price_implied_probability"))
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
    home_cover, away_cover, push = _resolved_projection(projection)
    if side == "home":
        win, loss, side_spread = home_cover, away_cover, home_spread
    elif side == "away":
        win, loss, side_spread = away_cover, home_cover, -home_spread
    else:
        raise RunLineValueError("run-line side must be home or away")
    no_vig = _optional_finite(outcome.get("no_vig_probability"))
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
        raise RunLineValueError(f"invalid run-line value math for {line_key}/{side}") from exc
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
                "home_spread": home_spread,
                "line_key": line_key,
                "median_market_hold": line.get("median_market_hold"),
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
                "team_id": outcome_team_id,
            },
        }
    )
    return RunLineOutcomeValueV1(
        source_game_id=prediction.source_game_id,
        outcome_team_id=outcome_team_id,
        side=side,
        line_key=line_key,
        home_spread=home_spread,
        side_spread=side_spread,
        american_price=value_math.american_price,
        best_price_books=books,
        bookmaker_count=_book_count(outcome.get("bookmaker_count")),
        consensus_confidence=str(outcome.get("consensus_confidence") or "insufficient"),
        market_retrieved_at=market_retrieved_at,
        effective_provider_timestamps=timestamps,
        freshness_status=freshness,
        complete_two_way_market=complete_two_way,
        median_market_hold=_optional_finite(line.get("median_market_hold")),
        market_evidence_checksum=market_evidence_checksum,
        resolved_model_cover_probability=value_math.model_win_probability,
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


def evaluate_run_line_value(
    prediction: RunLinePredictionV1,
    odds: OddsSnapshotV1,
) -> RunLineGameValueV1:
    """Evaluate every retained canonical spread line against V2A prediction evidence."""

    if odds.availability is OddsAvailability.UNAVAILABLE:
        return RunLineGameValueV1(
            source_game_id=prediction.source_game_id,
            away_team_id=prediction.away_team_id,
            home_team_id=prediction.home_team_id,
            source_prediction_checksum=prediction.checksum,
            odds_summary_checksum=None,
            odds_retrieved_at=None,
            values=(),
            warnings=("odds_unavailable",),
        )
    if odds.summary is None or odds.summary_checksum is None or odds.retrieved_at is None:
        raise RunLineValueError("available odds lack canonical summary lineage")
    summary = thaw_mapping(odds.summary)
    markets = _mapping(summary.get("markets"), "odds-summary markets")
    spreads_raw = markets.get("spreads")
    if not isinstance(spreads_raw, Mapping):
        return RunLineGameValueV1(
            source_game_id=prediction.source_game_id,
            away_team_id=prediction.away_team_id,
            home_team_id=prediction.home_team_id,
            source_prediction_checksum=prediction.checksum,
            odds_summary_checksum=odds.summary_checksum,
            odds_retrieved_at=odds.retrieved_at,
            values=(),
            warnings=("spreads_unavailable",),
        )
    spreads = _mapping(spreads_raw, "spreads market")
    lines = _mapping(spreads.get("lines"), "spreads lines")
    values: list[RunLineOutcomeValueV1] = []
    warnings: list[str] = []
    for raw_line_key in sorted(lines):
        line_key = str(raw_line_key)
        line = _mapping(lines[raw_line_key], f"spread line {line_key}")
        if line.get("valid_line") is not True:
            continue
        home_spread = _optional_finite(line.get("line"))
        if home_spread is None:
            continue
        projection = prediction.project(home_spread)
        outcomes = _mapping(line.get("outcomes"), f"spread outcomes {line_key}")
        for side, team_id in (("home", prediction.home_team_id), ("away", prediction.away_team_id)):
            raw_outcome = outcomes.get(team_id)
            if not isinstance(raw_outcome, Mapping):
                warnings.append(f"missing_{side}_outcome:{line_key}")
                continue
            outcome = _mapping(raw_outcome, f"spread outcome {line_key}/{team_id}")
            value = _outcome_value(
                prediction,
                odds_summary_checksum=odds.summary_checksum,
                market_retrieved_at=odds.retrieved_at,
                line_key=line_key,
                line=line,
                home_spread=home_spread,
                projection=projection,
                side=side,
                outcome_team_id=team_id,
                outcome=outcome,
            )
            if value is not None:
                values.append(value)
    if not values:
        warnings.append("no_evaluable_run_line_market")
    values.sort(key=lambda value: (value.home_spread, 0 if value.side == "home" else 1, value.line_key))
    return RunLineGameValueV1(
        source_game_id=prediction.source_game_id,
        away_team_id=prediction.away_team_id,
        home_team_id=prediction.home_team_id,
        source_prediction_checksum=prediction.checksum,
        odds_summary_checksum=odds.summary_checksum,
        odds_retrieved_at=odds.retrieved_at,
        values=tuple(values),
        warnings=tuple(warnings),
    )
