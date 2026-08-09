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
from app.team_aliases import CANONICAL_TEAM_KEYS

GAME_TOTAL_PREDICTION_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_PREDICTION_V1"
GAME_TOTAL_PROJECTION_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_PROJECTION_V1"
GAME_TOTAL_CALCULATION_VERSION = "DSE_MLB_FULL_GAME_TOTAL_RUNS_V1"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MultiMarketContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise MultiMarketContractError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MultiMarketContractError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MultiMarketContractError(f"{name} must be finite")
    return result


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise MultiMarketContractError(f"{name} must be between zero and one")
    return result


@dataclass(frozen=True, slots=True)
class GameTotalProjectionV1:
    total_line: float
    over_probability: float
    under_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = GAME_TOTAL_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_line", _number(self.total_line, "total_line"))
        for name in ("over_probability", "under_probability", "push_probability", "unresolved_probability"):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.over_probability + self.under_probability + self.push_probability + self.unresolved_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise MultiMarketContractError("game-total projection mass must sum to one")
        object.__setattr__(self, "source_distribution_checksum", _sha(self.source_distribution_checksum, "source_distribution_checksum"))
        if self.contract_version != GAME_TOTAL_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported game-total projection contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "over_probability": self.over_probability,
            "push_probability": self.push_probability,
            "source_distribution_checksum": self.source_distribution_checksum,
            "total_line": self.total_line,
            "under_probability": self.under_probability,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class GameTotalPredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    target: PredictionTargetV1
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    upstream_game_prediction_checksum: str
    upstream_model_manifest_checksum: str
    model_id: str
    model_version: str
    expected_total_runs: float
    total_runs_distribution: DiscreteDistributionV1
    calculation_version: str = GAME_TOTAL_CALCULATION_VERSION
    contract_version: str = GAME_TOTAL_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise MultiMarketContractError("game-total team identity is invalid")
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.GAME_TOTAL
            or self.target.period is not PredictionPeriod.FULL_GAME
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise MultiMarketContractError("game-total target is invalid")
        capability = capability_for_family(PredictionMarketFamily.GAME_TOTAL)
        if self.rollout_state is not capability.rollout_state:
            raise MultiMarketContractError("game-total rollout state disagrees with capability")
        if self.recommendation_eligible is not capability.recommendation_eligible:
            raise MultiMarketContractError("game-total recommendation eligibility disagrees with capability")
        for name in ("upstream_game_prediction_checksum", "upstream_model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        expected = _number(self.expected_total_runs, "expected_total_runs")
        if expected <= 0.0:
            raise MultiMarketContractError("expected_total_runs must be positive")
        object.__setattr__(self, "expected_total_runs", expected)
        if self.total_runs_distribution.kind is not PredictionDistributionKind.FULL_GAME_TOTAL_RUNS:
            raise MultiMarketContractError("game-total prediction requires total-runs distribution")
        if self.calculation_version != GAME_TOTAL_CALCULATION_VERSION:
            raise MultiMarketContractError("unsupported game-total calculation version")
        if self.contract_version != GAME_TOTAL_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported game-total prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.total_runs_distribution.checksum

    def project(self, total_line: float) -> GameTotalProjectionV1:
        line = _number(total_line, "total_line")
        projection = self.total_runs_distribution.threshold_projection(line)
        return GameTotalProjectionV1(
            total_line=line,
            over_probability=projection.above_probability,
            under_probability=projection.below_probability,
            push_probability=projection.equal_probability,
            unresolved_probability=projection.unresolved_probability,
            source_distribution_checksum=self.distribution_checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "expected_total_runs": self.expected_total_runs,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "total_runs_distribution": self.total_runs_distribution.as_dict(),
            "upstream_game_prediction_checksum": self.upstream_game_prediction_checksum,
            "upstream_model_manifest_checksum": self.upstream_model_manifest_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def full_game_total_runs_distribution(distribution: RunDistributionV1) -> DiscreteDistributionV1:
    mass: dict[int, float] = {}
    for home_runs, home_probability in enumerate(distribution.home_run_probabilities):
        for away_runs, away_probability in enumerate(distribution.away_run_probabilities):
            total_runs = home_runs + away_runs
            mass[total_runs] = mass.get(total_runs, 0.0) + home_probability * away_probability
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.FULL_GAME_TOTAL_RUNS,
        outcomes=tuple(DiscreteOutcomeProbabilityV1(total, mass[total]) for total in sorted(mass)),
        unresolved_probability=distribution.approximation_tail_bound,
    )


def build_game_total_prediction(game_prediction: GamePredictionV1) -> GameTotalPredictionV1:
    capability = capability_for_family(PredictionMarketFamily.GAME_TOTAL)
    target = PredictionTargetV1(
        source_game_id=game_prediction.source_game_id,
        family=PredictionMarketFamily.GAME_TOTAL,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )
    return GameTotalPredictionV1(
        source_game_id=game_prediction.source_game_id,
        away_team_id=game_prediction.away_team_id,
        home_team_id=game_prediction.home_team_id,
        target=target,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
        upstream_game_prediction_checksum=game_prediction.checksum,
        upstream_model_manifest_checksum=game_prediction.model_manifest_checksum,
        model_id=game_prediction.model_id,
        model_version=game_prediction.model_version,
        expected_total_runs=game_prediction.expected_home_runs + game_prediction.expected_away_runs,
        total_runs_distribution=full_game_total_runs_distribution(game_prediction.distribution),
    )
