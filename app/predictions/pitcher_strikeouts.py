from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
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

PITCHER_STRIKEOUT_PREDICTION_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_PREDICTION_V1"
PITCHER_STRIKEOUT_PROJECTION_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_PROJECTION_V1"
PITCHER_STRIKEOUT_MODEL_SCOPE = "pitcher_strikeouts_independent"


class PitcherStrikeoutPredictionError(ValueError):
    """Raised when V7 pitcher-strikeout prediction evidence violates its contract."""


class PitcherStarterBindingState(StrEnum):
    EXPECTED = "expected"
    CONFIRMED = "confirmed"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PitcherStrikeoutPredictionError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PitcherStrikeoutPredictionError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PitcherStrikeoutPredictionError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PitcherStrikeoutPredictionError(f"{name} must be finite numeric")
    return result


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise PitcherStrikeoutPredictionError(f"{name} must be between zero and one")
    return result


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutProjectionV1:
    pitcher_id: str
    line: float
    over_probability: float
    under_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = PITCHER_STRIKEOUT_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "pitcher_id", _text(self.pitcher_id, "pitcher_id"))
        line = _number(self.line, "line")
        if line < 0.0:
            raise PitcherStrikeoutPredictionError("pitcher-strikeout line must be nonnegative")
        object.__setattr__(self, "line", line)
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
            raise PitcherStrikeoutPredictionError(
                "pitcher-strikeout projection mass must sum to one"
            )
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != PITCHER_STRIKEOUT_PROJECTION_CONTRACT_VERSION:
            raise PitcherStrikeoutPredictionError(
                "unsupported V7 pitcher-strikeout projection contract"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "line": self.line,
            "over_probability": self.over_probability,
            "pitcher_id": self.pitcher_id,
            "push_probability": self.push_probability,
            "source_distribution_checksum": self.source_distribution_checksum,
            "under_probability": self.under_probability,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutPredictionV1:
    source_game_id: str
    pitcher_id: str
    pitcher_name: str
    team_id: str
    opponent_team_id: str
    starter_binding_state: PitcherStarterBindingState
    starter_binding_checksum: str
    target: PredictionTargetV1
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    source_model_input_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    model_scope: str
    expected_strikeouts: float
    strikeout_distribution: DiscreteDistributionV1
    contract_version: str = PITCHER_STRIKEOUT_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "pitcher_id", _text(self.pitcher_id, "pitcher_id"))
        object.__setattr__(self, "pitcher_name", _text(self.pitcher_name, "pitcher_name"))
        if (
            self.team_id not in CANONICAL_TEAM_KEYS
            or self.opponent_team_id not in CANONICAL_TEAM_KEYS
            or self.team_id == self.opponent_team_id
        ):
            raise PitcherStrikeoutPredictionError("pitcher-strikeout team identity is invalid")
        if self.starter_binding_state not in {
            PitcherStarterBindingState.EXPECTED,
            PitcherStarterBindingState.CONFIRMED,
        }:
            raise PitcherStrikeoutPredictionError("V7 requires an expected or confirmed starter")
        object.__setattr__(
            self,
            "starter_binding_checksum",
            _sha(self.starter_binding_checksum, "starter_binding_checksum"),
        )
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.PITCHER_STRIKEOUTS
            or self.target.period is not PredictionPeriod.FULL_GAME
            or self.target.subject_kind is not PredictionSubjectKind.PLAYER
            or self.target.subject_id != self.pitcher_id
            or self.target.statistic != "strikeouts"
        ):
            raise PitcherStrikeoutPredictionError("pitcher-strikeout prediction target is invalid")
        capability = capability_for_family(PredictionMarketFamily.PITCHER_STRIKEOUTS)
        if self.rollout_state is not capability.rollout_state:
            raise PitcherStrikeoutPredictionError(
                "pitcher-strikeout rollout state disagrees with capability"
            )
        if self.recommendation_eligible is not capability.recommendation_eligible:
            raise PitcherStrikeoutPredictionError(
                "pitcher-strikeout recommendation eligibility disagrees with capability"
            )
        for name in ("source_model_input_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        object.__setattr__(self, "model_scope", _text(self.model_scope, "model_scope"))
        if self.model_scope != PITCHER_STRIKEOUT_MODEL_SCOPE:
            raise PitcherStrikeoutPredictionError(
                "V7 pitcher strikeouts require an independent pitcher-strikeout model scope"
            )
        expected = _number(self.expected_strikeouts, "expected_strikeouts")
        if expected < 0.0:
            raise PitcherStrikeoutPredictionError("expected_strikeouts must be nonnegative")
        object.__setattr__(self, "expected_strikeouts", expected)
        if self.strikeout_distribution.kind is not PredictionDistributionKind.PITCHER_STRIKEOUTS:
            raise PitcherStrikeoutPredictionError(
                "V7 prediction requires PITCHER_STRIKEOUTS distribution"
            )
        if any(outcome.value < 0 for outcome in self.strikeout_distribution.outcomes):
            raise PitcherStrikeoutPredictionError(
                "pitcher-strikeout count outcomes must be nonnegative"
            )
        if self.contract_version != PITCHER_STRIKEOUT_PREDICTION_CONTRACT_VERSION:
            raise PitcherStrikeoutPredictionError(
                "unsupported V7 pitcher-strikeout prediction contract"
            )

    @property
    def distribution_checksum(self) -> str:
        return self.strikeout_distribution.checksum

    def project(self, line: float) -> PitcherStrikeoutProjectionV1:
        selected = _number(line, "line")
        if selected < 0.0:
            raise PitcherStrikeoutPredictionError("pitcher-strikeout line must be nonnegative")
        projection = self.strikeout_distribution.threshold_projection(selected)
        return PitcherStrikeoutProjectionV1(
            pitcher_id=self.pitcher_id,
            line=selected,
            over_probability=projection.above_probability,
            under_probability=projection.below_probability,
            push_probability=projection.equal_probability,
            unresolved_probability=projection.unresolved_probability,
            source_distribution_checksum=self.distribution_checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "distribution_checksum": self.distribution_checksum,
            "expected_strikeouts": self.expected_strikeouts,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_scope": self.model_scope,
            "model_version": self.model_version,
            "opponent_team_id": self.opponent_team_id,
            "pitcher_id": self.pitcher_id,
            "pitcher_name": self.pitcher_name,
            "recommendation_eligible": self.recommendation_eligible,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "source_model_input_checksum": self.source_model_input_checksum,
            "starter_binding_checksum": self.starter_binding_checksum,
            "starter_binding_state": self.starter_binding_state.value,
            "strikeout_distribution": self.strikeout_distribution.as_dict(),
            "target": self.target.as_dict(),
            "team_id": self.team_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def build_pitcher_strikeout_prediction(
    *,
    source_game_id: str,
    pitcher_id: str,
    pitcher_name: str,
    team_id: str,
    opponent_team_id: str,
    starter_binding_state: PitcherStarterBindingState,
    starter_binding_checksum: str,
    source_model_input_checksum: str,
    model_manifest_checksum: str,
    model_id: str,
    model_version: str,
    expected_strikeouts: float,
    strikeout_distribution: DiscreteDistributionV1,
) -> PitcherStrikeoutPredictionV1:
    capability = capability_for_family(PredictionMarketFamily.PITCHER_STRIKEOUTS)
    try:
        target = PredictionTargetV1(
            source_game_id=source_game_id,
            family=PredictionMarketFamily.PITCHER_STRIKEOUTS,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.PLAYER,
            subject_id=pitcher_id,
            statistic="strikeouts",
        )
    except MultiMarketContractError as exc:
        raise PitcherStrikeoutPredictionError(
            "invalid V7 pitcher-strikeout target"
        ) from exc
    return PitcherStrikeoutPredictionV1(
        source_game_id=source_game_id,
        pitcher_id=pitcher_id,
        pitcher_name=pitcher_name,
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        starter_binding_state=starter_binding_state,
        starter_binding_checksum=starter_binding_checksum,
        target=target,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
        source_model_input_checksum=source_model_input_checksum,
        model_manifest_checksum=model_manifest_checksum,
        model_id=model_id,
        model_version=model_version,
        model_scope=PITCHER_STRIKEOUT_MODEL_SCOPE,
        expected_strikeouts=expected_strikeouts,
        strikeout_distribution=strikeout_distribution,
    )
