from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.contracts import RunDistributionV1
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

FIRST_FIVE_SCORING_PREDICTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_SCORING_PREDICTION_V1"
FIRST_FIVE_MONEYLINE_PREDICTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_MONEYLINE_PREDICTION_V1"
FIRST_FIVE_RUN_LINE_PREDICTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_RUN_LINE_PREDICTION_V1"
FIRST_FIVE_RUN_LINE_PROJECTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_RUN_LINE_PROJECTION_V1"
FIRST_FIVE_TOTAL_PREDICTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_TOTAL_PREDICTION_V1"
FIRST_FIVE_TOTAL_PROJECTION_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_TOTAL_PROJECTION_V1"
FIRST_FIVE_CALCULATION_VERSION = "DSE_MLB_FIRST_FIVE_INDEPENDENT_SCORING_V1"
FIRST_FIVE_MODEL_SCOPE = "first_five_independent"



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



def _validate_teams(away_team_id: str, home_team_id: str) -> None:
    if (
        away_team_id not in CANONICAL_TEAM_KEYS
        or home_team_id not in CANONICAL_TEAM_KEYS
        or away_team_id == home_team_id
    ):
        raise MultiMarketContractError("first-five team identity is invalid")



def _target(source_game_id: str, family: PredictionMarketFamily) -> PredictionTargetV1:
    return PredictionTargetV1(
        source_game_id=source_game_id,
        family=family,
        period=PredictionPeriod.FIRST_FIVE,
        subject_kind=PredictionSubjectKind.GAME,
    )



def first_five_run_margin_distribution(distribution: RunDistributionV1) -> DiscreteDistributionV1:
    mass: dict[int, float] = {}
    for home_runs, home_probability in enumerate(distribution.home_run_probabilities):
        for away_runs, away_probability in enumerate(distribution.away_run_probabilities):
            margin = home_runs - away_runs
            mass[margin] = mass.get(margin, 0.0) + home_probability * away_probability
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.FIRST_FIVE_RUN_MARGIN,
        outcomes=tuple(
            DiscreteOutcomeProbabilityV1(margin, mass[margin])
            for margin in sorted(mass)
        ),
        unresolved_probability=distribution.approximation_tail_bound,
    )



def first_five_total_runs_distribution(distribution: RunDistributionV1) -> DiscreteDistributionV1:
    mass: dict[int, float] = {}
    for home_runs, home_probability in enumerate(distribution.home_run_probabilities):
        for away_runs, away_probability in enumerate(distribution.away_run_probabilities):
            total = home_runs + away_runs
            mass[total] = mass.get(total, 0.0) + home_probability * away_probability
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.FIRST_FIVE_TOTAL_RUNS,
        outcomes=tuple(
            DiscreteOutcomeProbabilityV1(total, mass[total])
            for total in sorted(mass)
        ),
        unresolved_probability=distribution.approximation_tail_bound,
    )


@dataclass(frozen=True, slots=True)
class FirstFiveScoringPredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_model_feature_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    distribution: RunDistributionV1
    rollout_state: ModelRolloutState = ModelRolloutState.REFERENCE
    recommendation_eligible: bool = False
    model_scope: str = FIRST_FIVE_MODEL_SCOPE
    calculation_version: str = FIRST_FIVE_CALCULATION_VERSION
    contract_version: str = FIRST_FIVE_SCORING_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        object.__setattr__(
            self,
            "upstream_model_feature_checksum",
            _sha(self.upstream_model_feature_checksum, "upstream_model_feature_checksum"),
        )
        object.__setattr__(
            self,
            "model_manifest_checksum",
            _sha(self.model_manifest_checksum, "model_manifest_checksum"),
        )
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.rollout_state is not ModelRolloutState.REFERENCE or self.recommendation_eligible:
            raise MultiMarketContractError("V4A first-five scoring evidence must remain reference-only")
        if self.model_scope != FIRST_FIVE_MODEL_SCOPE:
            raise MultiMarketContractError("first-five scoring evidence requires an independent first-five model scope")
        if self.calculation_version != FIRST_FIVE_CALCULATION_VERSION:
            raise MultiMarketContractError("unsupported first-five scoring calculation version")
        if self.contract_version != FIRST_FIVE_SCORING_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five scoring prediction contract")

    @property
    def expected_home_runs(self) -> float:
        return self.distribution.home_run_rate

    @property
    def expected_away_runs(self) -> float:
        return self.distribution.away_run_rate

    @property
    def distribution_checksum(self) -> str:
        return canonical_sha256(self.distribution.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "distribution": self.distribution.as_dict(),
            "distribution_checksum": self.distribution_checksum,
            "expected_away_runs": self.expected_away_runs,
            "expected_home_runs": self.expected_home_runs,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_scope": self.model_scope,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "upstream_model_feature_checksum": self.upstream_model_feature_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class FirstFiveMoneylinePredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    target: PredictionTargetV1
    home_win_probability: float
    away_win_probability: float
    tie_probability: float
    unresolved_probability: float
    upstream_first_five_scoring_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    contract_version: str = FIRST_FIVE_MONEYLINE_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.FIRST_FIVE_MONEYLINE
            or self.target.period is not PredictionPeriod.FIRST_FIVE
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise MultiMarketContractError("first-five moneyline target is invalid")
        capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_MONEYLINE)
        if self.rollout_state is not capability.rollout_state or self.recommendation_eligible is not capability.recommendation_eligible:
            raise MultiMarketContractError("first-five moneyline lifecycle disagrees with frozen capability")
        for name in (
            "home_win_probability",
            "away_win_probability",
            "tie_probability",
            "unresolved_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.home_win_probability
            + self.away_win_probability
            + self.tie_probability
            + self.unresolved_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise MultiMarketContractError("first-five moneyline probability mass must sum to one")
        for name in ("upstream_first_five_scoring_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.contract_version != FIRST_FIVE_MONEYLINE_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five moneyline prediction contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "away_win_probability": self.away_win_probability,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
            "home_win_probability": self.home_win_probability,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "tie_probability": self.tie_probability,
            "unresolved_probability": self.unresolved_probability,
            "upstream_first_five_scoring_checksum": self.upstream_first_five_scoring_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class FirstFiveRunLineProjectionV1:
    home_spread: float
    home_cover_probability: float
    away_cover_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = FIRST_FIVE_RUN_LINE_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "home_spread", _number(self.home_spread, "home_spread"))
        for name in (
            "home_cover_probability",
            "away_cover_probability",
            "push_probability",
            "unresolved_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        if not math.isclose(
            self.home_cover_probability
            + self.away_cover_probability
            + self.push_probability
            + self.unresolved_probability,
            1.0,
            abs_tol=1e-9,
        ):
            raise MultiMarketContractError("first-five run-line probability mass must sum to one")
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != FIRST_FIVE_RUN_LINE_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five run-line projection contract")

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


@dataclass(frozen=True, slots=True)
class FirstFiveRunLinePredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    target: PredictionTargetV1
    expected_home_margin: float
    run_margin_distribution: DiscreteDistributionV1
    upstream_first_five_scoring_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    contract_version: str = FIRST_FIVE_RUN_LINE_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.FIRST_FIVE_RUN_LINE
            or self.target.period is not PredictionPeriod.FIRST_FIVE
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise MultiMarketContractError("first-five run-line target is invalid")
        capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_RUN_LINE)
        if self.rollout_state is not capability.rollout_state or self.recommendation_eligible is not capability.recommendation_eligible:
            raise MultiMarketContractError("first-five run-line lifecycle disagrees with frozen capability")
        object.__setattr__(self, "expected_home_margin", _number(self.expected_home_margin, "expected_home_margin"))
        if self.run_margin_distribution.kind is not PredictionDistributionKind.FIRST_FIVE_RUN_MARGIN:
            raise MultiMarketContractError("first-five run-line requires first-five run-margin distribution")
        for name in ("upstream_first_five_scoring_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.contract_version != FIRST_FIVE_RUN_LINE_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five run-line prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.run_margin_distribution.checksum

    def project(self, home_spread: float) -> FirstFiveRunLineProjectionV1:
        spread = _number(home_spread, "home_spread")
        threshold = self.run_margin_distribution.threshold_projection(-spread)
        return FirstFiveRunLineProjectionV1(
            home_spread=spread,
            home_cover_probability=threshold.above_probability,
            away_cover_probability=threshold.below_probability,
            push_probability=threshold.equal_probability,
            unresolved_probability=threshold.unresolved_probability,
            source_distribution_checksum=self.distribution_checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "expected_home_margin": self.expected_home_margin,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "run_margin_distribution": self.run_margin_distribution.as_dict(),
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "upstream_first_five_scoring_checksum": self.upstream_first_five_scoring_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class FirstFiveTotalProjectionV1:
    total_line: float
    over_probability: float
    under_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = FIRST_FIVE_TOTAL_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
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
            raise MultiMarketContractError("first-five total probability mass must sum to one")
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != FIRST_FIVE_TOTAL_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five total projection contract")

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


@dataclass(frozen=True, slots=True)
class FirstFiveTotalPredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    target: PredictionTargetV1
    expected_total_runs: float
    total_runs_distribution: DiscreteDistributionV1
    upstream_first_five_scoring_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    contract_version: str = FIRST_FIVE_TOTAL_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.FIRST_FIVE_TOTAL
            or self.target.period is not PredictionPeriod.FIRST_FIVE
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise MultiMarketContractError("first-five total target is invalid")
        capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_TOTAL)
        if self.rollout_state is not capability.rollout_state or self.recommendation_eligible is not capability.recommendation_eligible:
            raise MultiMarketContractError("first-five total lifecycle disagrees with frozen capability")
        expected = _number(self.expected_total_runs, "expected_total_runs")
        if expected <= 0.0:
            raise MultiMarketContractError("expected_total_runs must be positive")
        object.__setattr__(self, "expected_total_runs", expected)
        if self.total_runs_distribution.kind is not PredictionDistributionKind.FIRST_FIVE_TOTAL_RUNS:
            raise MultiMarketContractError("first-five total requires first-five total-runs distribution")
        for name in ("upstream_first_five_scoring_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.contract_version != FIRST_FIVE_TOTAL_PREDICTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported first-five total prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.total_runs_distribution.checksum

    def project(self, total_line: float) -> FirstFiveTotalProjectionV1:
        line = _number(total_line, "total_line")
        threshold = self.total_runs_distribution.threshold_projection(line)
        return FirstFiveTotalProjectionV1(
            total_line=line,
            over_probability=threshold.above_probability,
            under_probability=threshold.below_probability,
            push_probability=threshold.equal_probability,
            unresolved_probability=threshold.unresolved_probability,
            source_distribution_checksum=self.distribution_checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "expected_total_runs": self.expected_total_runs,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_version": self.model_version,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "total_runs_distribution": self.total_runs_distribution.as_dict(),
            "upstream_first_five_scoring_checksum": self.upstream_first_five_scoring_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())



def build_first_five_moneyline_prediction(source: FirstFiveScoringPredictionV1) -> FirstFiveMoneylinePredictionV1:
    margin = first_five_run_margin_distribution(source.distribution)
    home = sum(item.probability for item in margin.outcomes if item.value > 0)
    away = sum(item.probability for item in margin.outcomes if item.value < 0)
    tie = sum(item.probability for item in margin.outcomes if item.value == 0)
    capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_MONEYLINE)
    return FirstFiveMoneylinePredictionV1(
        source_game_id=source.source_game_id,
        away_team_id=source.away_team_id,
        home_team_id=source.home_team_id,
        target=_target(source.source_game_id, PredictionMarketFamily.FIRST_FIVE_MONEYLINE),
        home_win_probability=home,
        away_win_probability=away,
        tie_probability=tie,
        unresolved_probability=margin.unresolved_probability,
        upstream_first_five_scoring_checksum=source.checksum,
        model_manifest_checksum=source.model_manifest_checksum,
        model_id=source.model_id,
        model_version=source.model_version,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
    )



def build_first_five_run_line_prediction(source: FirstFiveScoringPredictionV1) -> FirstFiveRunLinePredictionV1:
    capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_RUN_LINE)
    return FirstFiveRunLinePredictionV1(
        source_game_id=source.source_game_id,
        away_team_id=source.away_team_id,
        home_team_id=source.home_team_id,
        target=_target(source.source_game_id, PredictionMarketFamily.FIRST_FIVE_RUN_LINE),
        expected_home_margin=source.expected_home_runs - source.expected_away_runs,
        run_margin_distribution=first_five_run_margin_distribution(source.distribution),
        upstream_first_five_scoring_checksum=source.checksum,
        model_manifest_checksum=source.model_manifest_checksum,
        model_id=source.model_id,
        model_version=source.model_version,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
    )



def build_first_five_total_prediction(source: FirstFiveScoringPredictionV1) -> FirstFiveTotalPredictionV1:
    capability = capability_for_family(PredictionMarketFamily.FIRST_FIVE_TOTAL)
    return FirstFiveTotalPredictionV1(
        source_game_id=source.source_game_id,
        away_team_id=source.away_team_id,
        home_team_id=source.home_team_id,
        target=_target(source.source_game_id, PredictionMarketFamily.FIRST_FIVE_TOTAL),
        expected_total_runs=source.expected_home_runs + source.expected_away_runs,
        total_runs_distribution=first_five_total_runs_distribution(source.distribution),
        upstream_first_five_scoring_checksum=source.checksum,
        model_manifest_checksum=source.model_manifest_checksum,
        model_id=source.model_id,
        model_version=source.model_version,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
    )
