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

FIRST_INNING_SCORING_PREDICTION_CONTRACT_VERSION = (
    "DSE_MLB_FIRST_INNING_SCORING_PREDICTION_V1"
)
NRFI_YRFI_PREDICTION_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_PREDICTION_V1"
FIRST_INNING_CALCULATION_VERSION = "DSE_MLB_FIRST_INNING_INDEPENDENT_SCORING_V1"
FIRST_INNING_MODEL_SCOPE = "first_inning_independent"


class FirstInningPredictionError(MultiMarketContractError):
    """Raised when V5A first-inning prediction evidence is invalid."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstInningPredictionError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstInningPredictionError(f"{name} must be lowercase SHA-256")
    return text


def _probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstInningPredictionError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise FirstInningPredictionError(f"{name} must be between zero and one")
    return result


def _validate_teams(away_team_id: str, home_team_id: str) -> None:
    if (
        away_team_id not in CANONICAL_TEAM_KEYS
        or home_team_id not in CANONICAL_TEAM_KEYS
        or away_team_id == home_team_id
    ):
        raise FirstInningPredictionError("first-inning team identity is invalid")


def first_inning_total_runs_distribution(
    distribution: RunDistributionV1,
) -> DiscreteDistributionV1:
    """Collapse independent first-inning team-run marginals to total runs."""

    mass: dict[int, float] = {}
    for home_runs, home_probability in enumerate(distribution.home_run_probabilities):
        for away_runs, away_probability in enumerate(distribution.away_run_probabilities):
            total_runs = home_runs + away_runs
            mass[total_runs] = mass.get(total_runs, 0.0) + (
                home_probability * away_probability
            )
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.FIRST_INNING_TOTAL_RUNS,
        outcomes=tuple(
            DiscreteOutcomeProbabilityV1(total_runs, mass[total_runs])
            for total_runs in sorted(mass)
        ),
        unresolved_probability=distribution.approximation_tail_bound,
    )


@dataclass(frozen=True, slots=True)
class FirstInningScoringPredictionV1:
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
    model_scope: str = FIRST_INNING_MODEL_SCOPE
    calculation_version: str = FIRST_INNING_CALCULATION_VERSION
    contract_version: str = FIRST_INNING_SCORING_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        for name in ("upstream_model_feature_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        capability = capability_for_family(PredictionMarketFamily.NRFI_YRFI)
        if (
            capability.rollout_state is not ModelRolloutState.REFERENCE
            or capability.recommendation_eligible
            or self.rollout_state is not capability.rollout_state
            or self.recommendation_eligible
        ):
            raise FirstInningPredictionError(
                "V5A first-inning scoring evidence must remain reference-only"
            )
        if self.model_scope != FIRST_INNING_MODEL_SCOPE:
            raise FirstInningPredictionError(
                "first-inning scoring requires an independent first-inning model scope"
            )
        if self.calculation_version != FIRST_INNING_CALCULATION_VERSION:
            raise FirstInningPredictionError("unsupported first-inning calculation version")
        if self.contract_version != FIRST_INNING_SCORING_PREDICTION_CONTRACT_VERSION:
            raise FirstInningPredictionError(
                "unsupported first-inning scoring prediction contract"
            )

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
class NrfiYrfiPredictionV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    target: PredictionTargetV1
    nrfi_probability: float
    yrfi_probability: float
    total_runs_distribution: DiscreteDistributionV1
    upstream_first_inning_scoring_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    calculation_version: str = FIRST_INNING_CALCULATION_VERSION
    contract_version: str = NRFI_YRFI_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        _validate_teams(self.away_team_id, self.home_team_id)
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.NRFI_YRFI
            or self.target.period is not PredictionPeriod.FIRST_INNING
            or self.target.subject_kind is not PredictionSubjectKind.GAME
        ):
            raise FirstInningPredictionError("NRFI/YRFI prediction target is invalid")
        capability = capability_for_family(PredictionMarketFamily.NRFI_YRFI)
        if (
            self.rollout_state is not capability.rollout_state
            or self.recommendation_eligible is not capability.recommendation_eligible
        ):
            raise FirstInningPredictionError(
                "NRFI/YRFI lifecycle disagrees with frozen capability"
            )
        nrfi = _probability(self.nrfi_probability, "nrfi_probability")
        yrfi = _probability(self.yrfi_probability, "yrfi_probability")
        object.__setattr__(self, "nrfi_probability", nrfi)
        object.__setattr__(self, "yrfi_probability", yrfi)
        if not math.isclose(nrfi + yrfi, 1.0, abs_tol=1e-10):
            raise FirstInningPredictionError("NRFI/YRFI probabilities must sum to one")
        if (
            self.total_runs_distribution.kind
            is not PredictionDistributionKind.FIRST_INNING_TOTAL_RUNS
        ):
            raise FirstInningPredictionError(
                "NRFI/YRFI requires a first-inning total-runs distribution"
            )
        zero_mass = sum(
            item.probability
            for item in self.total_runs_distribution.outcomes
            if item.value == 0
        )
        if not math.isclose(nrfi, zero_mass, abs_tol=1e-10):
            raise FirstInningPredictionError(
                "NRFI probability must equal retained zero-run probability"
            )
        expected_yrfi = 1.0 - zero_mass
        if not math.isclose(yrfi, expected_yrfi, abs_tol=1e-10):
            raise FirstInningPredictionError(
                "YRFI probability must include all positive and unresolved tail mass"
            )
        for name in (
            "upstream_first_inning_scoring_checksum",
            "model_manifest_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        if self.calculation_version != FIRST_INNING_CALCULATION_VERSION:
            raise FirstInningPredictionError("unsupported NRFI/YRFI calculation version")
        if self.contract_version != NRFI_YRFI_PREDICTION_CONTRACT_VERSION:
            raise FirstInningPredictionError("unsupported NRFI/YRFI prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.total_runs_distribution.checksum

    def as_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calculation_version": self.calculation_version,
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "home_team_id": self.home_team_id,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_version": self.model_version,
            "nrfi_probability": self.nrfi_probability,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "target": self.target.as_dict(),
            "total_runs_distribution": self.total_runs_distribution.as_dict(),
            "upstream_first_inning_scoring_checksum": (
                self.upstream_first_inning_scoring_checksum
            ),
            "yrfi_probability": self.yrfi_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def build_nrfi_yrfi_prediction(
    source: FirstInningScoringPredictionV1,
) -> NrfiYrfiPredictionV1:
    """Build exact zero-run vs one-or-more-run first-inning probabilities."""

    total_distribution = first_inning_total_runs_distribution(source.distribution)
    nrfi = sum(
        item.probability for item in total_distribution.outcomes if item.value == 0
    )
    # Any truncated tail necessarily implies a positive run total, so it belongs to
    # YRFI rather than remaining unresolved for this binary market.
    yrfi = 1.0 - nrfi
    capability = capability_for_family(PredictionMarketFamily.NRFI_YRFI)
    return NrfiYrfiPredictionV1(
        source_game_id=source.source_game_id,
        away_team_id=source.away_team_id,
        home_team_id=source.home_team_id,
        target=PredictionTargetV1(
            source_game_id=source.source_game_id,
            family=PredictionMarketFamily.NRFI_YRFI,
            period=PredictionPeriod.FIRST_INNING,
            subject_kind=PredictionSubjectKind.GAME,
        ),
        nrfi_probability=nrfi,
        yrfi_probability=yrfi,
        total_runs_distribution=total_distribution,
        upstream_first_inning_scoring_checksum=source.checksum,
        model_manifest_checksum=source.model_manifest_checksum,
        model_id=source.model_id,
        model_version=source.model_version,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
    )
