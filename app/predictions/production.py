from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS

PREDICTIONS_CONTRACT_VERSION = "DSE_MLB_ML_PREDICTIONS_V1"
PREDICTIONS_PHASE_INPUT_CONTRACT = "DSE_MLB_ML_PREDICTIONS_PHASE_INPUT_V1"
REVIEWED_ANALYST_PROVIDER_CONTRACT = "DSE_REVIEWED_ANALYST_V1"
PREDICTIONS_PROVIDER_POLICY_VERSION = "DSE_MLB_ML_PROVIDER_POLICY_V1"
MINIMUM_REVIEWED_INTERVAL_WIDTH = 0.10


class PredictionsContractError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PredictionsContractError(f"{name} must be non-empty trimmed text")
    return value


def _checksum(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PredictionsContractError(f"{name} must be a lowercase SHA-256")
    return text


def _utc(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PredictionsContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PredictionsContractError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise PredictionsContractError(f"{name} must be finite and between zero and one")
    return result


def _canonical_mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(canonical_json_bytes(dict(value)))
    except (TypeError, ValueError) as exc:
        raise PredictionsContractError(f"{name} must be finite canonical JSON") from exc
    if not isinstance(parsed, dict):
        raise PredictionsContractError(f"{name} must be an object")
    return parsed


def _contains_forbidden_market_key(value: object) -> bool:
    forbidden = {
        "odds",
        "sportsbook",
        "sportsbook_price",
        "market_price",
        "implied_prob",
        "no_vig",
        "consensus",
        "line",
        "market_context",
        "selected_offer",
        "bookmaker_offer",
        "bookmaker",
        "bookmaker_key",
        "price",
        "best_price",
        "american_odds",
        "decimal_odds",
        "implied_probability",
        "consensus_probability",
        "consensus_market_probability",
        "no_vig_probability",
        "line_movement",
        "market",
        "market_key",
        "edge",
        "expected_value",
        "ev",
        "recommendation",
        "rank",
    }
    if isinstance(value, Mapping):
        return any(
            re.sub(r"[^a-z0-9]+", "_", str(key).casefold()).strip("_") in forbidden
            or _contains_forbidden_market_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_contains_forbidden_market_key(item) for item in value)
    return False


@dataclass(frozen=True, slots=True)
class PredictionProviderPolicyV1:
    provider_kind: str = "reviewed_analyst"
    provider_contract: str = REVIEWED_ANALYST_PROVIDER_CONTRACT
    provider_version: str = "reviewed-analyst-method-v1"
    analyst_identity: str = "human-reviewed-local"
    calibration_state: str = "uncalibrated"
    market_independent: bool = True
    minimum_interval_width: float = MINIMUM_REVIEWED_INTERVAL_WIDTH
    policy_version: str = PREDICTIONS_PROVIDER_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "provider_kind",
            "provider_contract",
            "provider_version",
            "analyst_identity",
            "calibration_state",
            "policy_version",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.provider_kind == "reviewed_analyst" and self.provider_contract != REVIEWED_ANALYST_PROVIDER_CONTRACT:
            raise PredictionsContractError("reviewed analyst provider contract is invalid")
        if self.provider_kind == "reviewed_analyst" and self.calibration_state != "uncalibrated":
            raise PredictionsContractError("reviewed analyst predictions must be uncalibrated")
        if self.market_independent is not True:
            raise PredictionsContractError("V1 prediction policy must be market-independent")
        width = _probability(self.minimum_interval_width, "minimum_interval_width")
        if width < MINIMUM_REVIEWED_INTERVAL_WIDTH:
            raise PredictionsContractError("reviewed analyst interval policy is too narrow")
        object.__setattr__(self, "minimum_interval_width", width)

    def identity_dict(self) -> dict[str, object]:
        return {
            "analyst_identity": self.analyst_identity,
            "calibration_state": self.calibration_state,
            "market_independent": self.market_independent,
            "minimum_interval_width": self.minimum_interval_width,
            "policy_version": self.policy_version,
            "provider_contract": self.provider_contract,
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ReviewedPredictionInputV1:
    run_id: str
    source_game_id: str
    upstream_model_feature_set_snapshot_id: str
    upstream_model_feature_set_checksum: str
    upstream_model_feature_game_checksum: str
    predictive_feature_checksum: str
    provider_policy: PredictionProviderPolicyV1
    home_probability: float
    home_lower: float
    home_upper: float
    generated_at: datetime
    sealed_at: datetime
    authoring_evidence: Mapping[str, object]
    market_independence_attested: bool
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(
            self,
            "upstream_model_feature_set_snapshot_id",
            _text(self.upstream_model_feature_set_snapshot_id, "upstream_model_feature_set_snapshot_id"),
        )
        for name in (
            "upstream_model_feature_set_checksum",
            "upstream_model_feature_game_checksum",
            "predictive_feature_checksum",
        ):
            object.__setattr__(self, name, _checksum(getattr(self, name), name))
        home = _probability(self.home_probability, "home_probability")
        lower = _probability(self.home_lower, "home_lower")
        upper = _probability(self.home_upper, "home_upper")
        if not lower <= home <= upper:
            raise PredictionsContractError("home probability must be inside its interval")
        if upper - lower + 1e-15 < self.provider_policy.minimum_interval_width:
            raise PredictionsContractError("reviewed analyst interval is too narrow")
        object.__setattr__(self, "home_probability", home)
        object.__setattr__(self, "home_lower", lower)
        object.__setattr__(self, "home_upper", upper)
        generated = _utc(self.generated_at, "generated_at")
        sealed = _utc(self.sealed_at, "sealed_at")
        if generated > sealed:
            raise PredictionsContractError("generated_at cannot follow sealed_at")
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "sealed_at", sealed)
        if not isinstance(self.market_independence_attested, bool) or self.market_independence_attested is not True:
            raise PredictionsContractError("reviewer must explicitly attest market independence")
        evidence = _canonical_mapping(self.authoring_evidence, "authoring_evidence")
        if _contains_forbidden_market_key(evidence):
            raise PredictionsContractError("authoring evidence contains market-derived input")
        object.__setattr__(self, "authoring_evidence", evidence)
        payload = self.as_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured) != payload:
            raise PredictionsContractError("reviewed prediction input contains credential-bearing material")

    @property
    def evidence_checksum(self) -> str:
        return canonical_sha256(dict(self.authoring_evidence))

    def identity_dict(self) -> dict[str, object]:
        return {
            "authoring_evidence": dict(self.authoring_evidence),
            "evidence_checksum": self.evidence_checksum,
            "generated_at": self.generated_at.isoformat(),
            "home_lower": self.home_lower,
            "home_probability": self.home_probability,
            "home_upper": self.home_upper,
            "market_independence_attested": self.market_independence_attested,
            "predictive_feature_checksum": self.predictive_feature_checksum,
            "provider_policy": self.provider_policy.as_dict(),
            "run_id": self.run_id,
            "sealed_at": self.sealed_at.isoformat(),
            "source_game_id": self.source_game_id,
            "upstream_model_feature_game_checksum": self.upstream_model_feature_game_checksum,
            "upstream_model_feature_set_checksum": self.upstream_model_feature_set_checksum,
            "upstream_model_feature_set_snapshot_id": self.upstream_model_feature_set_snapshot_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class MoneylinePredictionV1:
    source_game_id: str
    ordinal: int
    away_team_id: str
    home_team_id: str
    scheduled_start_time: datetime
    predictive_feature_checksum: str
    upstream_model_feature_game_checksum: str
    provider_policy: PredictionProviderPolicyV1
    home_probability: float
    home_lower: float
    home_upper: float
    generated_at: datetime
    sealed_at: datetime
    evidence_checksum: str
    market_independence_attested: bool
    completeness_state: str = "complete"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise PredictionsContractError("ordinal must be a positive integer")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise PredictionsContractError("prediction team identity is invalid")
        start = _utc(self.scheduled_start_time, "scheduled_start_time")
        generated = _utc(self.generated_at, "generated_at")
        sealed = _utc(self.sealed_at, "sealed_at")
        if generated > sealed or sealed >= start:
            raise PredictionsContractError("prediction must be generated and sealed before game start")
        object.__setattr__(self, "scheduled_start_time", start)
        object.__setattr__(self, "generated_at", generated)
        object.__setattr__(self, "sealed_at", sealed)
        for name in ("predictive_feature_checksum", "upstream_model_feature_game_checksum", "evidence_checksum"):
            object.__setattr__(self, name, _checksum(getattr(self, name), name))
        if not isinstance(self.market_independence_attested, bool) or self.market_independence_attested is not True:
            raise PredictionsContractError("prediction requires explicit market-independence attestation")
        home = _probability(self.home_probability, "home_probability")
        lower = _probability(self.home_lower, "home_lower")
        upper = _probability(self.home_upper, "home_upper")
        if not lower <= home <= upper or upper - lower + 1e-15 < self.provider_policy.minimum_interval_width:
            raise PredictionsContractError("prediction interval is invalid")
        object.__setattr__(self, "home_probability", home)
        object.__setattr__(self, "home_lower", lower)
        object.__setattr__(self, "home_upper", upper)
        if self.completeness_state not in {"complete", "degraded"}:
            raise PredictionsContractError("prediction completeness_state is invalid")

    @property
    def away_probability(self) -> float:
        return 1.0 - self.home_probability

    @property
    def away_lower(self) -> float:
        return 1.0 - self.home_upper

    @property
    def away_upper(self) -> float:
        return 1.0 - self.home_lower

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_lower": self.away_lower,
            "away_probability": self.away_probability,
            "away_team_id": self.away_team_id,
            "away_upper": self.away_upper,
            "calibration_state": self.provider_policy.calibration_state,
            "completeness_state": self.completeness_state,
            "evidence_checksum": self.evidence_checksum,
            "generated_at": self.generated_at.isoformat(),
            "home_lower": self.home_lower,
            "home_probability": self.home_probability,
            "home_team_id": self.home_team_id,
            "home_upper": self.home_upper,
            "market_independence_attestation": self.market_independence_attested,
            "ordinal": self.ordinal,
            "predictive_feature_checksum": self.predictive_feature_checksum,
            "provider_identity": self.provider_policy.as_dict(),
            "scheduled_start_time": self.scheduled_start_time.isoformat(),
            "sealed_at": self.sealed_at.isoformat(),
            "source_game_id": self.source_game_id,
            "upstream_model_feature_game_checksum": self.upstream_model_feature_game_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PredictionsV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    provider_policy: PredictionProviderPolicyV1
    upstream_model_feature_set_snapshot_id: str
    upstream_model_feature_set_checksum: str
    upstream_data_quality_snapshot_id: str
    upstream_data_quality_checksum: str
    input_inventory_checksum: str
    games: tuple[MoneylinePredictionV1, ...]
    warnings: tuple[Mapping[str, object], ...] = ()
    contract_version: str = PREDICTIONS_CONTRACT_VERSION
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))
        if self.contract_version != PREDICTIONS_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported Predictions contract")
        for name in (
            "upstream_model_feature_set_checksum",
            "upstream_data_quality_checksum",
            "input_inventory_checksum",
        ):
            object.__setattr__(self, name, _checksum(getattr(self, name), name))
        for name in ("upstream_model_feature_set_snapshot_id", "upstream_data_quality_snapshot_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        games = tuple(self.games)
        if [game.ordinal for game in games] != list(range(1, len(games) + 1)):
            raise PredictionsContractError("prediction ordinals must be contiguous")
        if len({game.source_game_id for game in games}) != len(games):
            raise PredictionsContractError("prediction game identity must be unique")
        if any(game.sealed_at > self.observed_at for game in games):
            raise PredictionsContractError("prediction input cannot be sealed after the phase observation boundary")
        object.__setattr__(self, "games", games)
        warnings = tuple(_canonical_mapping(value, "warning") for value in self.warnings)
        object.__setattr__(self, "warnings", warnings)
        payload = self.identity_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured) != payload:
            raise PredictionsContractError("Predictions snapshot contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "input_inventory_checksum": self.input_inventory_checksum,
            "observed_at": self.observed_at.isoformat(),
            "provider_policy": self.provider_policy.as_dict(),
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_data_quality_checksum": self.upstream_data_quality_checksum,
            "upstream_data_quality_snapshot_id": self.upstream_data_quality_snapshot_id,
            "upstream_model_feature_set_checksum": self.upstream_model_feature_set_checksum,
            "upstream_model_feature_set_snapshot_id": self.upstream_model_feature_set_snapshot_id,
            "warnings": [dict(value) for value in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
