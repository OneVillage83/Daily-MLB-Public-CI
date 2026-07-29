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
from app.recommendation_gate.policy import (
    RECOMMENDATION_POLICY_ID,
    RECOMMENDATION_POLICY_VERSION,
    RecommendationPolicyV1,
)
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS
from app.value_engine.contracts import ValueMarket, ValueSide

RECOMMENDATION_GATE_CONTRACT_VERSION = "DSE_RECOMMENDATION_GATE_V1"
RECOMMENDATION_GAME_CONTRACT_VERSION = "DSE_RECOMMENDATION_GAME_V1"
RECOMMENDATION_CONTRACT_VERSION = "DSE_RECOMMENDATION_V1"
RECOMMENDATION_GATE_RESULT_CONTRACT_VERSION = "DSE_RECOMMENDATION_GATE_RESULT_V1"


class RecommendationGateContractError(ValueError):
    """Raised when canonical Recommendation Gate V1 evidence is invalid."""


class RecommendationDecision(StrEnum):
    BET = "BET"
    LEAN = "LEAN"
    PASS = "PASS"
    AVOID = "AVOID"


class EvidenceConfidenceBand(StrEnum):
    ELITE = "Elite"
    HIGH = "High"
    STRONG = "Strong"
    LEAN = "Lean"
    PASS = "Pass"


class OperationalRiskLevel(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    UNACCEPTABLE = "unacceptable"


class RecommendationReason(StrEnum):
    QUALIFIED_BET = "qualified_bet"
    QUALIFIED_LEAN = "qualified_lean"
    MODEL_NOT_RECOMMENDATION_ELIGIBLE = "model_not_recommendation_eligible"
    MODEL_NOT_PRODUCTION = "model_not_production"
    MODEL_NOT_CALIBRATED = "model_not_calibrated"
    DATA_QUALITY_INSUFFICIENT = "data_quality_insufficient"
    CALCULATION_INCOMPLETE = "calculation_incomplete"
    INCOMPLETE_TWO_WAY_MARKET = "incomplete_two_way_market"
    MISSING_NO_VIG_PROBABILITY = "missing_no_vig_probability"
    MARKET_STALE = "market_stale"
    MARKET_FRESHNESS_UNKNOWN = "market_freshness_unknown"
    MARKET_AGING = "market_aging"
    INSUFFICIENT_MARKET_COVERAGE = "insufficient_market_coverage"
    INSUFFICIENT_MODEL_MARKET_DISAGREEMENT = (
        "insufficient_model_market_disagreement"
    )
    INSUFFICIENT_EXPECTED_VALUE = "insufficient_expected_value"
    INSUFFICIENT_EVIDENCE_CONFIDENCE = "insufficient_evidence_confidence"
    BET_FLOOR_NOT_MET = "bet_floor_not_met"


class RecommendationGameWarning(StrEnum):
    NO_VALUE_ROWS = "no_value_rows"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RecommendationGateContractError(
            f"{name} must be non-empty trimmed text"
        )
    return value


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RecommendationGateContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise RecommendationGateContractError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecommendationGateContractError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RecommendationGateContractError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise RecommendationGateContractError(f"{name} must be in [0,1]")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def confidence_band(score: int) -> EvidenceConfidenceBand:
    if score >= 90:
        return EvidenceConfidenceBand.ELITE
    if score >= 80:
        return EvidenceConfidenceBand.HIGH
    if score >= 70:
        return EvidenceConfidenceBand.STRONG
    if score >= 60:
        return EvidenceConfidenceBand.LEAN
    return EvidenceConfidenceBand.PASS


@dataclass(frozen=True, slots=True)
class PolicyGateResultV1:
    gate_code: str
    passed: bool
    threshold: str
    observed_value: str
    reason_code: str
    source_checksum: str
    evaluated_at: datetime
    policy_id: str = RECOMMENDATION_POLICY_ID
    policy_version: str = RECOMMENDATION_POLICY_VERSION
    contract_version: str = RECOMMENDATION_GATE_RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("gate_code", "threshold", "observed_value", "reason_code"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self, "source_checksum", _sha(self.source_checksum, "source_checksum")
        )
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        if self.policy_id != RECOMMENDATION_POLICY_ID:
            raise RecommendationGateContractError("unsupported gate policy_id")
        if self.policy_version != RECOMMENDATION_POLICY_VERSION:
            raise RecommendationGateContractError("unsupported gate policy_version")
        if self.contract_version != RECOMMENDATION_GATE_RESULT_CONTRACT_VERSION:
            raise RecommendationGateContractError("unsupported gate-result contract")

    def _content_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "gate_code": self.gate_code,
            "observed_value": self.observed_value,
            "passed": self.passed,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "reason_code": self.reason_code,
            "source_checksum": self.source_checksum,
            "threshold": self.threshold,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RecommendationV1:
    upstream_market_value_checksum: str
    market: ValueMarket
    line_key: str
    side: ValueSide
    market_line: float | None
    american_price: float
    bookmaker_count: int
    freshness_status: str
    conditional_model_probability: float
    no_vig_probability: float | None
    no_vig_probability_edge: float | None
    expected_value_per_unit: float
    expected_roi_percent: float
    decision: RecommendationDecision
    evidence_confidence_score: int
    evidence_confidence_band: EvidenceConfidenceBand
    operational_risk_score: int
    operational_risk_level: OperationalRiskLevel
    reasons: tuple[RecommendationReason, ...]
    gate_results: tuple[PolicyGateResultV1, ...]
    review_required: bool
    publication_candidate: bool
    manual_promotion_permitted: bool
    policy_checksum: str
    evaluated_at: datetime
    contract_version: str = RECOMMENDATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "upstream_market_value_checksum",
            _sha(self.upstream_market_value_checksum, "upstream_market_value_checksum"),
        )
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(
            self, "market_line", _optional_number(self.market_line, "market_line")
        )
        if self.market is ValueMarket.MONEYLINE and self.market_line is not None:
            raise RecommendationGateContractError("moneyline cannot contain a point")
        if self.market is not ValueMarket.MONEYLINE and self.market_line is None:
            raise RecommendationGateContractError("spread/total requires a point")
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise RecommendationGateContractError("invalid American price")
        object.__setattr__(self, "american_price", price)
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count <= 0
        ):
            raise RecommendationGateContractError("bookmaker_count must be positive")
        object.__setattr__(
            self, "freshness_status", _text(self.freshness_status, "freshness_status")
        )
        object.__setattr__(
            self,
            "conditional_model_probability",
            _probability(
                self.conditional_model_probability,
                "conditional_model_probability",
            ),
        )
        object.__setattr__(
            self,
            "no_vig_probability",
            _optional_probability(self.no_vig_probability, "no_vig_probability"),
        )
        object.__setattr__(
            self,
            "no_vig_probability_edge",
            _optional_number(self.no_vig_probability_edge, "no_vig_probability_edge"),
        )
        for name in ("expected_value_per_unit", "expected_roi_percent"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not math.isclose(
            self.expected_roi_percent,
            self.expected_value_per_unit * 100.0,
            abs_tol=1e-10,
        ):
            raise RecommendationGateContractError("ROI disagrees with EV")
        for name in ("evidence_confidence_score", "operational_risk_score"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 100
            ):
                raise RecommendationGateContractError(f"{name} must be in [0,100]")
        if self.evidence_confidence_band is not confidence_band(
            self.evidence_confidence_score
        ):
            raise RecommendationGateContractError("confidence band mismatch")
        if self.operational_risk_score != 100 - self.evidence_confidence_score:
            raise RecommendationGateContractError("risk score mismatch")
        reasons = tuple(sorted(set(self.reasons), key=lambda reason: reason.value))
        if not reasons:
            raise RecommendationGateContractError("recommendation reasons cannot be empty")
        object.__setattr__(self, "reasons", reasons)
        gates = tuple(self.gate_results)
        if len(gates) != 17 or len({gate.gate_code for gate in gates}) != 17:
            raise RecommendationGateContractError("exactly 17 unique gates are required")
        object.__setattr__(self, "gate_results", gates)
        actionable = self.decision in {
            RecommendationDecision.BET,
            RecommendationDecision.LEAN,
        }
        if self.review_required != actionable:
            raise RecommendationGateContractError("review_required mismatch")
        if self.publication_candidate != (self.decision is RecommendationDecision.BET):
            raise RecommendationGateContractError("publication_candidate mismatch")
        if self.manual_promotion_permitted:
            raise RecommendationGateContractError("manual promotion is prohibited")
        if self.decision is RecommendationDecision.AVOID:
            if self.operational_risk_level is not OperationalRiskLevel.UNACCEPTABLE:
                raise RecommendationGateContractError("AVOID risk must be unacceptable")
        elif self.operational_risk_level is OperationalRiskLevel.UNACCEPTABLE:
            raise RecommendationGateContractError("only AVOID may be unacceptable")
        if self.decision is not RecommendationDecision.AVOID and (
            self.no_vig_probability is None or self.no_vig_probability_edge is None
        ):
            raise RecommendationGateContractError(
                "non-AVOID decisions require no-vig evidence"
            )
        object.__setattr__(self, "policy_checksum", _sha(self.policy_checksum, "policy_checksum"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        if self.contract_version != RECOMMENDATION_CONTRACT_VERSION:
            raise RecommendationGateContractError("unsupported recommendation contract")

    def _content_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "bookmaker_count": self.bookmaker_count,
            "conditional_model_probability": self.conditional_model_probability,
            "contract_version": self.contract_version,
            "decision": self.decision.value,
            "evaluated_at": self.evaluated_at.isoformat(),
            "evidence_confidence_band": self.evidence_confidence_band.value,
            "evidence_confidence_score": self.evidence_confidence_score,
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "freshness_status": self.freshness_status,
            "gate_results": [gate.as_dict() for gate in self.gate_results],
            "line_key": self.line_key,
            "manual_promotion_permitted": self.manual_promotion_permitted,
            "market": self.market.value,
            "market_line": self.market_line,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "operational_risk_level": self.operational_risk_level.value,
            "operational_risk_score": self.operational_risk_score,
            "policy_checksum": self.policy_checksum,
            "publication_candidate": self.publication_candidate,
            "reasons": [reason.value for reason in self.reasons],
            "review_required": self.review_required,
            "side": self.side.value,
            "upstream_market_value_checksum": self.upstream_market_value_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RecommendationGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_value_game_checksum: str
    model_manifest_checksum: str
    deployment_status: ModelDeploymentStatus
    calibration_status: ModelCalibrationStatus
    model_recommendation_eligible: bool
    quality_disposition: DataQualityDisposition
    recommendations: tuple[RecommendationV1, ...]
    warnings: tuple[RecommendationGameWarning, ...]
    contract_version: str = RECOMMENDATION_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise RecommendationGateContractError("edge_event_id mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise RecommendationGateContractError("daily_mlb_game_id mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise RecommendationGateContractError("invalid game teams")
        object.__setattr__(
            self,
            "upstream_value_game_checksum",
            _sha(self.upstream_value_game_checksum, "upstream_value_game_checksum"),
        )
        object.__setattr__(
            self,
            "model_manifest_checksum",
            _sha(self.model_manifest_checksum, "model_manifest_checksum"),
        )
        recommendations = tuple(self.recommendations)
        if len({item.upstream_market_value_checksum for item in recommendations}) != len(
            recommendations
        ):
            raise RecommendationGateContractError("duplicate upstream value decisions")
        object.__setattr__(self, "recommendations", recommendations)
        warnings = tuple(sorted(set(self.warnings), key=lambda warning: warning.value))
        object.__setattr__(self, "warnings", warnings)
        if not recommendations and warnings != (RecommendationGameWarning.NO_VALUE_ROWS,):
            raise RecommendationGateContractError(
                "zero-recommendation game requires NO_VALUE_ROWS"
            )
        if recommendations and warnings:
            raise RecommendationGateContractError(
                "recommendation-bearing game cannot carry warnings"
            )
        if self.contract_version != RECOMMENDATION_GAME_CONTRACT_VERSION:
            raise RecommendationGateContractError("unsupported recommendation-game contract")

    def decision_count(self, decision: RecommendationDecision) -> int:
        return sum(item.decision is decision for item in self.recommendations)

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calibration_status": self.calibration_status.value,
            "contract_version": self.contract_version,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "decision_counts": {
                decision.value: self.decision_count(decision)
                for decision in RecommendationDecision
            },
            "deployment_status": self.deployment_status.value,
            "edge_event_id": self.edge_event_id,
            "home_team_id": self.home_team_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_recommendation_eligible": self.model_recommendation_eligible,
            "quality_disposition": self.quality_disposition.value,
            "recommendations": [item.as_dict() for item in self.recommendations],
            "source_game_id": self.source_game_id,
            "upstream_value_game_checksum": self.upstream_value_game_checksum,
            "warnings": [warning.value for warning in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RecommendationGateV1:
    requested_date: str
    as_of_time: datetime
    evaluated_at: datetime
    upstream_value_engine_checksum: str
    policy: RecommendationPolicyV1
    games: tuple[RecommendationGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = RECOMMENDATION_GATE_CONTRACT_VERSION
    sport: str = "MLB"
    league: str = "MLB"

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        object.__setattr__(
            self,
            "upstream_value_engine_checksum",
            _sha(self.upstream_value_engine_checksum, "upstream_value_engine_checksum"),
        )
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise RecommendationGateContractError("duplicate recommendation games")
        object.__setattr__(self, "games", games)
        if self.contract_version != RECOMMENDATION_GATE_CONTRACT_VERSION:
            raise RecommendationGateContractError("unsupported Recommendation Gate contract")
        if self.sport != "MLB" or self.league != "MLB":
            raise RecommendationGateContractError("sport and league must be MLB")
        payload = self._content_dict()
        configured = tuple(str(item) for item in secret_values if str(item))
        if redact_value(payload, configured, preserve_field_names=("key",)) != payload:
            raise RecommendationGateContractError(
                "Recommendation Gate contains credential-bearing material"
            )

    @property
    def recommendation_count(self) -> int:
        return sum(len(game.recommendations) for game in self.games)

    def decision_count(self, decision: RecommendationDecision) -> int:
        return sum(game.decision_count(decision) for game in self.games)

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "decision_counts": {
                decision.value: self.decision_count(decision)
                for decision in RecommendationDecision
            },
            "evaluated_at": self.evaluated_at.isoformat(),
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "recommendation_count": self.recommendation_count,
            "requested_date": self.requested_date,
            "sport": self.sport,
            "upstream_value_engine_checksum": self.upstream_value_engine_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
