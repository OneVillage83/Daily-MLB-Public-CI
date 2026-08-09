from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from statistics import fmean

from app.daily_slate.contracts import canonical_sha256
from app.model_feature_set.schema import FEATURE_NAME_SET_V1
from app.predictions.contracts import (
    ModelCalibrationStatus,
    ModelDeploymentStatus,
    PredictionFeatureTermV1,
    PredictionModelManifestV1,
)
from app.predictions.probability import build_run_distribution, decisive_moneyline_probabilities
from app.team_aliases import CANONICAL_TEAM_KEYS

FULL_GAME_SCORING_TRAINER_VERSION = "DSE_MLB_EMPIRICAL_POISSON_TRAINER_V1"
HISTORICAL_FEATURE_VALUE_CONTRACT_VERSION = "DSE_MLB_HISTORICAL_FEATURE_VALUE_V1"
HISTORICAL_SCORING_ROW_CONTRACT_VERSION = "DSE_MLB_HISTORICAL_SCORING_ROW_V1"
SCORING_TRAINING_DATASET_CONTRACT_VERSION = "DSE_MLB_SCORING_TRAINING_DATASET_V1"
SCORING_MODEL_EVALUATION_CONTRACT_VERSION = "DSE_MLB_SCORING_MODEL_EVALUATION_V1"
SCORING_TRAINING_RESULT_CONTRACT_VERSION = "DSE_MLB_SCORING_TRAINING_RESULT_V1"


class ScoringTrainingError(ValueError):
    """Raised when empirical full-game scoring training evidence is invalid."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ScoringTrainingError(f"{name} must be non-empty trimmed text")
    return value


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScoringTrainingError(f"{name} must be finite numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ScoringTrainingError(f"{name} must be finite numeric")
    return numeric


def _nonnegative(value: object, name: str) -> float:
    numeric = _finite(value, name)
    if numeric < 0.0:
        raise ScoringTrainingError(f"{name} must be nonnegative")
    return numeric


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ScoringTrainingError(f"{name} must be lowercase SHA-256")
    return text


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScoringTrainingError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class HistoricalFeatureValueV1:
    feature_name: str
    value: float | None
    contract_version: str = HISTORICAL_FEATURE_VALUE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.feature_name not in FEATURE_NAME_SET_V1:
            raise ScoringTrainingError(f"unsupported historical feature: {self.feature_name}")
        if self.value is not None:
            object.__setattr__(self, "value", _finite(self.value, "historical feature value"))
        if self.contract_version != HISTORICAL_FEATURE_VALUE_CONTRACT_VERSION:
            raise ScoringTrainingError("unsupported historical feature-value contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "feature_name": self.feature_name,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class HistoricalScoringRowV1:
    source_game_id: str
    game_date: date
    away_team_id: str
    home_team_id: str
    away_runs: int
    home_runs: int
    features: tuple[HistoricalFeatureValueV1, ...]
    contract_version: str = HISTORICAL_SCORING_ROW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if not isinstance(self.game_date, date):
            raise ScoringTrainingError("game_date must be a date")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise ScoringTrainingError("historical scoring team identity is invalid")
        for name in ("away_runs", "home_runs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScoringTrainingError(f"{name} must be a nonnegative integer")
        features = tuple(self.features)
        names = [item.feature_name for item in features]
        if names != sorted(names) or len(names) != len(set(names)):
            raise ScoringTrainingError("historical features must be unique and sorted by feature_name")
        object.__setattr__(self, "features", features)
        if self.contract_version != HISTORICAL_SCORING_ROW_CONTRACT_VERSION:
            raise ScoringTrainingError("unsupported historical scoring-row contract")

    def feature_map(self) -> dict[str, float | None]:
        return {item.feature_name: item.value for item in self.features}

    def as_dict(self) -> dict[str, object]:
        return {
            "away_runs": self.away_runs,
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "features": [item.as_dict() for item in self.features],
            "game_date": self.game_date.isoformat(),
            "home_runs": self.home_runs,
            "home_team_id": self.home_team_id,
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class ScoringTrainingDatasetV1:
    rows: tuple[HistoricalScoringRowV1, ...]
    contract_version: str = SCORING_TRAINING_DATASET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        rows = tuple(self.rows)
        keys = [(row.game_date, row.source_game_id) for row in rows]
        if keys != sorted(keys):
            raise ScoringTrainingError("training rows must be sorted chronologically by date and source_game_id")
        if len({row.source_game_id for row in rows}) != len(rows):
            raise ScoringTrainingError("training dataset contains duplicate source_game_id values")
        object.__setattr__(self, "rows", rows)
        if self.contract_version != SCORING_TRAINING_DATASET_CONTRACT_VERSION:
            raise ScoringTrainingError("unsupported scoring training-dataset contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "rows": [row.as_dict() for row in self.rows],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class ScoringModelEvaluationV1:
    training_row_count: int
    validation_row_count: int
    validation_start: date
    validation_end: date
    model_poisson_nll_per_team: float
    baseline_poisson_nll_per_team: float
    home_runs_mae: float
    away_runs_mae: float
    total_runs_mae: float
    run_margin_mae: float
    moneyline_brier_score: float
    decisive_validation_game_count: int
    baseline_home_run_rate: float
    baseline_away_run_rate: float
    contract_version: str = SCORING_MODEL_EVALUATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("training_row_count", "validation_row_count", "decisive_validation_game_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScoringTrainingError(f"{name} must be a nonnegative integer")
        if self.training_row_count == 0 or self.validation_row_count == 0:
            raise ScoringTrainingError("training and validation inventories must be non-empty")
        if self.decisive_validation_game_count > self.validation_row_count:
            raise ScoringTrainingError("decisive validation count exceeds validation inventory")
        if self.validation_end < self.validation_start:
            raise ScoringTrainingError("validation date range is invalid")
        for name in (
            "model_poisson_nll_per_team",
            "baseline_poisson_nll_per_team",
            "home_runs_mae",
            "away_runs_mae",
            "total_runs_mae",
            "run_margin_mae",
            "moneyline_brier_score",
            "baseline_home_run_rate",
            "baseline_away_run_rate",
        ):
            object.__setattr__(self, name, _nonnegative(getattr(self, name), name))
        if self.moneyline_brier_score > 1.0:
            raise ScoringTrainingError("moneyline_brier_score must be at most one")
        if self.contract_version != SCORING_MODEL_EVALUATION_CONTRACT_VERSION:
            raise ScoringTrainingError("unsupported scoring model-evaluation contract")

    @property
    def holdout_nll_delta(self) -> float:
        return self.model_poisson_nll_per_team - self.baseline_poisson_nll_per_team

    @property
    def model_improves_holdout_nll(self) -> bool:
        return self.holdout_nll_delta < 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "away_runs_mae": self.away_runs_mae,
            "baseline_away_run_rate": self.baseline_away_run_rate,
            "baseline_home_run_rate": self.baseline_home_run_rate,
            "baseline_poisson_nll_per_team": self.baseline_poisson_nll_per_team,
            "contract_version": self.contract_version,
            "decisive_validation_game_count": self.decisive_validation_game_count,
            "holdout_nll_delta": self.holdout_nll_delta,
            "home_runs_mae": self.home_runs_mae,
            "model_improves_holdout_nll": self.model_improves_holdout_nll,
            "model_poisson_nll_per_team": self.model_poisson_nll_per_team,
            "moneyline_brier_score": self.moneyline_brier_score,
            "run_margin_mae": self.run_margin_mae,
            "total_runs_mae": self.total_runs_mae,
            "training_row_count": self.training_row_count,
            "validation_end": self.validation_end.isoformat(),
            "validation_row_count": self.validation_row_count,
            "validation_start": self.validation_start.isoformat(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class ScoringTrainingResultV1:
    manifest: PredictionModelManifestV1
    evaluation: ScoringModelEvaluationV1
    full_dataset_checksum: str
    training_subset_checksum: str
    validation_subset_checksum: str
    feature_names: tuple[str, ...]
    train_through: date
    validation_start: date
    validation_end: date
    home_fit_iterations: int
    away_fit_iterations: int
    home_fit_converged: bool
    away_fit_converged: bool
    trainer_version: str = FULL_GAME_SCORING_TRAINER_VERSION
    contract_version: str = SCORING_TRAINING_RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("full_dataset_checksum", "training_subset_checksum", "validation_subset_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        features = tuple(self.feature_names)
        if not features or features != tuple(sorted(set(features))):
            raise ScoringTrainingError("training result feature_names must be non-empty, unique, and sorted")
        if any(feature not in FEATURE_NAME_SET_V1 for feature in features):
            raise ScoringTrainingError("training result contains unsupported features")
        object.__setattr__(self, "feature_names", features)
        if not self.train_through < self.validation_start <= self.validation_end:
            raise ScoringTrainingError("training result temporal split is invalid")
        for name in ("home_fit_iterations", "away_fit_iterations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ScoringTrainingError(f"{name} must be positive")
        for name in ("home_fit_converged", "away_fit_converged"):
            if not isinstance(getattr(self, name), bool):
                raise ScoringTrainingError(f"{name} must be boolean")
        if self.manifest.deployment_status is not ModelDeploymentStatus.EVALUATION:
            raise ScoringTrainingError("empirical training result must emit an evaluation manifest")
        if self.manifest.calibration_status is not ModelCalibrationStatus.UNCALIBRATED:
            raise ScoringTrainingError("empirical training result must remain uncalibrated")
        if self.manifest.recommendation_eligible:
            raise ScoringTrainingError("empirical training result cannot be recommendation eligible")
        if self.manifest.training_dataset_checksum != self.training_subset_checksum:
            raise ScoringTrainingError("manifest training dataset checksum does not match training subset")
        if self.manifest.training_code_version != self.trainer_version:
            raise ScoringTrainingError("manifest training code version does not match trainer")
        if self.trainer_version != FULL_GAME_SCORING_TRAINER_VERSION:
            raise ScoringTrainingError("unsupported full-game scoring trainer version")
        if self.contract_version != SCORING_TRAINING_RESULT_CONTRACT_VERSION:
            raise ScoringTrainingError("unsupported scoring training-result contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "away_fit_converged": self.away_fit_converged,
            "away_fit_iterations": self.away_fit_iterations,
            "contract_version": self.contract_version,
            "evaluation": self.evaluation.as_dict(),
            "feature_names": list(self.feature_names),
            "full_dataset_checksum": self.full_dataset_checksum,
            "home_fit_converged": self.home_fit_converged,
            "home_fit_iterations": self.home_fit_iterations,
            "manifest": self.manifest.as_dict(),
            "train_through": self.train_through.isoformat(),
            "trainer_version": self.trainer_version,
            "training_subset_checksum": self.training_subset_checksum,
            "validation_end": self.validation_end.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "validation_subset_checksum": self.validation_subset_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def _rows_checksum(rows: tuple[HistoricalScoringRowV1, ...]) -> str:
    return canonical_sha256([row.as_dict() for row in rows])


def _feature_statistics(
    rows: tuple[HistoricalScoringRowV1, ...],
    feature_names: tuple[str, ...],
) -> tuple[tuple[float, float], ...]:
    result: list[tuple[float, float]] = []
    feature_maps = [row.feature_map() for row in rows]
    for feature_name in feature_names:
        observed = [mapping[feature_name] for mapping in feature_maps if mapping.get(feature_name) is not None]
        numeric = [float(value) for value in observed if value is not None]
        if not numeric:
            result.append((0.0, 1.0))
            continue
        center = fmean(numeric)
        variance = fmean((value - center) ** 2 for value in numeric)
        scale = math.sqrt(variance)
        if scale < 1e-12:
            scale = 1.0
        result.append((center, scale))
    return tuple(result)


def _design_matrix(
    rows: tuple[HistoricalScoringRowV1, ...],
    feature_names: tuple[str, ...],
    statistics: tuple[tuple[float, float], ...],
) -> list[list[float]]:
    matrix: list[list[float]] = []
    for row in rows:
        mapping = row.feature_map()
        vector = [1.0]
        for feature_name, (center, scale) in zip(feature_names, statistics, strict=True):
            raw = mapping.get(feature_name)
            effective = center if raw is None else raw
            vector.append((effective - center) / scale)
        matrix.append(vector)
    return matrix


def _solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    size = len(rhs)
    augmented = [matrix[row][:] + [rhs[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ScoringTrainingError("Poisson fit normal equations are singular")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0.0:
                continue
            augmented[row] = [
                left - factor * right
                for left, right in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[row][-1] for row in range(size)]


def _fit_poisson_log_rate(
    design: list[list[float]],
    outcomes: list[int],
    *,
    minimum_run_rate: float,
    maximum_run_rate: float,
    ridge_penalty: float,
    maximum_iterations: int,
    convergence_tolerance: float,
) -> tuple[list[float], int, bool]:
    if not design or len(design) != len(outcomes):
        raise ScoringTrainingError("Poisson fit design and outcome inventories must agree")
    dimension = len(design[0])
    if any(len(row) != dimension for row in design):
        raise ScoringTrainingError("Poisson fit design matrix is ragged")
    mean_runs = max(minimum_run_rate, min(maximum_run_rate, fmean(outcomes)))
    coefficients = [math.log(mean_runs)] + [0.0] * (dimension - 1)
    minimum_log_rate = math.log(minimum_run_rate)
    maximum_log_rate = math.log(maximum_run_rate)
    converged = False
    for iteration in range(1, maximum_iterations + 1):
        gradient = [0.0] * dimension
        information = [[0.0] * dimension for _ in range(dimension)]
        for vector, observed in zip(design, outcomes, strict=True):
            eta = sum(coefficient * value for coefficient, value in zip(coefficients, vector, strict=True))
            eta = min(maximum_log_rate, max(minimum_log_rate, eta))
            expected = math.exp(eta)
            residual = observed - expected
            for left in range(dimension):
                gradient[left] += vector[left] * residual
                for right in range(dimension):
                    information[left][right] += expected * vector[left] * vector[right]
        for index in range(1, dimension):
            gradient[index] -= ridge_penalty * coefficients[index]
            information[index][index] += ridge_penalty
        step = _solve_linear_system(information, gradient)
        maximum_step = max(abs(value) for value in step)
        if maximum_step > 1.0:
            scale = 1.0 / maximum_step
            step = [value * scale for value in step]
            maximum_step = 1.0
        coefficients = [value + delta for value, delta in zip(coefficients, step, strict=True)]
        if maximum_step < convergence_tolerance:
            converged = True
            return coefficients, iteration, converged
    return coefficients, maximum_iterations, converged


def _rate(
    manifest: PredictionModelManifestV1,
    row: HistoricalScoringRowV1,
    *,
    home: bool,
) -> float:
    mapping = row.feature_map()
    log_rate = manifest.home_log_rate_intercept if home else manifest.away_log_rate_intercept
    for term in manifest.terms:
        raw = mapping.get(term.feature_name)
        effective = term.missing_value if raw is None else raw
        standardized = (effective - term.center) / term.scale
        coefficient = term.home_coefficient if home else term.away_coefficient
        log_rate += standardized * coefficient
    return min(manifest.maximum_run_rate, max(manifest.minimum_run_rate, math.exp(log_rate)))


def _poisson_nll(observed: int, expected: float) -> float:
    return expected - observed * math.log(expected) + math.lgamma(observed + 1.0)


def _evaluate(
    manifest: PredictionModelManifestV1,
    training_rows: tuple[HistoricalScoringRowV1, ...],
    validation_rows: tuple[HistoricalScoringRowV1, ...],
    *,
    validation_start: date,
    validation_end: date,
) -> ScoringModelEvaluationV1:
    baseline_home = max(manifest.minimum_run_rate, min(manifest.maximum_run_rate, fmean(row.home_runs for row in training_rows)))
    baseline_away = max(manifest.minimum_run_rate, min(manifest.maximum_run_rate, fmean(row.away_runs for row in training_rows)))
    model_nll = 0.0
    baseline_nll = 0.0
    home_errors: list[float] = []
    away_errors: list[float] = []
    total_errors: list[float] = []
    margin_errors: list[float] = []
    brier_terms: list[float] = []
    for row in validation_rows:
        home_rate = _rate(manifest, row, home=True)
        away_rate = _rate(manifest, row, home=False)
        model_nll += _poisson_nll(row.home_runs, home_rate) + _poisson_nll(row.away_runs, away_rate)
        baseline_nll += _poisson_nll(row.home_runs, baseline_home) + _poisson_nll(row.away_runs, baseline_away)
        home_errors.append(abs(row.home_runs - home_rate))
        away_errors.append(abs(row.away_runs - away_rate))
        total_errors.append(abs((row.home_runs + row.away_runs) - (home_rate + away_rate)))
        margin_errors.append(abs((row.home_runs - row.away_runs) - (home_rate - away_rate)))
        if row.home_runs != row.away_runs:
            distribution = build_run_distribution(
                home_run_rate=home_rate,
                away_run_rate=away_rate,
                maximum_run_support=manifest.maximum_run_support,
            )
            home_win, _, _ = decisive_moneyline_probabilities(distribution)
            actual = 1.0 if row.home_runs > row.away_runs else 0.0
            brier_terms.append((home_win - actual) ** 2)
    denominator = 2.0 * len(validation_rows)
    return ScoringModelEvaluationV1(
        training_row_count=len(training_rows),
        validation_row_count=len(validation_rows),
        validation_start=validation_start,
        validation_end=validation_end,
        model_poisson_nll_per_team=model_nll / denominator,
        baseline_poisson_nll_per_team=baseline_nll / denominator,
        home_runs_mae=fmean(home_errors),
        away_runs_mae=fmean(away_errors),
        total_runs_mae=fmean(total_errors),
        run_margin_mae=fmean(margin_errors),
        moneyline_brier_score=0.0 if not brier_terms else fmean(brier_terms),
        decisive_validation_game_count=len(brier_terms),
        baseline_home_run_rate=baseline_home,
        baseline_away_run_rate=baseline_away,
    )


def fit_empirical_scoring_model(
    dataset: ScoringTrainingDatasetV1,
    *,
    feature_names: tuple[str, ...],
    train_through: date,
    validation_start: date,
    validation_end: date,
    model_id: str,
    model_version: str,
    trained_at: datetime,
    minimum_run_rate: float = 0.25,
    maximum_run_rate: float = 15.0,
    ridge_penalty: float = 1e-4,
    maximum_iterations: int = 100,
    convergence_tolerance: float = 1e-8,
    maximum_run_support: int = 60,
    tail_tolerance: float = 1e-12,
) -> ScoringTrainingResultV1:
    selected_features = tuple(sorted(set(feature_names)))
    if not selected_features or any(feature not in FEATURE_NAME_SET_V1 for feature in selected_features):
        raise ScoringTrainingError("feature_names must contain supported ModelFeatureSet V1 names")
    if not train_through < validation_start <= validation_end:
        raise ScoringTrainingError("temporal training split must keep holdout strictly after training")
    minimum_rate = _finite(minimum_run_rate, "minimum_run_rate")
    maximum_rate = _finite(maximum_run_rate, "maximum_run_rate")
    if minimum_rate <= 0.0 or maximum_rate <= minimum_rate:
        raise ScoringTrainingError("run-rate bounds are invalid")
    ridge = _nonnegative(ridge_penalty, "ridge_penalty")
    tolerance = _finite(convergence_tolerance, "convergence_tolerance")
    if tolerance <= 0.0:
        raise ScoringTrainingError("convergence_tolerance must be positive")
    if isinstance(maximum_iterations, bool) or not isinstance(maximum_iterations, int) or maximum_iterations < 1:
        raise ScoringTrainingError("maximum_iterations must be a positive integer")
    if isinstance(maximum_run_support, bool) or not isinstance(maximum_run_support, int) or maximum_run_support < 20:
        raise ScoringTrainingError("maximum_run_support must be at least 20")
    training_rows = tuple(row for row in dataset.rows if row.game_date <= train_through)
    validation_rows = tuple(row for row in dataset.rows if validation_start <= row.game_date <= validation_end)
    if len(training_rows) < len(selected_features) + 2:
        raise ScoringTrainingError("training inventory is too small for the selected feature dimension")
    if not validation_rows:
        raise ScoringTrainingError("validation inventory is empty")
    statistics = _feature_statistics(training_rows, selected_features)
    training_design = _design_matrix(training_rows, selected_features, statistics)
    home_coefficients, home_iterations, home_converged = _fit_poisson_log_rate(
        training_design,
        [row.home_runs for row in training_rows],
        minimum_run_rate=minimum_rate,
        maximum_run_rate=maximum_rate,
        ridge_penalty=ridge,
        maximum_iterations=maximum_iterations,
        convergence_tolerance=tolerance,
    )
    away_coefficients, away_iterations, away_converged = _fit_poisson_log_rate(
        training_design,
        [row.away_runs for row in training_rows],
        minimum_run_rate=minimum_rate,
        maximum_run_rate=maximum_rate,
        ridge_penalty=ridge,
        maximum_iterations=maximum_iterations,
        convergence_tolerance=tolerance,
    )
    terms = tuple(
        PredictionFeatureTermV1(
            feature_name=feature_name,
            center=center,
            scale=scale,
            missing_value=center,
            home_coefficient=home_coefficients[index + 1],
            away_coefficient=away_coefficients[index + 1],
        )
        for index, (feature_name, (center, scale)) in enumerate(zip(selected_features, statistics, strict=True))
    )
    training_subset_checksum = _rows_checksum(training_rows)
    manifest = PredictionModelManifestV1(
        model_id=_text(model_id, "model_id"),
        model_version=_text(model_version, "model_version"),
        model_kind="independent_poisson_log_rate",
        deployment_status=ModelDeploymentStatus.EVALUATION,
        calibration_status=ModelCalibrationStatus.UNCALIBRATED,
        recommendation_eligible=False,
        parameter_origin="empirical_temporal_poisson_fit",
        home_log_rate_intercept=home_coefficients[0],
        away_log_rate_intercept=away_coefficients[0],
        minimum_run_rate=minimum_rate,
        maximum_run_rate=maximum_rate,
        terms=terms,
        tail_tolerance=tail_tolerance,
        maximum_run_support=maximum_run_support,
        training_dataset_checksum=training_subset_checksum,
        training_code_version=FULL_GAME_SCORING_TRAINER_VERSION,
        trained_at=_aware_utc(trained_at, "trained_at"),
    )
    evaluation = _evaluate(
        manifest,
        training_rows,
        validation_rows,
        validation_start=validation_start,
        validation_end=validation_end,
    )
    return ScoringTrainingResultV1(
        manifest=manifest,
        evaluation=evaluation,
        full_dataset_checksum=dataset.checksum,
        training_subset_checksum=training_subset_checksum,
        validation_subset_checksum=_rows_checksum(validation_rows),
        feature_names=selected_features,
        train_through=train_through,
        validation_start=validation_start,
        validation_end=validation_end,
        home_fit_iterations=home_iterations,
        away_fit_iterations=away_iterations,
        home_fit_converged=home_converged,
        away_fit_converged=away_converged,
    )
