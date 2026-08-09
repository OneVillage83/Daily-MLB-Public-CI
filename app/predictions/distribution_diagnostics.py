from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean

from app.daily_slate.contracts import canonical_sha256
from app.predictions.contracts import PredictionModelManifestV1
from app.predictions.game_total import full_game_total_runs_distribution
from app.predictions.market_foundation import DiscreteDistributionV1
from app.predictions.probability import build_run_distribution
from app.predictions.run_line import full_game_run_margin_distribution
from app.predictions.training import (
    HistoricalScoringRowV1,
    ScoringTrainingDatasetV1,
    ScoringTrainingResultV1,
)

FULL_GAME_DISTRIBUTION_DIAGNOSTICS_VERSION = "DSE_MLB_FULL_GAME_DISTRIBUTION_DIAGNOSTICS_V1"
DISTRIBUTION_LOG_PROBABILITY_FLOOR = 1e-15


class DistributionDiagnosticsError(ValueError):
    """Raised when held-out distribution diagnostics cannot be reproduced safely."""


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DistributionDiagnosticsError(f"{name} must be finite numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        raise DistributionDiagnosticsError(f"{name} must be finite and nonnegative")
    return numeric


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise DistributionDiagnosticsError(f"{name} must be lowercase SHA-256")
    return value


def _rows_checksum(rows: tuple[HistoricalScoringRowV1, ...]) -> str:
    return canonical_sha256([row.as_dict() for row in rows])


def _run_rates(
    manifest: PredictionModelManifestV1,
    row: HistoricalScoringRowV1,
) -> tuple[float, float]:
    mapping = row.feature_map()
    home_log_rate = manifest.home_log_rate_intercept
    away_log_rate = manifest.away_log_rate_intercept
    for term in manifest.terms:
        raw = mapping.get(term.feature_name)
        effective = term.missing_value if raw is None else raw
        standardized = (effective - term.center) / term.scale
        home_log_rate += standardized * term.home_coefficient
        away_log_rate += standardized * term.away_coefficient
    home_rate = min(
        manifest.maximum_run_rate,
        max(manifest.minimum_run_rate, math.exp(home_log_rate)),
    )
    away_rate = min(
        manifest.maximum_run_rate,
        max(manifest.minimum_run_rate, math.exp(away_log_rate)),
    )
    return home_rate, away_rate


def _resolved_mass(distribution: DiscreteDistributionV1) -> float:
    resolved = 1.0 - distribution.unresolved_probability
    if resolved <= 0.0:
        raise DistributionDiagnosticsError("distribution contains no resolved probability mass")
    return resolved


def _resolved_probability(distribution: DiscreteDistributionV1, observed: int) -> float:
    resolved = _resolved_mass(distribution)
    probability = next(
        (item.probability for item in distribution.outcomes if item.value == observed),
        0.0,
    )
    return probability / resolved


def _log_loss(distribution: DiscreteDistributionV1, observed: int) -> float:
    probability = max(
        DISTRIBUTION_LOG_PROBABILITY_FLOOR,
        _resolved_probability(distribution, observed),
    )
    return -math.log(probability)


def _crps(distribution: DiscreteDistributionV1, observed: int) -> float:
    resolved = _resolved_mass(distribution)
    probabilities = {item.value: item.probability / resolved for item in distribution.outcomes}
    support = tuple(probabilities)
    if not support:
        raise DistributionDiagnosticsError("distribution has empty support")
    lower = min(min(support), observed)
    upper = max(max(support), observed)
    cumulative = 0.0
    score = 0.0
    for value in range(lower, upper + 1):
        cumulative += probabilities.get(value, 0.0)
        observed_cdf = 1.0 if observed <= value else 0.0
        score += (cumulative - observed_cdf) ** 2
    return score


@dataclass(frozen=True, slots=True)
class FullGameDistributionDiagnosticsV1:
    training_result_checksum: str
    validation_subset_checksum: str
    validation_game_count: int
    model_total_log_loss: float
    baseline_total_log_loss: float
    model_total_crps: float
    baseline_total_crps: float
    model_run_margin_log_loss: float
    baseline_run_margin_log_loss: float
    model_run_margin_crps: float
    baseline_run_margin_crps: float
    maximum_model_unresolved_probability: float
    maximum_baseline_unresolved_probability: float
    log_probability_floor: float = DISTRIBUTION_LOG_PROBABILITY_FLOOR
    contract_version: str = FULL_GAME_DISTRIBUTION_DIAGNOSTICS_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "training_result_checksum",
            _sha(self.training_result_checksum, "training_result_checksum"),
        )
        object.__setattr__(
            self,
            "validation_subset_checksum",
            _sha(self.validation_subset_checksum, "validation_subset_checksum"),
        )
        if (
            isinstance(self.validation_game_count, bool)
            or not isinstance(self.validation_game_count, int)
            or self.validation_game_count < 1
        ):
            raise DistributionDiagnosticsError("validation_game_count must be positive")
        for name in (
            "model_total_log_loss",
            "baseline_total_log_loss",
            "model_total_crps",
            "baseline_total_crps",
            "model_run_margin_log_loss",
            "baseline_run_margin_log_loss",
            "model_run_margin_crps",
            "baseline_run_margin_crps",
            "maximum_model_unresolved_probability",
            "maximum_baseline_unresolved_probability",
            "log_probability_floor",
        ):
            object.__setattr__(self, name, _finite_nonnegative(getattr(self, name), name))
        if not 0.0 <= self.maximum_model_unresolved_probability <= 1.0:
            raise DistributionDiagnosticsError("maximum_model_unresolved_probability must be a probability")
        if not 0.0 <= self.maximum_baseline_unresolved_probability <= 1.0:
            raise DistributionDiagnosticsError("maximum_baseline_unresolved_probability must be a probability")
        if self.log_probability_floor != DISTRIBUTION_LOG_PROBABILITY_FLOOR:
            raise DistributionDiagnosticsError("unsupported distribution log-probability floor")
        if self.contract_version != FULL_GAME_DISTRIBUTION_DIAGNOSTICS_VERSION:
            raise DistributionDiagnosticsError("unsupported full-game distribution diagnostics contract")

    @property
    def total_log_loss_delta(self) -> float:
        return self.model_total_log_loss - self.baseline_total_log_loss

    @property
    def total_crps_delta(self) -> float:
        return self.model_total_crps - self.baseline_total_crps

    @property
    def run_margin_log_loss_delta(self) -> float:
        return self.model_run_margin_log_loss - self.baseline_run_margin_log_loss

    @property
    def run_margin_crps_delta(self) -> float:
        return self.model_run_margin_crps - self.baseline_run_margin_crps

    @property
    def model_improves_total_log_loss(self) -> bool:
        return self.total_log_loss_delta < 0.0

    @property
    def model_improves_total_crps(self) -> bool:
        return self.total_crps_delta < 0.0

    @property
    def model_improves_run_margin_log_loss(self) -> bool:
        return self.run_margin_log_loss_delta < 0.0

    @property
    def model_improves_run_margin_crps(self) -> bool:
        return self.run_margin_crps_delta < 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "baseline_run_margin_crps": self.baseline_run_margin_crps,
            "baseline_run_margin_log_loss": self.baseline_run_margin_log_loss,
            "baseline_total_crps": self.baseline_total_crps,
            "baseline_total_log_loss": self.baseline_total_log_loss,
            "contract_version": self.contract_version,
            "log_probability_floor": self.log_probability_floor,
            "maximum_baseline_unresolved_probability": self.maximum_baseline_unresolved_probability,
            "maximum_model_unresolved_probability": self.maximum_model_unresolved_probability,
            "model_improves_run_margin_crps": self.model_improves_run_margin_crps,
            "model_improves_run_margin_log_loss": self.model_improves_run_margin_log_loss,
            "model_improves_total_crps": self.model_improves_total_crps,
            "model_improves_total_log_loss": self.model_improves_total_log_loss,
            "model_run_margin_crps": self.model_run_margin_crps,
            "model_run_margin_log_loss": self.model_run_margin_log_loss,
            "model_total_crps": self.model_total_crps,
            "model_total_log_loss": self.model_total_log_loss,
            "run_margin_crps_delta": self.run_margin_crps_delta,
            "run_margin_log_loss_delta": self.run_margin_log_loss_delta,
            "total_crps_delta": self.total_crps_delta,
            "total_log_loss_delta": self.total_log_loss_delta,
            "training_result_checksum": self.training_result_checksum,
            "validation_game_count": self.validation_game_count,
            "validation_subset_checksum": self.validation_subset_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def evaluate_full_game_distribution_diagnostics(
    dataset: ScoringTrainingDatasetV1,
    training_result: ScoringTrainingResultV1,
) -> FullGameDistributionDiagnosticsV1:
    if dataset.checksum != training_result.full_dataset_checksum:
        raise DistributionDiagnosticsError("dataset checksum does not match training result")
    validation_rows = tuple(
        row
        for row in dataset.rows
        if training_result.validation_start <= row.game_date <= training_result.validation_end
    )
    if not validation_rows:
        raise DistributionDiagnosticsError("validation inventory is empty")
    validation_checksum = _rows_checksum(validation_rows)
    if validation_checksum != training_result.validation_subset_checksum:
        raise DistributionDiagnosticsError("validation subset checksum does not match training result")

    model_total_log_losses: list[float] = []
    baseline_total_log_losses: list[float] = []
    model_total_crps_values: list[float] = []
    baseline_total_crps_values: list[float] = []
    model_margin_log_losses: list[float] = []
    baseline_margin_log_losses: list[float] = []
    model_margin_crps_values: list[float] = []
    baseline_margin_crps_values: list[float] = []
    model_unresolved: list[float] = []
    baseline_unresolved: list[float] = []

    manifest = training_result.manifest
    baseline_home = training_result.evaluation.baseline_home_run_rate
    baseline_away = training_result.evaluation.baseline_away_run_rate
    for row in validation_rows:
        home_rate, away_rate = _run_rates(manifest, row)
        model_score = build_run_distribution(
            home_run_rate=home_rate,
            away_run_rate=away_rate,
            maximum_run_support=manifest.maximum_run_support,
        )
        baseline_score = build_run_distribution(
            home_run_rate=baseline_home,
            away_run_rate=baseline_away,
            maximum_run_support=manifest.maximum_run_support,
        )
        model_total = full_game_total_runs_distribution(model_score)
        baseline_total = full_game_total_runs_distribution(baseline_score)
        model_margin = full_game_run_margin_distribution(model_score)
        baseline_margin = full_game_run_margin_distribution(baseline_score)
        observed_total = row.home_runs + row.away_runs
        observed_margin = row.home_runs - row.away_runs

        model_total_log_losses.append(_log_loss(model_total, observed_total))
        baseline_total_log_losses.append(_log_loss(baseline_total, observed_total))
        model_total_crps_values.append(_crps(model_total, observed_total))
        baseline_total_crps_values.append(_crps(baseline_total, observed_total))
        model_margin_log_losses.append(_log_loss(model_margin, observed_margin))
        baseline_margin_log_losses.append(_log_loss(baseline_margin, observed_margin))
        model_margin_crps_values.append(_crps(model_margin, observed_margin))
        baseline_margin_crps_values.append(_crps(baseline_margin, observed_margin))
        model_unresolved.append(max(model_total.unresolved_probability, model_margin.unresolved_probability))
        baseline_unresolved.append(
            max(baseline_total.unresolved_probability, baseline_margin.unresolved_probability)
        )

    return FullGameDistributionDiagnosticsV1(
        training_result_checksum=training_result.checksum,
        validation_subset_checksum=validation_checksum,
        validation_game_count=len(validation_rows),
        model_total_log_loss=fmean(model_total_log_losses),
        baseline_total_log_loss=fmean(baseline_total_log_losses),
        model_total_crps=fmean(model_total_crps_values),
        baseline_total_crps=fmean(baseline_total_crps_values),
        model_run_margin_log_loss=fmean(model_margin_log_losses),
        baseline_run_margin_log_loss=fmean(baseline_margin_log_losses),
        model_run_margin_crps=fmean(model_margin_crps_values),
        baseline_run_margin_crps=fmean(baseline_margin_crps_values),
        maximum_model_unresolved_probability=max(model_unresolved),
        maximum_baseline_unresolved_probability=max(baseline_unresolved),
    )
