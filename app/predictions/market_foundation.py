from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app.daily_slate.contracts import canonical_sha256


MARKET_CAPABILITY_CONTRACT_VERSION = "DSE_MLB_MARKET_CAPABILITY_V1"
PREDICTION_TARGET_CONTRACT_VERSION = "DSE_MLB_PREDICTION_TARGET_V1"
DISCRETE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_DISCRETE_OUTCOME_V1"
DISCRETE_DISTRIBUTION_CONTRACT_VERSION = "DSE_MLB_DISCRETE_DISTRIBUTION_V1"
THRESHOLD_PROJECTION_CONTRACT_VERSION = "DSE_MLB_THRESHOLD_PROJECTION_V1"
MULTI_MARKET_ROADMAP_CHECKSUM_V1 = "661b5ae23f90f324f61676a7aced041116f477e863d7f723bb16e46df8df7b4d"


class MultiMarketContractError(ValueError):
    """Raised when multi-market prediction evidence violates its contract."""


class PredictionMarketFamily(StrEnum):
    MONEYLINE = "moneyline"
    RUN_LINE = "run_line"
    GAME_TOTAL = "game_total"
    FIRST_FIVE_MONEYLINE = "first_five_moneyline"
    FIRST_FIVE_RUN_LINE = "first_five_run_line"
    FIRST_FIVE_TOTAL = "first_five_total"
    NRFI_YRFI = "nrfi_yrfi"
    TEAM_TOTAL = "team_total"
    PITCHER_STRIKEOUTS = "pitcher_strikeouts"
    PLAYER_PROP = "player_prop"


class PredictionPeriod(StrEnum):
    FULL_GAME = "full_game"
    FIRST_FIVE = "first_five"
    FIRST_INNING = "first_inning"


class PredictionSubjectKind(StrEnum):
    GAME = "game"
    TEAM = "team"
    PLAYER = "player"


class PredictionDistributionKind(StrEnum):
    BINARY_OUTCOME = "binary_outcome"
    FULL_GAME_RUN_MARGIN = "full_game_run_margin"
    FULL_GAME_TOTAL_RUNS = "full_game_total_runs"
    FIRST_FIVE_RUN_MARGIN = "first_five_run_margin"
    FIRST_FIVE_TOTAL_RUNS = "first_five_total_runs"
    FIRST_INNING_TOTAL_RUNS = "first_inning_total_runs"
    TEAM_RUNS = "team_runs"
    PITCHER_STRIKEOUTS = "pitcher_strikeouts"
    PLAYER_STAT_COUNT = "player_stat_count"


class ModelRolloutState(StrEnum):
    REFERENCE = "reference"
    SHADOW = "shadow"
    PRODUCTION = "production"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MultiMarketContractError(f"{name} must be non-empty trimmed text")
    return value


def _probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MultiMarketContractError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise MultiMarketContractError(f"{name} must be finite and between zero and one")
    return number


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise MultiMarketContractError(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise MultiMarketContractError(f"{name} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class MarketCapabilityV1:
    roadmap_version: int
    family: PredictionMarketFamily
    period: PredictionPeriod
    subject_kind: PredictionSubjectKind
    required_distribution_kind: PredictionDistributionKind
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    contract_version: str = MARKET_CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.roadmap_version, bool)
            or not isinstance(self.roadmap_version, int)
            or not 1 <= self.roadmap_version <= 8
        ):
            raise MultiMarketContractError("roadmap_version must be an integer from 1 through 8")
        if self.contract_version != MARKET_CAPABILITY_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported market-capability contract")
        if self.recommendation_eligible and self.rollout_state is not ModelRolloutState.PRODUCTION:
            raise MultiMarketContractError("only production market capabilities may be recommendation eligible")

    def as_dict(self) -> dict[str, object]:
        return {
            "roadmap_version": self.roadmap_version,
            "family": self.family.value,
            "period": self.period.value,
            "subject_kind": self.subject_kind.value,
            "required_distribution_kind": self.required_distribution_kind.value,
            "rollout_state": self.rollout_state.value,
            "recommendation_eligible": self.recommendation_eligible,
            "contract_version": self.contract_version,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PredictionTargetV1:
    source_game_id: str
    family: PredictionMarketFamily
    period: PredictionPeriod
    subject_kind: PredictionSubjectKind
    subject_id: str | None = None
    statistic: str | None = None
    contract_version: str = PREDICTION_TARGET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        if self.contract_version != PREDICTION_TARGET_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported prediction-target contract")
        expected_period = {
            PredictionMarketFamily.MONEYLINE: PredictionPeriod.FULL_GAME,
            PredictionMarketFamily.RUN_LINE: PredictionPeriod.FULL_GAME,
            PredictionMarketFamily.GAME_TOTAL: PredictionPeriod.FULL_GAME,
            PredictionMarketFamily.FIRST_FIVE_MONEYLINE: PredictionPeriod.FIRST_FIVE,
            PredictionMarketFamily.FIRST_FIVE_RUN_LINE: PredictionPeriod.FIRST_FIVE,
            PredictionMarketFamily.FIRST_FIVE_TOTAL: PredictionPeriod.FIRST_FIVE,
            PredictionMarketFamily.NRFI_YRFI: PredictionPeriod.FIRST_INNING,
            PredictionMarketFamily.TEAM_TOTAL: PredictionPeriod.FULL_GAME,
            PredictionMarketFamily.PITCHER_STRIKEOUTS: PredictionPeriod.FULL_GAME,
            PredictionMarketFamily.PLAYER_PROP: PredictionPeriod.FULL_GAME,
        }[self.family]
        if self.period is not expected_period:
            raise MultiMarketContractError("prediction target period does not match market family")
        expected_subject = {
            PredictionMarketFamily.MONEYLINE: PredictionSubjectKind.GAME,
            PredictionMarketFamily.RUN_LINE: PredictionSubjectKind.GAME,
            PredictionMarketFamily.GAME_TOTAL: PredictionSubjectKind.GAME,
            PredictionMarketFamily.FIRST_FIVE_MONEYLINE: PredictionSubjectKind.GAME,
            PredictionMarketFamily.FIRST_FIVE_RUN_LINE: PredictionSubjectKind.GAME,
            PredictionMarketFamily.FIRST_FIVE_TOTAL: PredictionSubjectKind.GAME,
            PredictionMarketFamily.NRFI_YRFI: PredictionSubjectKind.GAME,
            PredictionMarketFamily.TEAM_TOTAL: PredictionSubjectKind.TEAM,
            PredictionMarketFamily.PITCHER_STRIKEOUTS: PredictionSubjectKind.PLAYER,
            PredictionMarketFamily.PLAYER_PROP: PredictionSubjectKind.PLAYER,
        }[self.family]
        if self.subject_kind is not expected_subject:
            raise MultiMarketContractError("prediction target subject does not match market family")
        if self.subject_kind is PredictionSubjectKind.GAME:
            if self.subject_id is not None or self.statistic is not None:
                raise MultiMarketContractError("game-level prediction target cannot carry subject or statistic")
            return
        object.__setattr__(self, "subject_id", _required_text(self.subject_id, "subject_id"))
        if self.family is PredictionMarketFamily.TEAM_TOTAL:
            statistic = "runs" if self.statistic is None else _required_text(self.statistic, "statistic").casefold()
            if statistic != "runs":
                raise MultiMarketContractError("team-total V6 target statistic must be runs")
            object.__setattr__(self, "statistic", statistic)
            return
        statistic = _required_text(self.statistic, "statistic").casefold()
        if self.family is PredictionMarketFamily.PITCHER_STRIKEOUTS:
            if statistic != "strikeouts":
                raise MultiMarketContractError("V7 pitcher target statistic must be strikeouts")
        elif statistic == "strikeouts":
            raise MultiMarketContractError("strikeouts are reserved for the dedicated V7 pitcher market")
        object.__setattr__(self, "statistic", statistic)

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "family": self.family.value,
            "period": self.period.value,
            "source_game_id": self.source_game_id,
            "statistic": self.statistic,
            "subject_id": self.subject_id,
            "subject_kind": self.subject_kind.value,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class DiscreteOutcomeProbabilityV1:
    value: int
    probability: float
    contract_version: str = DISCRETE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise MultiMarketContractError("discrete outcome value must be an integer")
        object.__setattr__(self, "probability", _probability(self.probability, "outcome probability"))
        if self.contract_version != DISCRETE_OUTCOME_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported discrete-outcome contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "probability": self.probability,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ThresholdProjectionV1:
    threshold: float
    below_probability: float
    equal_probability: float
    above_probability: float
    unresolved_probability: float
    contract_version: str = THRESHOLD_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "threshold", _finite(self.threshold, "threshold"))
        for name in (
            "below_probability",
            "equal_probability",
            "above_probability",
            "unresolved_probability",
        ):
            object.__setattr__(self, name, _probability(getattr(self, name), name))
        total = (
            self.below_probability
            + self.equal_probability
            + self.above_probability
            + self.unresolved_probability
        )
        if abs(total - 1.0) > 1e-9:
            raise MultiMarketContractError("threshold probabilities must sum to one")
        if self.contract_version != THRESHOLD_PROJECTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported threshold-projection contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "above_probability": self.above_probability,
            "below_probability": self.below_probability,
            "contract_version": self.contract_version,
            "equal_probability": self.equal_probability,
            "threshold": self.threshold,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class DiscreteDistributionV1:
    kind: PredictionDistributionKind
    outcomes: tuple[DiscreteOutcomeProbabilityV1, ...]
    unresolved_probability: float = 0.0
    contract_version: str = DISCRETE_DISTRIBUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        outcomes = tuple(self.outcomes)
        if not outcomes:
            raise MultiMarketContractError("discrete distribution must contain at least one outcome")
        values = [outcome.value for outcome in outcomes]
        if values != sorted(values) or len(values) != len(set(values)):
            raise MultiMarketContractError("discrete distribution outcomes must be unique and sorted")
        object.__setattr__(self, "outcomes", outcomes)
        unresolved = _probability(self.unresolved_probability, "unresolved_probability")
        object.__setattr__(self, "unresolved_probability", unresolved)
        total = sum(outcome.probability for outcome in outcomes) + unresolved
        if abs(total - 1.0) > 1e-9:
            raise MultiMarketContractError("discrete distribution probability mass must sum to one")
        if self.contract_version != DISCRETE_DISTRIBUTION_CONTRACT_VERSION:
            raise MultiMarketContractError("unsupported discrete-distribution contract")

    def threshold_projection(self, threshold: float) -> ThresholdProjectionV1:
        selected = _finite(threshold, "threshold")
        below = sum(outcome.probability for outcome in self.outcomes if outcome.value < selected)
        equal = sum(outcome.probability for outcome in self.outcomes if outcome.value == selected)
        above = sum(outcome.probability for outcome in self.outcomes if outcome.value > selected)
        return ThresholdProjectionV1(
            threshold=selected,
            below_probability=below,
            equal_probability=equal,
            above_probability=above,
            unresolved_probability=self.unresolved_probability,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "kind": self.kind.value,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


MULTI_MARKET_ROADMAP_V1 = (
    MarketCapabilityV1(
        1,
        PredictionMarketFamily.MONEYLINE,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.BINARY_OUTCOME,
        ModelRolloutState.PRODUCTION,
        True,
    ),
    MarketCapabilityV1(
        2,
        PredictionMarketFamily.RUN_LINE,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FULL_GAME_RUN_MARGIN,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        3,
        PredictionMarketFamily.GAME_TOTAL,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FULL_GAME_TOTAL_RUNS,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        4,
        PredictionMarketFamily.FIRST_FIVE_MONEYLINE,
        PredictionPeriod.FIRST_FIVE,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FIRST_FIVE_RUN_MARGIN,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        4,
        PredictionMarketFamily.FIRST_FIVE_RUN_LINE,
        PredictionPeriod.FIRST_FIVE,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FIRST_FIVE_RUN_MARGIN,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        4,
        PredictionMarketFamily.FIRST_FIVE_TOTAL,
        PredictionPeriod.FIRST_FIVE,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FIRST_FIVE_TOTAL_RUNS,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        5,
        PredictionMarketFamily.NRFI_YRFI,
        PredictionPeriod.FIRST_INNING,
        PredictionSubjectKind.GAME,
        PredictionDistributionKind.FIRST_INNING_TOTAL_RUNS,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        6,
        PredictionMarketFamily.TEAM_TOTAL,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.TEAM,
        PredictionDistributionKind.TEAM_RUNS,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        7,
        PredictionMarketFamily.PITCHER_STRIKEOUTS,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.PLAYER,
        PredictionDistributionKind.PITCHER_STRIKEOUTS,
        ModelRolloutState.REFERENCE,
        False,
    ),
    MarketCapabilityV1(
        8,
        PredictionMarketFamily.PLAYER_PROP,
        PredictionPeriod.FULL_GAME,
        PredictionSubjectKind.PLAYER,
        PredictionDistributionKind.PLAYER_STAT_COUNT,
        ModelRolloutState.REFERENCE,
        False,
    ),
)

if canonical_sha256([capability.as_dict() for capability in MULTI_MARKET_ROADMAP_V1]) != MULTI_MARKET_ROADMAP_CHECKSUM_V1:
    raise RuntimeError("multi-market roadmap changed without a checkpoint/version update")


def capabilities_for_version(version: int) -> tuple[MarketCapabilityV1, ...]:
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 8:
        raise MultiMarketContractError("roadmap version must be an integer from 1 through 8")
    return tuple(capability for capability in MULTI_MARKET_ROADMAP_V1 if capability.roadmap_version == version)


def capability_for_family(family: PredictionMarketFamily) -> MarketCapabilityV1:
    matches = tuple(capability for capability in MULTI_MARKET_ROADMAP_V1 if capability.family is family)
    if len(matches) != 1:
        raise MultiMarketContractError("market family capability is not uniquely defined")
    return matches[0]
