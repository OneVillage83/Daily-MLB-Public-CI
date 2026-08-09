from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.contracts import GamePredictionV1, RunDistributionV1
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    ModelRolloutState,
    MultiMarketContractError,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    PredictionTargetV1,
    capability_for_family,
)

RUN_LINE_PREDICTION_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_PREDICTION_V1"
RUN_LINE_PROJECTION_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_PROJECTION_V1"
RUN_LINE_CALCULATION_VERSION = "DSE_MLB_FULL_GAME_RUN_MARGIN_V1"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MultiMarketContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise MultiMarketContractError(f"{name} must be a lowercase SHA-256")
    return text


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MultiMarketContractError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MultiMarketContractError(f"{name} must be finite")
    return number


def _probability(value: object, name: str) -> float:
    number = _finite(value, name)
    if not 0.0 <= number <= 1.0:
        raise MultiMarketContractError(f"{name} must be between zero and one")
    return number


@dataclass(frozen=True, slots=True)
class RunLineProjectionV1:
    """A line-specific query over a market-independent run-margin distribution."""

    home_spread: float
    home_cover_probability: float
    away_cover_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = RUN_LINE_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "home_spread", _finite(self.home_spread, "home_spread"))
        for name in (
            "home_cover_probability",
            "away_cover_probability",
            "push_probability",
            "unresolved_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        total = (
            self.home_cover_probability
            + self.away_cover_probability
            + self.push_probability
            + self.unresolved_probability
        )
        if abs(total - 1.0) > 1e-9:
            raise MultiMarketContractError("run-line projection probability mass must sum to one")
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha256(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != RUN_LINE_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported run-line projection contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "away_cover_probability": self.away_cover_probability,
            "contract_version": self.contract_version,
            "home_cover_probability": self.home_cover_probability,
            "home_spread": self.home_spread,
            "push_probability": self.push_probability,
            "source_distribution_checksum": self.source_distribution_checksum,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class RunLinePredictionV1:
    """V2A run-line prediction evidence; sportsbook lines remain outside Predictions."""

    source_game_id: str
    target: PredictionTargetV1
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    upstream_game_prediction_checksum: str
    upstream_model_manifest_checksum: str
    model_id: str
    model_version: str
    expected_home_margin: float
    run_margin_distribution: DiscreteDistributionV1
    calculation_version: str = RUN_LINE_CALCULATION_VERSION
    contract_version: str = RUN_LINE_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = _required_text(self.source_game_id, "source_game_id")
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.target.source_game_id != source_game_id:
            raise MultiMarketContractError("run-line target game identity mismatch")
        if (
            self.target.family is not PredictionMarketFamily.RUN_LINE
            or self.target.period is not PredictionPeriod.FULL_GAME
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise MultiMarketContractError("run-line prediction target is invalid")
        capability = capability_for_family(PredictionMarketFamily.RUN_LINE)
        if self.rollout_state is not capability.rollout_state:
            raise MultiMarketContractError("run-line rollout state disagrees with frozen capability")
        if self.recommendation_eligible is not capability.recommendation_eligible:
            raise MultiMarketContractError("run-line recommendation eligibility disagrees with frozen capability")
        if self.recommendation_eligible and self.rollout_state is not ModelRolloutState.PRODUCTION:
            raise MultiMarketContractError("only production run-line predictions may be recommendation eligible")
        for name in ("upstream_game_prediction_checksum", "upstream_model_manifest_checksum"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        object.__setattr__(self, "model_id", _required_text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _required_text(self.model_version, "model_version"))
        object.__setattr__(
            self,
            "expected_home_margin",
            _finite(self.expected_home_margin, "expected_home_margin"),
        )
        if self.run_margin_distribution.kind is not PredictionDistributionKind.FULL_GAME_RUN_MARGIN:
            raise MultiMarketContractError("run-line prediction requires full-game run-margin distribution")
        if self.calculation_version != RUN_LINE_CALCULATION_VERSION:
            raise MultiMarketContractError("unsupported run-line calculation version")
        if self.contract_version != RUN_LINE_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported run-line prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.run_margin_distribution.checksum

    def project(self, home_spread: float) -> RunLineProjectionV1:
        """Evaluate a home run line without storing the sportsbook line in prediction evidence."""

        spread = _finite(home_spread, "home_spread")
        threshold = self.run_margin_distribution.threshold_projection(-spread)
        return RunLineProjectionV1(
            home_spread=spread,
            home_cover_probability=threshold.above_probability,
            away_cover_probability=threshold.below_probability,
            push_probability=threshold.equal_probability,
            unresolved_probability=threshold.unresolved_probability,
            source_distribution_checksum=self.distribution_checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "expected_home_margin": self.expected_home_margin,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "run_margin_distribution": self.run_margin_distribution.as_dict(),
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "upstream_game_prediction_checksum": self.upstream_game_prediction_checksum,
            "upstream_model_manifest_checksum": self.upstream_model_manifest_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def full_game_run_margin_distribution(distribution: RunDistributionV1) -> DiscreteDistributionV1:
    """Collapse retained home/away score mass into P(home runs - away runs)."""

    margin_mass: dict[int, float] = {}
    for home_runs, home_probability in enumerate(distribution.home_run_probabilities):
        for away_runs, away_probability in enumerate(distribution.away_run_probabilities):
            margin = home_runs - away_runs
            margin_mass[margin] = margin_mass.get(margin, 0.0) + home_probability * away_probability
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.FULL_GAME_RUN_MARGIN,
        outcomes=tuple(
            DiscreteOutcomeProbabilityV1(margin, margin_mass[margin])
            for margin in sorted(margin_mass)
        ),
        unresolved_probability=distribution.approximation_tail_bound,
    )


def build_run_line_prediction(game_prediction: GamePredictionV1) -> RunLinePredictionV1:
    """Build the V2A reference/shadow evidence from an upstream score distribution."""

    capability = capability_for_family(PredictionMarketFamily.RUN_LINE)
    if capability.rollout_state is ModelRolloutState.PRODUCTION and not game_prediction.recommendation_eligible:
        raise MultiMarketContractError(
            "production run-line capability requires recommendation-eligible upstream model evidence"
        )
    target = PredictionTargetV1(
        source_game_id=game_prediction.source_game_id,
        family=PredictionMarketFamily.RUN_LINE,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )
    return RunLinePredictionV1(
        source_game_id=game_prediction.source_game_id,
        target=target,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
        upstream_game_prediction_checksum=game_prediction.checksum,
        upstream_model_manifest_checksum=game_prediction.model_manifest_checksum,
        model_id=game_prediction.model_id,
        model_version=game_prediction.model_version,
        expected_home_margin=game_prediction.expected_home_runs - game_prediction.expected_away_runs,
        run_margin_distribution=full_game_run_margin_distribution(game_prediction.distribution),
    )
