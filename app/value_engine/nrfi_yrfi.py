from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.odds_weather.first_inning import (
    FIRST_INNING_TOTALS_MARKET,
    FirstInningNormalizedOddsV1,
)
from app.predictions.first_inning import NrfiYrfiPredictionV1
from app.value_engine.pricing import calculate_value_math

NRFI_YRFI_VALUE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_VALUE_OUTCOME_V1"
NRFI_YRFI_VALUE_GAME_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_VALUE_GAME_V1"
NRFI_YRFI_VALUE_CALCULATION_VERSION = "DSE_MLB_NRFI_YRFI_VALUE_V1"
NRFI_YRFI_REFERENCE_REASON = "prediction_not_recommendation_eligible"
NRFI_YRFI_BINDING_PENDING_REASON = "first_inning_production_event_binding_pending"
NRFI_YRFI_TOTAL_LINE = 0.5


class NrfiYrfiValueError(ValueError):
    """Raised when V5B NRFI/YRFI Value evidence violates its reference contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NrfiYrfiValueError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise NrfiYrfiValueError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NrfiYrfiValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise NrfiYrfiValueError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise NrfiYrfiValueError(f"{name} must be between zero and one")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise NrfiYrfiValueError(f"{name} must be a mapping")
    return value


def _books(outcome: Mapping[str, Any]) -> tuple[str, ...]:
    raw = outcome.get("best_price_books")
    if not isinstance(raw, list):
        raise NrfiYrfiValueError("normalized outcome lacks best_price_books")
    books = tuple(sorted({_text(item, "best_price_book") for item in raw}))
    if not books:
        raise NrfiYrfiValueError("normalized outcome lacks a best-price bookmaker")
    return books


@dataclass(frozen=True, slots=True)
class NrfiYrfiOutcomeValueV1:
    source_game_id: str
    provider_event_id: str
    side: str
    sportsbook_outcome: str
    total_line: float
    american_price: float
    best_price_books: tuple[str, ...]
    bookmaker_count: int
    consensus_confidence: str
    complete_two_way_market: bool
    median_market_hold: float | None
    model_win_probability: float
    model_loss_probability: float
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
    calculation_version: str = NRFI_YRFI_VALUE_CALCULATION_VERSION
    contract_version: str = NRFI_YRFI_VALUE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        if self.side not in {"nrfi", "yrfi"}:
            raise NrfiYrfiValueError("NRFI/YRFI Value side must be nrfi or yrfi")
        expected_outcome = "Under" if self.side == "nrfi" else "Over"
        if self.sportsbook_outcome != expected_outcome:
            raise NrfiYrfiValueError("NRFI/YRFI sportsbook outcome mapping is invalid")
        line = _number(self.total_line, "total_line")
        if not math.isclose(line, NRFI_YRFI_TOTAL_LINE, abs_tol=1e-12):
            raise NrfiYrfiValueError("NRFI/YRFI Value requires the 0.5 first-inning total")
        object.__setattr__(self, "total_line", line)
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise NrfiYrfiValueError("American price magnitude must be at least 100")
        object.__setattr__(self, "american_price", price)
        object.__setattr__(self, "best_price_books", _books({"best_price_books": list(self.best_price_books)}))
        if isinstance(self.bookmaker_count, bool) or not isinstance(self.bookmaker_count, int) or self.bookmaker_count < 1:
            raise NrfiYrfiValueError("bookmaker_count must be positive")
        object.__setattr__(self, "consensus_confidence", _text(self.consensus_confidence, "consensus_confidence"))
        object.__setattr__(self, "median_market_hold", _optional_number(self.median_market_hold, "median_market_hold"))
        for name in (
            "model_win_probability",
            "model_loss_probability",
            "raw_implied_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(self.model_win_probability + self.model_loss_probability, 1.0, abs_tol=1e-10):
            raise NrfiYrfiValueError("NRFI/YRFI model probabilities must sum to one")
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
        object.__setattr__(self, "no_vig_probability_edge", _optional_number(self.no_vig_probability_edge, "no_vig_probability_edge"))
        if not math.isclose(self.expected_roi_percent, self.expected_value_per_unit * 100.0, abs_tol=1e-10):
            raise NrfiYrfiValueError("expected ROI must equal EV multiplied by 100")
        for name in (
            "market_evidence_checksum",
            "source_prediction_checksum",
            "source_projection_checksum",
            "source_normalized_odds_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        reasons = tuple(sorted({_text(item, "ineligibility_reason") for item in self.ineligibility_reasons}))
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.prediction_recommendation_eligible or self.recommendation_gate_input_eligible:
            raise NrfiYrfiValueError("V5B reference Value cannot be recommendation eligible")
        for required in (NRFI_YRFI_REFERENCE_REASON, NRFI_YRFI_BINDING_PENDING_REASON):
            if required not in reasons:
                raise NrfiYrfiValueError("NRFI/YRFI reference Value is missing required blockers")
        if self.calculation_version != NRFI_YRFI_VALUE_CALCULATION_VERSION:
            raise NrfiYrfiValueError("unsupported NRFI/YRFI Value calculation")
        if self.contract_version != NRFI_YRFI_VALUE_OUTCOME_CONTRACT_VERSION:
            raise NrfiYrfiValueError("unsupported NRFI/YRFI Value outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "best_price_books": list(self.best_price_books),
            "bookmaker_count": self.bookmaker_count,
            "calculation_version": self.calculation_version,
            "complete_two_way_market": self.complete_two_way_market,
            "consensus_confidence": self.consensus_confidence,
            "contract_version": self.contract_version,
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "fair_american_odds": self.fair_american_odds,
            "fair_decimal_odds": self.fair_decimal_odds,
            "ineligibility_reasons": list(self.ineligibility_reasons),
            "market_evidence_checksum": self.market_evidence_checksum,
            "median_market_hold": self.median_market_hold,
            "model_loss_probability": self.model_loss_probability,
            "model_win_probability": self.model_win_probability,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "prediction_recommendation_eligible": self.prediction_recommendation_eligible,
            "provider_event_id": self.provider_event_id,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": self.recommendation_gate_input_eligible,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_projection_checksum": self.source_projection_checksum,
            "sportsbook_outcome": self.sportsbook_outcome,
            "total_line": self.total_line,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class NrfiYrfiGameValueV1:
    source_game_id: str
    provider_event_id: str
    source_prediction_checksum: str
    source_normalized_odds_checksum: str
    outcomes: tuple[NrfiYrfiOutcomeValueV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = NRFI_YRFI_VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(self, "source_prediction_checksum", _sha(self.source_prediction_checksum, "source_prediction_checksum"))
        object.__setattr__(self, "source_normalized_odds_checksum", _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"))
        outcomes = tuple(self.outcomes)
        if len({item.side for item in outcomes}) != len(outcomes):
            raise NrfiYrfiValueError("NRFI/YRFI game Value contains duplicate sides")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.source_prediction_checksum != self.source_prediction_checksum
            or item.source_normalized_odds_checksum != self.source_normalized_odds_checksum
            for item in outcomes
        ):
            raise NrfiYrfiValueError("NRFI/YRFI game Value lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if self.contract_version != NRFI_YRFI_VALUE_GAME_CONTRACT_VERSION:
            raise NrfiYrfiValueError("unsupported NRFI/YRFI game Value contract")

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
            "source_prediction_checksum": self.source_prediction_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def evaluate_nrfi_yrfi_value(
    prediction: NrfiYrfiPredictionV1,
    odds: FirstInningNormalizedOddsV1,
) -> NrfiYrfiGameValueV1:
    if prediction.recommendation_eligible:
        raise NrfiYrfiValueError("V5B requires reference NRFI/YRFI prediction evidence")
    summary = odds.summary
    if summary.get("home_team_key") != prediction.home_team_id or summary.get("away_team_key") != prediction.away_team_id:
        raise NrfiYrfiValueError("first-inning odds and NRFI/YRFI prediction teams do not match")
    markets = _mapping(summary.get("markets"), "markets")
    market = markets.get(FIRST_INNING_TOTALS_MARKET)
    warnings: list[str] = []
    outcomes: list[NrfiYrfiOutcomeValueV1] = []
    if not isinstance(market, Mapping):
        warnings.append("first_inning_totals_market_unavailable")
    else:
        lines = _mapping(market.get("lines"), "first-inning total lines")
        line = lines.get("0.5")
        if not isinstance(line, Mapping) or not bool(line.get("valid_line")):
            warnings.append("nrfi_yrfi_0_5_line_unavailable")
        else:
            line_outcomes = _mapping(line.get("outcomes"), "0.5 line outcomes")
            projection_checksum = canonical_sha256(
                {
                    "nrfi_probability": prediction.nrfi_probability,
                    "total_line": NRFI_YRFI_TOTAL_LINE,
                    "yrfi_probability": prediction.yrfi_probability,
                }
            )
            market_checksum = canonical_sha256(dict(line))
            for side, sportsbook_name, win, loss in (
                ("nrfi", "Under", prediction.nrfi_probability, prediction.yrfi_probability),
                ("yrfi", "Over", prediction.yrfi_probability, prediction.nrfi_probability),
            ):
                outcome = line_outcomes.get(sportsbook_name)
                if not isinstance(outcome, Mapping) or outcome.get("best_price") is None:
                    warnings.append(f"nrfi_yrfi_price_unavailable:{side}")
                    continue
                price = _number(outcome.get("best_price"), "best_price")
                value_math = calculate_value_math(
                    win_probability=win,
                    loss_probability=loss,
                    push_probability=0.0,
                    price=price,
                    retained_raw_implied_probability=outcome.get("best_price_implied_probability"),
                    no_vig_probability=outcome.get("no_vig_probability"),
                )
                reasons = {NRFI_YRFI_REFERENCE_REASON, NRFI_YRFI_BINDING_PENDING_REASON}
                if not bool(line.get("complete_two_way_market")):
                    reasons.add("incomplete_two_way_market")
                if value_math.no_vig_probability is None:
                    reasons.add("no_vig_probability_unavailable")
                outcomes.append(
                    NrfiYrfiOutcomeValueV1(
                        source_game_id=prediction.source_game_id,
                        provider_event_id=odds.provider_event_id,
                        side=side,
                        sportsbook_outcome=sportsbook_name,
                        total_line=NRFI_YRFI_TOTAL_LINE,
                        american_price=price,
                        best_price_books=_books(outcome),
                        bookmaker_count=int(outcome.get("bookmaker_count") or 0),
                        consensus_confidence=_text(outcome.get("consensus_confidence"), "consensus_confidence"),
                        complete_two_way_market=bool(line.get("complete_two_way_market")),
                        median_market_hold=_optional_number(line.get("median_market_hold"), "median_market_hold"),
                        model_win_probability=value_math.model_win_probability,
                        model_loss_probability=value_math.model_loss_probability,
                        raw_implied_probability=value_math.raw_implied_probability,
                        no_vig_probability=value_math.no_vig_probability,
                        raw_probability_edge=value_math.raw_probability_edge,
                        no_vig_probability_edge=value_math.no_vig_probability_edge,
                        net_profit_per_unit=value_math.net_profit_per_unit,
                        expected_value_per_unit=value_math.expected_value_per_unit,
                        expected_roi_percent=value_math.expected_roi_percent,
                        fair_decimal_odds=value_math.fair_decimal_odds,
                        fair_american_odds=value_math.fair_american_odds,
                        market_evidence_checksum=market_checksum,
                        source_prediction_checksum=prediction.checksum,
                        source_projection_checksum=projection_checksum,
                        source_normalized_odds_checksum=odds.checksum,
                        prediction_recommendation_eligible=False,
                        recommendation_gate_input_eligible=False,
                        ineligibility_reasons=tuple(reasons),
                    )
                )
    return NrfiYrfiGameValueV1(
        source_game_id=prediction.source_game_id,
        provider_event_id=odds.provider_event_id,
        source_prediction_checksum=prediction.checksum,
        source_normalized_odds_checksum=odds.checksum,
        outcomes=tuple(outcomes),
        warnings=tuple(warnings),
    )
