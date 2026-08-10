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

TEAM_TOTAL_PREDICTION_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_PREDICTION_V1"
TEAM_TOTAL_PROJECTION_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_PROJECTION_V1"
TEAM_TOTAL_CALCULATION_VERSION = "DSE_MLB_TEAM_RUNS_FROM_FULL_GAME_SCORING_V1"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MultiMarketContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise MultiMarketContractError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MultiMarketContractError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise MultiMarketContractError(f"{name} must be finite numeric")
    return result


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise MultiMarketContractError(f"{name} must be between zero and one")
    return result


def team_run_distribution(
    distribution: RunDistributionV1,
    *,
    is_home: bool,
) -> DiscreteDistributionV1:
    probabilities = (
        distribution.home_run_probabilities
        if is_home
        else distribution.away_run_probabilities
    )
    tail = (
        distribution.home_tail_probability
        if is_home
        else distribution.away_tail_probability
    )
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.TEAM_RUNS,
        outcomes=tuple(
            DiscreteOutcomeProbabilityV1(runs, probability)
            for runs, probability in enumerate(probabilities)
        ),
        unresolved_probability=tail,
    )


@dataclass(frozen=True, slots=True)
class TeamTotalProjectionV1:
    team_id: str
    total_line: float
    over_probability: float
    under_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = TEAM_TOTAL_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.team_id not in CANONICAL_TEAM_KEYS:
            raise MultiMarketContractError("team-total projection team must be canonical")
        object.__setattr__(self, "total_line", _number(self.total_line, "total_line"))
        for name in (
            "over_probability",
            "under_probability",
            "push_probability",
            "unresolved_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.over_probability
            + self.under_probability
            + self.push_probability
            + self.unresolved_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise MultiMarketContractError("team-total projection mass must sum to one")
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != TEAM_TOTAL_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported team-total projection contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "over_probability": self.over_probability,
            "push_probability": self.push_probability,
            "source_distribution_checksum": self.source_distribution_checksum,
            "team_id": self.team_id,
            "total_line": self.total_line,
            "under_probability": self.under_probability,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class TeamTotalPredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    team_id: str
    opponent_team_id: str
    target: PredictionTargetV1
    expected_team_runs: float
    team_runs_distribution: DiscreteDistributionV1
    upstream_game_prediction_checksum: str
    upstream_model_manifest_checksum: str
    model_id: str
    model_version: str
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    calculation_version: str = TEAM_TOTAL_CALCULATION_VERSION
    contract_version: str = TEAM_TOTAL_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise MultiMarketContractError("team-total game identity is invalid")
        if self.team_id not in {self.away_team_id, self.home_team_id}:
            raise MultiMarketContractError("team-total subject is not one of the game teams")
        expected_opponent = (
            self.home_team_id if self.team_id == self.away_team_id else self.away_team_id
        )
        if self.opponent_team_id != expected_opponent:
            raise MultiMarketContractError("team-total opponent identity mismatch")
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.TEAM_TOTAL
            or self.target.period is not PredictionPeriod.FULL_GAME
            or self.target.subject_kind is not PredictionSubjectKind.TEAM
            or self.target.subject_id != self.team_id
            or self.target.statistic != "runs"
        ):
            raise MultiMarketContractError("team-total target is invalid")
        capability = capability_for_family(PredictionMarketFamily.TEAM_TOTAL)
        if (
            self.rollout_state is not capability.rollout_state
            or self.recommendation_eligible is not capability.recommendation_eligible
        ):
            raise MultiMarketContractError("team-total lifecycle disagrees with frozen capability")
        expected = _number(self.expected_team_runs, "expected_team_runs")
        if expected <= 0.0:
            raise MultiMarketContractError("expected_team_runs must be positive")
        object.__setattr__(self, "expected_team_runs", expected)
        if self.team_runs_distribution.kind is not PredictionDistributionKind.TEAM_RUNS:
            raise MultiMarketContractError("team-total prediction requires team-runs distribution")
        for name in (
            "upstream_game_prediction_checksum",
            "upstream_model_manifest_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.calculation_version != TEAM_TOTAL_CALCULATION_VERSION:
            raise MultiMarketContractError("unsupported team-total calculation version")
        if self.contract_version != TEAM_TOTAL_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported team-total prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.team_runs_distribution.checksum

    def project(self, total_line: float) -> TeamTotalProjectionV1:
        line = _number(total_line, "total_line")
        projection = self.team_runs_distribution.threshold_projection(line)
        return TeamTotalProjectionV1(
            team_id=self.team_id,
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
            "expected_team_runs": self.expected_team_runs,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "opponent_team_id": self.opponent_team_id,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "team_id": self.team_id,
            "team_runs_distribution": self.team_runs_distribution.as_dict(),
            "upstream_game_prediction_checksum": self.upstream_game_prediction_checksum,
            "upstream_model_manifest_checksum": self.upstream_model_manifest_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def _team_prediction(
    game_prediction: GamePredictionV1,
    *,
    team_id: str,
    opponent_team_id: str,
    is_home: bool,
) -> TeamTotalPredictionV1:
    capability = capability_for_family(PredictionMarketFamily.TEAM_TOTAL)
    return TeamTotalPredictionV1(
        source_game_id=game_prediction.source_game_id,
        away_team_id=game_prediction.away_team_id,
        home_team_id=game_prediction.home_team_id,
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        target=PredictionTargetV1(
            source_game_id=game_prediction.source_game_id,
            family=PredictionMarketFamily.TEAM_TOTAL,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.TEAM,
            subject_id=team_id,
            statistic="runs",
        ),
        expected_team_runs=(
            game_prediction.expected_home_runs
            if is_home
            else game_prediction.expected_away_runs
        ),
        team_runs_distribution=team_run_distribution(
            game_prediction.distribution,
            is_home=is_home,
        ),
        upstream_game_prediction_checksum=game_prediction.checksum,
        upstream_model_manifest_checksum=game_prediction.model_manifest_checksum,
        model_id=game_prediction.model_id,
        model_version=game_prediction.model_version,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
    )


def build_team_total_predictions(
    game_prediction: GamePredictionV1,
) -> tuple[TeamTotalPredictionV1, TeamTotalPredictionV1]:
    """Return deterministic away-then-home V6 team-total prediction evidence."""

    return (
        _team_prediction(
            game_prediction,
            team_id=game_prediction.away_team_id,
            opponent_team_id=game_prediction.home_team_id,
            is_home=False,
        ),
        _team_prediction(
            game_prediction,
            team_id=game_prediction.home_team_id,
            opponent_team_id=game_prediction.away_team_id,
            is_home=True,
        ),
    )
