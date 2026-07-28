from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.data_quality.contracts import DataQualityDisposition
from app.daily_slate.contracts import (
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.predictions.contracts import ModelCalibrationStatus, ModelDeploymentStatus
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS

VALUE_ENGINE_CONTRACT_VERSION = "DSE_VALUE_ENGINE_V1"
VALUE_GAME_CONTRACT_VERSION = "DSE_VALUE_GAME_V1"
MARKET_VALUE_CONTRACT_VERSION = "DSE_MARKET_VALUE_V1"
VALUE_CALCULATION_VERSION = "DSE_VALUE_CALCULATION_V1"


class ValueEngineContractError(ValueError):
    """Raised when canonical Value Engine evidence is invalid."""


class ValueMarket(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"


class ValueSide(StrEnum):
    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class ValueCalculationState(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ValueIneligibilityReason(StrEnum):
    MODEL_NOT_RECOMMENDATION_ELIGIBLE = "model_not_recommendation_eligible"
    DATA_QUALITY_INSUFFICIENT = "data_quality_insufficient"
    INCOMPLETE_TWO_WAY_MARKET = "incomplete_two_way_market"
    MISSING_NO_VIG_PROBABILITY = "missing_no_vig_probability"
    MARKET_STALE = "market_stale"
    MARKET_FRESHNESS_UNKNOWN = "market_freshness_unknown"
    CALCULATION_INCOMPLETE = "calculation_incomplete"


class ValueGameWarning(StrEnum):
    ODDS_UNAVAILABLE = "odds_unavailable"
    NO_EVALUABLE_MARKETS = "no_evaluable_markets"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueEngineContractError(f"{name} must be non-empty trimmed text")
    return value


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueEngineContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueEngineContractError(f"{name} must be lowercase SHA-256")
    return text


def _optional_sha(value: object, name: str) -> str | None:
    return None if value is None else _sha(value, name)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueEngineContractError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueEngineContractError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise ValueEngineContractError(f"{name} must be between zero and one")
    return result


@dataclass(frozen=True, slots=True)
class MarketValueV1:
    market: ValueMarket
    line_key: str
    side: ValueSide
    market_line: float | None
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
    model_win_probability: float
    model_loss_probability: float
    model_push_probability: float
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
    approximation_tail_bound: float
    calculation_state: ValueCalculationState
    recommendation_gate_input_eligible: bool
    ineligibility_reasons: tuple[ValueIneligibilityReason, ...]
    calculation_version: str = VALUE_CALCULATION_VERSION
    contract_version: str = MARKET_VALUE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(
            self,
            "market_line",
            _optional_number(self.market_line, "market_line"),
        )
        if self.market is ValueMarket.MONEYLINE and self.market_line is not None:
            raise ValueEngineContractError("moneyline must not contain a point")
        if self.market is not ValueMarket.MONEYLINE and self.market_line is None:
            raise ValueEngineContractError("spread and total require a point")
        team_sides = {ValueSide.HOME, ValueSide.AWAY}
        total_sides = {ValueSide.OVER, ValueSide.UNDER}
        if self.market is ValueMarket.TOTAL and self.side not in total_sides:
            raise ValueEngineContractError("total side must be over or under")
        if self.market is not ValueMarket.TOTAL and self.side not in team_sides:
            raise ValueEngineContractError("team-market side must be home or away")
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise ValueEngineContractError("American price must have magnitude >= 100")
        object.__setattr__(self, "american_price", price)
        books = tuple(
            sorted({_text(item, "best_price_book") for item in self.best_price_books})
        )
        if not books:
            raise ValueEngineContractError("best_price_books cannot be empty")
        object.__setattr__(self, "best_price_books", books)
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count <= 0
        ):
            raise ValueEngineContractError("bookmaker_count must be positive")
        object.__setattr__(
            self,
            "consensus_confidence",
            _text(self.consensus_confidence, "consensus_confidence"),
        )
        object.__setattr__(
            self,
            "market_retrieved_at",
            _utc(self.market_retrieved_at, "market_retrieved_at"),
        )
        timestamps = tuple(
            sorted(
                {
                    _utc(item, "effective_provider_timestamp")
                    for item in self.effective_provider_timestamps
                }
            )
        )
        object.__setattr__(self, "effective_provider_timestamps", timestamps)
        object.__setattr__(
            self,
            "freshness_status",
            _text(self.freshness_status, "freshness_status"),
        )
        object.__setattr__(
            self,
            "median_market_hold",
            _optional_number(self.median_market_hold, "median_market_hold"),
        )
        object.__setattr__(
            self,
            "market_evidence_checksum",
            _sha(self.market_evidence_checksum, "market_evidence_checksum"),
        )
        for name in (
            "model_win_probability",
            "model_loss_probability",
            "model_push_probability",
            "conditional_model_probability",
            "raw_implied_probability",
            "approximation_tail_bound",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        probability_sum = (
            self.model_win_probability
            + self.model_loss_probability
            + self.model_push_probability
        )
        if not math.isclose(probability_sum, 1.0, abs_tol=1e-9):
            raise ValueEngineContractError("model win/loss/push must sum to one")
        decisive = self.model_win_probability + self.model_loss_probability
        if decisive <= 0.0 or not math.isclose(
            self.conditional_model_probability,
            self.model_win_probability / decisive,
            abs_tol=1e-10,
        ):
            raise ValueEngineContractError("conditional model probability mismatch")
        if self.no_vig_probability is not None:
            object.__setattr__(
                self,
                "no_vig_probability",
                _probability(self.no_vig_probability, "no_vig_probability"),
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
            _optional_number(
                self.no_vig_probability_edge,
                "no_vig_probability_edge",
            ),
        )
        if not math.isclose(
            self.expected_roi_percent,
            self.expected_value_per_unit * 100.0,
            abs_tol=1e-10,
        ):
            raise ValueEngineContractError("ROI must equal EV multiplied by 100")
        reasons = tuple(
            sorted(set(self.ineligibility_reasons), key=lambda reason: reason.value)
        )
        object.__setattr__(self, "ineligibility_reasons", reasons)
        if self.recommendation_gate_input_eligible != (not reasons):
            raise ValueEngineContractError("gate eligibility must agree with reasons")
        expected_state = (
            ValueCalculationState.COMPLETE
            if self.no_vig_probability is not None
            else ValueCalculationState.PARTIAL
        )
        if self.calculation_state is not expected_state:
            raise ValueEngineContractError("calculation state mismatch")
        if self.calculation_version != VALUE_CALCULATION_VERSION:
            raise ValueEngineContractError("unsupported value calculation version")
        if self.contract_version != MARKET_VALUE_CONTRACT_VERSION:
            raise ValueEngineContractError("unsupported market-value contract")

    def _content_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "approximation_tail_bound": self.approximation_tail_bound,
            "best_price_books": list(self.best_price_books),
            "bookmaker_count": self.bookmaker_count,
            "calculation_state": self.calculation_state.value,
            "calculation_version": self.calculation_version,
            "complete_two_way_market": self.complete_two_way_market,
            "conditional_model_probability": self.conditional_model_probability,
            "consensus_confidence": self.consensus_confidence,
            "contract_version": self.contract_version,
            "effective_provider_timestamps": [
                item.isoformat() for item in self.effective_provider_timestamps
            ],
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "fair_american_odds": self.fair_american_odds,
            "fair_decimal_odds": self.fair_decimal_odds,
            "freshness_status": self.freshness_status,
            "ineligibility_reasons": [
                reason.value for reason in self.ineligibility_reasons
            ],
            "line_key": self.line_key,
            "market": self.market.value,
            "market_evidence_checksum": self.market_evidence_checksum,
            "market_line": self.market_line,
            "market_retrieved_at": self.market_retrieved_at.isoformat(),
            "median_market_hold": self.median_market_hold,
            "model_loss_probability": self.model_loss_probability,
            "model_push_probability": self.model_push_probability,
            "model_win_probability": self.model_win_probability,
            "net_profit_per_unit": self.net_profit_per_unit,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "raw_implied_probability": self.raw_implied_probability,
            "raw_probability_edge": self.raw_probability_edge,
            "recommendation_gate_input_eligible": (
                self.recommendation_gate_input_eligible
            ),
            "side": self.side.value,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ValueGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_prediction_game_checksum: str
    upstream_matchup_packet_game_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    deployment_status: ModelDeploymentStatus
    calibration_status: ModelCalibrationStatus
    model_recommendation_eligible: bool
    quality_disposition: DataQualityDisposition
    quality_issue_codes: tuple[str, ...]
    market_reference_checksum: str | None
    odds_summary_checksum: str | None
    values: tuple[MarketValueV1, ...]
    warnings: tuple[ValueGameWarning, ...]
    contract_version: str = VALUE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise ValueEngineContractError("edge_event_id identity mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise ValueEngineContractError("daily_mlb_game_id identity mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise ValueEngineContractError("ValueGame teams are invalid")
        for name in (
            "upstream_prediction_game_checksum",
            "upstream_matchup_packet_game_checksum",
            "model_manifest_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(
            self,
            "model_version",
            _text(self.model_version, "model_version"),
        )
        codes = tuple(
            sorted({_text(item, "quality issue code") for item in self.quality_issue_codes})
        )
        object.__setattr__(self, "quality_issue_codes", codes)
        object.__setattr__(
            self,
            "market_reference_checksum",
            _optional_sha(self.market_reference_checksum, "market_reference_checksum"),
        )
        object.__setattr__(
            self,
            "odds_summary_checksum",
            _optional_sha(self.odds_summary_checksum, "odds_summary_checksum"),
        )
        if self.market_reference_checksum != self.odds_summary_checksum:
            raise ValueEngineContractError("market-reference checksum mismatch")
        values = tuple(self.values)
        if len({item.checksum for item in values}) != len(values):
            raise ValueEngineContractError("duplicate value rows")
        object.__setattr__(self, "values", values)
        warnings = tuple(sorted(set(self.warnings), key=lambda warning: warning.value))
        object.__setattr__(self, "warnings", warnings)
        if not values and not warnings:
            raise ValueEngineContractError("empty game requires explicit warning")
        if values and ValueGameWarning.NO_EVALUABLE_MARKETS in warnings:
            raise ValueEngineContractError("no-market warning conflicts with values")
        if self.contract_version != VALUE_GAME_CONTRACT_VERSION:
            raise ValueEngineContractError("unsupported value-game contract")

    @property
    def calculation_state(self) -> ValueCalculationState:
        if not self.values:
            return ValueCalculationState.UNAVAILABLE
        if all(
            value.calculation_state is ValueCalculationState.COMPLETE
            for value in self.values
        ):
            return ValueCalculationState.COMPLETE
        return ValueCalculationState.PARTIAL

    @property
    def gate_input_eligible_count(self) -> int:
        return sum(value.recommendation_gate_input_eligible for value in self.values)

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calculation_state": self.calculation_state.value,
            "calibration_status": self.calibration_status.value,
            "contract_version": self.contract_version,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "deployment_status": self.deployment_status.value,
            "edge_event_id": self.edge_event_id,
            "gate_input_eligible_count": self.gate_input_eligible_count,
            "home_team_id": self.home_team_id,
            "market_reference_checksum": self.market_reference_checksum,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_recommendation_eligible": self.model_recommendation_eligible,
            "model_version": self.model_version,
            "odds_summary_checksum": self.odds_summary_checksum,
            "quality_disposition": self.quality_disposition.value,
            "quality_issue_codes": list(self.quality_issue_codes),
            "source_game_id": self.source_game_id,
            "upstream_matchup_packet_game_checksum": (
                self.upstream_matchup_packet_game_checksum
            ),
            "upstream_prediction_game_checksum": (
                self.upstream_prediction_game_checksum
            ),
            "values": [value.as_dict() for value in self.values],
            "warnings": [warning.value for warning in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ValueEngineV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_predictions_checksum: str
    upstream_matchup_packet_checksum: str
    model_manifest_checksum: str
    games: tuple[ValueGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = VALUE_ENGINE_CONTRACT_VERSION
    calculation_version: str = VALUE_CALCULATION_VERSION
    sport: str = "MLB"
    league: str = "MLB"

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, "observed_at"),
        )
        for name in (
            "upstream_predictions_checksum",
            "upstream_matchup_packet_checksum",
            "model_manifest_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise ValueEngineContractError("duplicate Value Engine games")
        if any(
            game.model_manifest_checksum != self.model_manifest_checksum
            for game in games
        ):
            raise ValueEngineContractError("game model-manifest lineage mismatch")
        object.__setattr__(self, "games", games)
        if self.contract_version != VALUE_ENGINE_CONTRACT_VERSION:
            raise ValueEngineContractError("unsupported Value Engine contract")
        if self.calculation_version != VALUE_CALCULATION_VERSION:
            raise ValueEngineContractError("unsupported value calculation version")
        if self.sport != "MLB" or self.league != "MLB":
            raise ValueEngineContractError("sport and league must both be MLB")
        payload = self._content_dict()
        configured = tuple(str(item) for item in secret_values if str(item))
        if redact_value(payload, configured, preserve_field_names=("key",)) != payload:
            raise ValueEngineContractError("Value Engine contains credential material")

    @property
    def value_row_count(self) -> int:
        return sum(len(game.values) for game in self.games)

    @property
    def gate_input_eligible_count(self) -> int:
        return sum(game.gate_input_eligible_count for game in self.games)

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "gate_input_eligible_count": self.gate_input_eligible_count,
            "league": self.league,
            "model_manifest_checksum": self.model_manifest_checksum,
            "observed_at": self.observed_at.isoformat(),
            "requested_date": self.requested_date,
            "sport": self.sport,
            "upstream_matchup_packet_checksum": self.upstream_matchup_packet_checksum,
            "upstream_predictions_checksum": self.upstream_predictions_checksum,
            "value_row_count": self.value_row_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
