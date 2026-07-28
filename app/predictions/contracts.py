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
from app.model_feature_set.schema import (
    FEATURE_NAME_SET_V1,
    MODEL_FEATURE_SCHEMA_CHECKSUM,
    MODEL_FEATURE_SCHEMA_VERSION,
)
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS

PREDICTIONS_CONTRACT_VERSION = "DSE_PREDICTIONS_V1"
PREDICTION_GAME_CONTRACT_VERSION = "DSE_PREDICTION_GAME_V1"
PREDICTION_MODEL_MANIFEST_CONTRACT_VERSION = "DSE_PREDICTION_MODEL_MANIFEST_V1"
PREDICTION_FEATURE_TERM_CONTRACT_VERSION = "DSE_PREDICTION_FEATURE_TERM_V1"
RUN_DISTRIBUTION_CONTRACT_VERSION = "DSE_RUN_DISTRIBUTION_V1"
PREDICTION_CALCULATION_VERSION = "DSE_INDEPENDENT_POISSON_V1"
PREDICTIONS_SPORT = "MLB"
PREDICTIONS_LEAGUE = "MLB"


class PredictionsContractError(ValueError):
    """Raised when a canonical Predictions V1 contract is invalid."""


class ModelDeploymentStatus(StrEnum):
    REFERENCE_ONLY = "reference_only"
    EVALUATION = "evaluation"
    PRODUCTION = "production"


class ModelCalibrationStatus(StrEnum):
    UNCALIBRATED_REFERENCE = "uncalibrated_reference"
    UNCALIBRATED = "uncalibrated"
    CALIBRATED = "calibrated"


class PredictionInputState(StrEnum):
    COMPLETE = "complete"
    IMPUTED = "imputed"
    PRIOR_ONLY = "prior_only"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PredictionsContractError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _required_text(value, name)


def _aware_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PredictionsContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _optional_aware_utc(value: object, name: str) -> datetime | None:
    return None if value is None else _aware_utc(value, name)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PredictionsContractError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PredictionsContractError(f"{name} must be finite")
    return numeric


def _probability(value: object, name: str) -> float:
    numeric = _finite(value, name)
    if numeric < 0.0 or numeric > 1.0:
        raise PredictionsContractError(f"{name} must be between 0 and 1")
    return numeric


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise PredictionsContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


@dataclass(frozen=True, slots=True)
class PredictionFeatureTermV1:
    feature_name: str
    center: float
    scale: float
    missing_value: float
    home_coefficient: float
    away_coefficient: float
    contract_version: str = PREDICTION_FEATURE_TERM_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.feature_name not in FEATURE_NAME_SET_V1:
            raise PredictionsContractError(
                f"prediction term feature is not in ModelFeatureSet V1: {self.feature_name}"
            )
        object.__setattr__(self, "center", _finite(self.center, "term center"))
        scale = _finite(self.scale, "term scale")
        if scale <= 0.0:
            raise PredictionsContractError("term scale must be positive")
        object.__setattr__(self, "scale", scale)
        object.__setattr__(
            self,
            "missing_value",
            _finite(self.missing_value, "term missing_value"),
        )
        object.__setattr__(
            self,
            "home_coefficient",
            _finite(self.home_coefficient, "term home_coefficient"),
        )
        object.__setattr__(
            self,
            "away_coefficient",
            _finite(self.away_coefficient, "term away_coefficient"),
        )
        if self.contract_version != PREDICTION_FEATURE_TERM_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported prediction feature-term contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "away_coefficient": self.away_coefficient,
            "center": self.center,
            "contract_version": self.contract_version,
            "feature_name": self.feature_name,
            "home_coefficient": self.home_coefficient,
            "missing_value": self.missing_value,
            "scale": self.scale,
        }


@dataclass(frozen=True, slots=True)
class PredictionModelManifestV1:
    model_id: str
    model_version: str
    model_kind: str
    deployment_status: ModelDeploymentStatus
    calibration_status: ModelCalibrationStatus
    recommendation_eligible: bool
    parameter_origin: str
    home_log_rate_intercept: float
    away_log_rate_intercept: float
    minimum_run_rate: float
    maximum_run_rate: float
    terms: tuple[PredictionFeatureTermV1, ...]
    tail_tolerance: float
    maximum_run_support: int
    training_dataset_checksum: str | None = None
    training_code_version: str | None = None
    trained_at: datetime | None = None
    feature_schema_version: str = MODEL_FEATURE_SCHEMA_VERSION
    feature_schema_checksum: str = MODEL_FEATURE_SCHEMA_CHECKSUM
    calculation_version: str = PREDICTION_CALCULATION_VERSION
    contract_version: str = PREDICTION_MODEL_MANIFEST_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("model_id", "model_version", "model_kind", "parameter_origin"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        object.__setattr__(
            self,
            "home_log_rate_intercept",
            _finite(self.home_log_rate_intercept, "home_log_rate_intercept"),
        )
        object.__setattr__(
            self,
            "away_log_rate_intercept",
            _finite(self.away_log_rate_intercept, "away_log_rate_intercept"),
        )
        minimum = _finite(self.minimum_run_rate, "minimum_run_rate")
        maximum = _finite(self.maximum_run_rate, "maximum_run_rate")
        if minimum <= 0.0 or maximum <= minimum:
            raise PredictionsContractError("run-rate bounds are invalid")
        object.__setattr__(self, "minimum_run_rate", minimum)
        object.__setattr__(self, "maximum_run_rate", maximum)
        terms = tuple(self.terms)
        if len({term.feature_name for term in terms}) != len(terms):
            raise PredictionsContractError("prediction manifest contains duplicate terms")
        object.__setattr__(self, "terms", terms)
        tolerance = _finite(self.tail_tolerance, "tail_tolerance")
        if tolerance <= 0.0 or tolerance >= 1.0:
            raise PredictionsContractError("tail_tolerance must be between 0 and 1")
        object.__setattr__(self, "tail_tolerance", tolerance)
        if (
            isinstance(self.maximum_run_support, bool)
            or not isinstance(self.maximum_run_support, int)
            or self.maximum_run_support < 20
            or self.maximum_run_support > 200
        ):
            raise PredictionsContractError(
                "maximum_run_support must be an integer between 20 and 200"
            )
        if self.training_dataset_checksum is not None:
            object.__setattr__(
                self,
                "training_dataset_checksum",
                _sha256(
                    self.training_dataset_checksum,
                    "training_dataset_checksum",
                ),
            )
        object.__setattr__(
            self,
            "training_code_version",
            _optional_text(self.training_code_version, "training_code_version"),
        )
        object.__setattr__(
            self,
            "trained_at",
            _optional_aware_utc(self.trained_at, "trained_at"),
        )
        if self.feature_schema_version != MODEL_FEATURE_SCHEMA_VERSION:
            raise PredictionsContractError("unsupported ModelFeatureSet schema version")
        if self.feature_schema_checksum != MODEL_FEATURE_SCHEMA_CHECKSUM:
            raise PredictionsContractError("ModelFeatureSet schema checksum mismatch")
        if self.calculation_version != PREDICTION_CALCULATION_VERSION:
            raise PredictionsContractError("unsupported prediction calculation version")
        if self.contract_version != PREDICTION_MODEL_MANIFEST_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported prediction model manifest contract")
        if self.recommendation_eligible and (
            self.deployment_status is not ModelDeploymentStatus.PRODUCTION
            or self.calibration_status is not ModelCalibrationStatus.CALIBRATED
        ):
            raise PredictionsContractError(
                "recommendation-eligible models must be production and calibrated"
            )
        if self.deployment_status is ModelDeploymentStatus.REFERENCE_ONLY:
            if self.recommendation_eligible:
                raise PredictionsContractError(
                    "reference-only model cannot be recommendation eligible"
                )
            if (
                self.calibration_status
                is not ModelCalibrationStatus.UNCALIBRATED_REFERENCE
            ):
                raise PredictionsContractError(
                    "reference-only model must use uncalibrated_reference status"
                )

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_log_rate_intercept": self.away_log_rate_intercept,
            "calculation_version": self.calculation_version,
            "calibration_status": self.calibration_status.value,
            "contract_version": self.contract_version,
            "deployment_status": self.deployment_status.value,
            "feature_schema_checksum": self.feature_schema_checksum,
            "feature_schema_version": self.feature_schema_version,
            "home_log_rate_intercept": self.home_log_rate_intercept,
            "maximum_run_rate": self.maximum_run_rate,
            "maximum_run_support": self.maximum_run_support,
            "minimum_run_rate": self.minimum_run_rate,
            "model_id": self.model_id,
            "model_kind": self.model_kind,
            "model_version": self.model_version,
            "parameter_origin": self.parameter_origin,
            "recommendation_eligible": self.recommendation_eligible,
            "tail_tolerance": self.tail_tolerance,
            "terms": [term.as_dict() for term in self.terms],
            "trained_at": None if self.trained_at is None else self.trained_at.isoformat(),
            "training_code_version": self.training_code_version,
            "training_dataset_checksum": self.training_dataset_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PredictionFeatureContributionV1:
    feature_name: str
    raw_value: float | None
    effective_value: float
    standardized_value: float
    home_log_rate_contribution: float
    away_log_rate_contribution: float
    was_imputed: bool

    def __post_init__(self) -> None:
        if self.feature_name not in FEATURE_NAME_SET_V1:
            raise PredictionsContractError(
                "contribution feature is not in ModelFeatureSet V1"
            )
        if self.raw_value is not None:
            object.__setattr__(
                self,
                "raw_value",
                _finite(self.raw_value, "contribution raw_value"),
            )
        for name in (
            "effective_value",
            "standardized_value",
            "home_log_rate_contribution",
            "away_log_rate_contribution",
        ):
            object.__setattr__(self, name, _finite(getattr(self, name), name))
        if not isinstance(self.was_imputed, bool):
            raise PredictionsContractError("was_imputed must be boolean")
        if self.was_imputed != (self.raw_value is None):
            raise PredictionsContractError(
                "was_imputed must agree with contribution raw_value"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "away_log_rate_contribution": self.away_log_rate_contribution,
            "effective_value": self.effective_value,
            "feature_name": self.feature_name,
            "home_log_rate_contribution": self.home_log_rate_contribution,
            "raw_value": self.raw_value,
            "standardized_value": self.standardized_value,
            "was_imputed": self.was_imputed,
        }


@dataclass(frozen=True, slots=True)
class RunDistributionV1:
    home_run_rate: float
    away_run_rate: float
    maximum_run_support: int
    home_run_probabilities: tuple[float, ...]
    away_run_probabilities: tuple[float, ...]
    home_tail_probability: float
    away_tail_probability: float
    retained_joint_probability: float
    approximation_tail_bound: float
    calculation_version: str = PREDICTION_CALCULATION_VERSION
    contract_version: str = RUN_DISTRIBUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        home_rate = _finite(self.home_run_rate, "home_run_rate")
        away_rate = _finite(self.away_run_rate, "away_run_rate")
        if home_rate <= 0.0 or away_rate <= 0.0:
            raise PredictionsContractError("run rates must be positive")
        object.__setattr__(self, "home_run_rate", home_rate)
        object.__setattr__(self, "away_run_rate", away_rate)
        if (
            isinstance(self.maximum_run_support, bool)
            or not isinstance(self.maximum_run_support, int)
            or self.maximum_run_support < 0
        ):
            raise PredictionsContractError("maximum_run_support must be nonnegative")
        expected_length = self.maximum_run_support + 1
        home = tuple(
            _probability(value, "home_run_probability")
            for value in self.home_run_probabilities
        )
        away = tuple(
            _probability(value, "away_run_probability")
            for value in self.away_run_probabilities
        )
        if len(home) != expected_length or len(away) != expected_length:
            raise PredictionsContractError(
                "run probability arrays must cover zero through maximum_run_support"
            )
        object.__setattr__(self, "home_run_probabilities", home)
        object.__setattr__(self, "away_run_probabilities", away)
        home_tail = _probability(
            self.home_tail_probability,
            "home_tail_probability",
        )
        away_tail = _probability(
            self.away_tail_probability,
            "away_tail_probability",
        )
        object.__setattr__(self, "home_tail_probability", home_tail)
        object.__setattr__(self, "away_tail_probability", away_tail)
        if not math.isclose(sum(home) + home_tail, 1.0, abs_tol=1e-10):
            raise PredictionsContractError("home run probabilities do not sum to one")
        if not math.isclose(sum(away) + away_tail, 1.0, abs_tol=1e-10):
            raise PredictionsContractError("away run probabilities do not sum to one")
        retained = _probability(
            self.retained_joint_probability,
            "retained_joint_probability",
        )
        expected_retained = (1.0 - home_tail) * (1.0 - away_tail)
        if not math.isclose(retained, expected_retained, abs_tol=1e-10):
            raise PredictionsContractError(
                "retained_joint_probability disagrees with marginal tails"
            )
        object.__setattr__(self, "retained_joint_probability", retained)
        tail_bound = _probability(
            self.approximation_tail_bound,
            "approximation_tail_bound",
        )
        if not math.isclose(tail_bound, 1.0 - retained, abs_tol=1e-10):
            raise PredictionsContractError(
                "approximation_tail_bound disagrees with retained probability"
            )
        object.__setattr__(self, "approximation_tail_bound", tail_bound)
        if self.calculation_version != PREDICTION_CALCULATION_VERSION:
            raise PredictionsContractError("unsupported run-distribution calculation")
        if self.contract_version != RUN_DISTRIBUTION_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported run-distribution contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "approximation_tail_bound": self.approximation_tail_bound,
            "away_run_probabilities": list(self.away_run_probabilities),
            "away_run_rate": self.away_run_rate,
            "away_tail_probability": self.away_tail_probability,
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "home_run_probabilities": list(self.home_run_probabilities),
            "home_run_rate": self.home_run_rate,
            "home_tail_probability": self.home_tail_probability,
            "maximum_run_support": self.maximum_run_support,
            "retained_joint_probability": self.retained_joint_probability,
        }


@dataclass(frozen=True, slots=True)
class GamePredictionV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_model_feature_game_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    deployment_status: ModelDeploymentStatus
    calibration_status: ModelCalibrationStatus
    recommendation_eligible: bool
    quality_disposition: DataQualityDisposition
    quality_issue_codes: tuple[str, ...]
    market_reference_checksum: str | None
    input_state: PredictionInputState
    used_feature_names: tuple[str, ...]
    imputed_feature_names: tuple[str, ...]
    model_input_checksum: str
    contributions: tuple[PredictionFeatureContributionV1, ...]
    distribution: RunDistributionV1
    home_win_probability: float
    away_win_probability: float
    tied_after_regulation_probability: float
    contract_version: str = PREDICTION_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise PredictionsContractError("edge_event_id identity mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise PredictionsContractError("daily_mlb_game_id identity mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
        ):
            raise PredictionsContractError("prediction teams must be canonical MLB IDs")
        if self.away_team_id == self.home_team_id:
            raise PredictionsContractError("prediction teams must differ")
        for name in (
            "upstream_model_feature_game_checksum",
            "model_manifest_checksum",
            "model_input_checksum",
        ):
            object.__setattr__(
                self,
                name,
                _sha256(getattr(self, name), name),
            )
        object.__setattr__(self, "model_id", _required_text(self.model_id, "model_id"))
        object.__setattr__(
            self,
            "model_version",
            _required_text(self.model_version, "model_version"),
        )
        if self.recommendation_eligible and (
            self.deployment_status is not ModelDeploymentStatus.PRODUCTION
            or self.calibration_status is not ModelCalibrationStatus.CALIBRATED
        ):
            raise PredictionsContractError(
                "recommendation-eligible prediction must be production and calibrated"
            )
        codes = tuple(
            sorted(
                {
                    _required_text(value, "quality issue code")
                    for value in self.quality_issue_codes
                }
            )
        )
        object.__setattr__(self, "quality_issue_codes", codes)
        if self.market_reference_checksum is not None:
            object.__setattr__(
                self,
                "market_reference_checksum",
                _sha256(self.market_reference_checksum, "market_reference_checksum"),
            )
        used = tuple(self.used_feature_names)
        imputed = tuple(self.imputed_feature_names)
        if len(set(used)) != len(used):
            raise PredictionsContractError("used_feature_names contains duplicates")
        if len(set(imputed)) != len(imputed):
            raise PredictionsContractError("imputed_feature_names contains duplicates")
        if any(name not in FEATURE_NAME_SET_V1 for name in (*used, *imputed)):
            raise PredictionsContractError("prediction feature name is not in schema")
        if not set(imputed).issubset(set(used)):
            raise PredictionsContractError(
                "imputed_feature_names must be a subset of used_feature_names"
            )
        object.__setattr__(self, "used_feature_names", used)
        object.__setattr__(self, "imputed_feature_names", imputed)
        contributions = tuple(self.contributions)
        if tuple(item.feature_name for item in contributions) != used:
            raise PredictionsContractError(
                "contribution order must exactly match used_feature_names"
            )
        if tuple(
            item.feature_name for item in contributions if item.was_imputed
        ) != imputed:
            raise PredictionsContractError(
                "contribution imputation state disagrees with imputed_feature_names"
            )
        object.__setattr__(self, "contributions", contributions)
        expected_input_state = (
            PredictionInputState.PRIOR_ONLY
            if not used or len(imputed) == len(used)
            else PredictionInputState.IMPUTED
            if imputed
            else PredictionInputState.COMPLETE
        )
        if self.input_state is not expected_input_state:
            raise PredictionsContractError(
                "input_state disagrees with model-term missingness"
            )
        home = _probability(self.home_win_probability, "home_win_probability")
        away = _probability(self.away_win_probability, "away_win_probability")
        tie = _probability(
            self.tied_after_regulation_probability,
            "tied_after_regulation_probability",
        )
        if not math.isclose(home + away, 1.0, abs_tol=1e-10):
            raise PredictionsContractError(
                "decisive home/away win probabilities must sum to one"
            )
        if home <= 0.0 or away <= 0.0:
            raise PredictionsContractError(
                "decisive win probabilities must both be positive"
            )
        object.__setattr__(self, "home_win_probability", home)
        object.__setattr__(self, "away_win_probability", away)
        object.__setattr__(self, "tied_after_regulation_probability", tie)
        if self.contract_version != PREDICTION_GAME_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported prediction-game contract")

    @property
    def expected_home_runs(self) -> float:
        return self.distribution.home_run_rate

    @property
    def expected_away_runs(self) -> float:
        return self.distribution.away_run_rate

    @property
    def expected_total_runs(self) -> float:
        return self.expected_home_runs + self.expected_away_runs

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "away_win_probability": self.away_win_probability,
            "calibration_status": self.calibration_status.value,
            "contract_version": self.contract_version,
            "contributions": [item.as_dict() for item in self.contributions],
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "deployment_status": self.deployment_status.value,
            "distribution": self.distribution.as_dict(),
            "edge_event_id": self.edge_event_id,
            "expected_away_runs": self.expected_away_runs,
            "expected_home_runs": self.expected_home_runs,
            "expected_total_runs": self.expected_total_runs,
            "home_team_id": self.home_team_id,
            "home_win_probability": self.home_win_probability,
            "imputed_feature_names": list(self.imputed_feature_names),
            "input_state": self.input_state.value,
            "market_reference_checksum": self.market_reference_checksum,
            "model_id": self.model_id,
            "model_input_checksum": self.model_input_checksum,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_version": self.model_version,
            "quality_disposition": self.quality_disposition.value,
            "quality_issue_codes": list(self.quality_issue_codes),
            "recommendation_eligible": self.recommendation_eligible,
            "source_game_id": self.source_game_id,
            "tied_after_regulation_probability": self.tied_after_regulation_probability,
            "upstream_model_feature_game_checksum": self.upstream_model_feature_game_checksum,
            "used_feature_names": list(self.used_feature_names),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PredictionsV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_model_feature_set_checksum: str
    model_manifest: PredictionModelManifestV1
    games: tuple[GamePredictionV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    feature_schema_version: str = MODEL_FEATURE_SCHEMA_VERSION
    feature_schema_checksum: str = MODEL_FEATURE_SCHEMA_CHECKSUM
    contract_version: str = PREDICTIONS_CONTRACT_VERSION
    sport: str = PREDICTIONS_SPORT
    league: str = PREDICTIONS_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(
            self,
            "as_of_time",
            _aware_utc(self.as_of_time, "as_of_time"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "upstream_model_feature_set_checksum",
            _sha256(
                self.upstream_model_feature_set_checksum,
                "upstream_model_feature_set_checksum",
            ),
        )
        if self.feature_schema_version != MODEL_FEATURE_SCHEMA_VERSION:
            raise PredictionsContractError("unsupported feature schema version")
        if self.feature_schema_checksum != MODEL_FEATURE_SCHEMA_CHECKSUM:
            raise PredictionsContractError("feature schema checksum mismatch")
        if self.contract_version != PREDICTIONS_CONTRACT_VERSION:
            raise PredictionsContractError("unsupported Predictions V1 contract")
        if self.sport != "MLB" or self.league != "MLB":
            raise PredictionsContractError("sport and league must both be MLB")
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise PredictionsContractError("PredictionsV1 contains duplicate games")
        if any(
            game.model_manifest_checksum != self.model_manifest.checksum
            or game.model_id != self.model_manifest.model_id
            or game.model_version != self.model_manifest.model_version
            or game.deployment_status is not self.model_manifest.deployment_status
            or game.calibration_status is not self.model_manifest.calibration_status
            or game.recommendation_eligible
            != self.model_manifest.recommendation_eligible
            for game in games
        ):
            raise PredictionsContractError(
                "prediction game model lineage disagrees with manifest"
            )
        object.__setattr__(self, "games", games)
        payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured, preserve_field_names=("key",)) != payload:
            raise PredictionsContractError(
                "PredictionsV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "feature_schema_checksum": self.feature_schema_checksum,
            "feature_schema_version": self.feature_schema_version,
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "model_manifest": self.model_manifest.as_dict(),
            "observed_at": self.observed_at.isoformat(),
            "requested_date": self.requested_date,
            "sport": self.sport,
            "upstream_model_feature_set_checksum": self.upstream_model_feature_set_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
