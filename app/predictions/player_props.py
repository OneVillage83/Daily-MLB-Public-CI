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

PLAYER_PROP_PREDICTION_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_PREDICTION_V1"
PLAYER_PROP_PROJECTION_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_PROJECTION_V1"
PLAYER_PROP_MODEL_SCOPE = "player_prop_independent"


class PlayerPropPredictionError(ValueError):
    """Raised when V8 player-prop prediction evidence violates its contract."""


class PlayerPropRole(StrEnum):
    BATTER = "batter"
    PITCHER = "pitcher"


class PlayerPropStatistic(StrEnum):
    BATTER_HOME_RUNS = "home_runs"
    BATTER_HITS = "hits"
    BATTER_TOTAL_BASES = "total_bases"
    BATTER_RBIS = "rbis"
    BATTER_RUNS_SCORED = "runs_scored"
    BATTER_HITS_RUNS_RBIS = "hits_runs_rbis"
    BATTER_SINGLES = "singles"
    BATTER_DOUBLES = "doubles"
    BATTER_TRIPLES = "triples"
    BATTER_WALKS = "walks"
    BATTER_STRIKEOUTS = "batter_strikeouts"
    BATTER_STOLEN_BASES = "stolen_bases"
    PITCHER_HITS_ALLOWED = "hits_allowed"
    PITCHER_WALKS = "pitcher_walks"
    PITCHER_EARNED_RUNS = "earned_runs"
    PITCHER_OUTS = "outs"


BATTER_PLAYER_PROP_STATISTICS = frozenset(
    {
        PlayerPropStatistic.BATTER_HOME_RUNS,
        PlayerPropStatistic.BATTER_HITS,
        PlayerPropStatistic.BATTER_TOTAL_BASES,
        PlayerPropStatistic.BATTER_RBIS,
        PlayerPropStatistic.BATTER_RUNS_SCORED,
        PlayerPropStatistic.BATTER_HITS_RUNS_RBIS,
        PlayerPropStatistic.BATTER_SINGLES,
        PlayerPropStatistic.BATTER_DOUBLES,
        PlayerPropStatistic.BATTER_TRIPLES,
        PlayerPropStatistic.BATTER_WALKS,
        PlayerPropStatistic.BATTER_STRIKEOUTS,
        PlayerPropStatistic.BATTER_STOLEN_BASES,
    }
)
PITCHER_PLAYER_PROP_STATISTICS = frozenset(
    {
        PlayerPropStatistic.PITCHER_HITS_ALLOWED,
        PlayerPropStatistic.PITCHER_WALKS,
        PlayerPropStatistic.PITCHER_EARNED_RUNS,
        PlayerPropStatistic.PITCHER_OUTS,
    }
)
SUPPORTED_PLAYER_PROP_STATISTICS = BATTER_PLAYER_PROP_STATISTICS | PITCHER_PLAYER_PROP_STATISTICS


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlayerPropPredictionError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PlayerPropPredictionError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlayerPropPredictionError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlayerPropPredictionError(f"{name} must be finite numeric")
    return result


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise PlayerPropPredictionError(f"{name} must be between zero and one")
    return result


def _role_for_statistic(statistic: PlayerPropStatistic) -> PlayerPropRole:
    if statistic in BATTER_PLAYER_PROP_STATISTICS:
        return PlayerPropRole.BATTER
    if statistic in PITCHER_PLAYER_PROP_STATISTICS:
        return PlayerPropRole.PITCHER
    raise PlayerPropPredictionError("unsupported V8 player-prop statistic")


@dataclass(frozen=True, slots=True)
class PlayerPropProjectionV1:
    player_id: str
    statistic: PlayerPropStatistic
    line: float
    over_probability: float
    under_probability: float
    push_probability: float
    unresolved_probability: float
    source_distribution_checksum: str
    contract_version: str = PLAYER_PROP_PROJECTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "player_id", _text(self.player_id, "player_id"))
        line = _number(self.line, "line")
        if line < 0.0:
            raise PlayerPropPredictionError("player-prop line must be nonnegative")
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
            raise PlayerPropPredictionError("player-prop projection mass must sum to one")
        object.__setattr__(
            self,
            "source_distribution_checksum",
            _sha(self.source_distribution_checksum, "source_distribution_checksum"),
        )
        if self.contract_version != PLAYER_PROP_PROJECTION_CONTRACT_VERSION:
            raise PlayerPropPredictionError("unsupported V8 player-prop projection contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "line": self.line,
            "over_probability": self.over_probability,
            "player_id": self.player_id,
            "push_probability": self.push_probability,
            "source_distribution_checksum": self.source_distribution_checksum,
            "statistic": self.statistic.value,
            "under_probability": self.under_probability,
            "unresolved_probability": self.unresolved_probability,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PlayerPropPredictionV1:
    source_game_id: str
    player_id: str
    player_name: str
    team_id: str
    opponent_team_id: str
    role: PlayerPropRole
    statistic: PlayerPropStatistic
    target: PredictionTargetV1
    rollout_state: ModelRolloutState
    recommendation_eligible: bool
    source_model_input_checksum: str
    model_manifest_checksum: str
    model_id: str
    model_version: str
    model_scope: str
    expected_stat_count: float
    stat_distribution: DiscreteDistributionV1
    contract_version: str = PLAYER_PROP_PREDICTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "player_id", _text(self.player_id, "player_id"))
        object.__setattr__(self, "player_name", _text(self.player_name, "player_name"))
        if (
            self.team_id not in CANONICAL_TEAM_KEYS
            or self.opponent_team_id not in CANONICAL_TEAM_KEYS
            or self.team_id == self.opponent_team_id
        ):
            raise PlayerPropPredictionError("player-prop team identity is invalid")
        expected_role = _role_for_statistic(self.statistic)
        if self.role is not expected_role:
            raise PlayerPropPredictionError("player-prop role disagrees with statistic")
        if (
            self.target.source_game_id != self.source_game_id
            or self.target.family is not PredictionMarketFamily.PLAYER_PROP
            or self.target.period is not PredictionPeriod.FULL_GAME
            or self.target.subject_kind is not PredictionSubjectKind.PLAYER
            or self.target.subject_id != self.player_id
            or self.target.statistic != self.statistic.value
        ):
            raise PlayerPropPredictionError("player-prop prediction target is invalid")
        capability = capability_for_family(PredictionMarketFamily.PLAYER_PROP)
        if self.rollout_state is not capability.rollout_state:
            raise PlayerPropPredictionError("player-prop rollout state disagrees with capability")
        if self.recommendation_eligible is not capability.recommendation_eligible:
            raise PlayerPropPredictionError(
                "player-prop recommendation eligibility disagrees with capability"
            )
        for name in ("source_model_input_checksum", "model_manifest_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        object.__setattr__(self, "model_id", _text(self.model_id, "model_id"))
        object.__setattr__(self, "model_version", _text(self.model_version, "model_version"))
        object.__setattr__(self, "model_scope", _text(self.model_scope, "model_scope"))
        if self.model_scope != PLAYER_PROP_MODEL_SCOPE:
            raise PlayerPropPredictionError(
                "V8 player props require an independent player-prop model scope"
            )
        expected = _number(self.expected_stat_count, "expected_stat_count")
        if expected < 0.0:
            raise PlayerPropPredictionError("expected_stat_count must be nonnegative")
        object.__setattr__(self, "expected_stat_count", expected)
        if self.stat_distribution.kind is not PredictionDistributionKind.PLAYER_STAT_COUNT:
            raise PlayerPropPredictionError("V8 prediction requires PLAYER_STAT_COUNT distribution")
        if any(outcome.value < 0 for outcome in self.stat_distribution.outcomes):
            raise PlayerPropPredictionError("player-prop count outcomes must be nonnegative")
        if self.contract_version != PLAYER_PROP_PREDICTION_CONTRACT_VERSION:
            raise PlayerPropPredictionError("unsupported V8 player-prop prediction contract")

    @property
    def distribution_checksum(self) -> str:
        return self.stat_distribution.checksum

    def project(self, line: float) -> PlayerPropProjectionV1:
        selected = _number(line, "line")
        if selected < 0.0:
            raise PlayerPropPredictionError("player-prop line must be nonnegative")
        projection = self.stat_distribution.threshold_projection(selected)
        return PlayerPropProjectionV1(
            player_id=self.player_id,
            statistic=self.statistic,
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
            "expected_stat_count": self.expected_stat_count,
            "model_id": self.model_id,
            "model_manifest_checksum": self.model_manifest_checksum,
            "model_scope": self.model_scope,
            "model_version": self.model_version,
            "opponent_team_id": self.opponent_team_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "recommendation_eligible": self.recommendation_eligible,
            "role": self.role.value,
            "rollout_state": self.rollout_state.value,
            "source_game_id": self.source_game_id,
            "source_model_input_checksum": self.source_model_input_checksum,
            "stat_distribution": self.stat_distribution.as_dict(),
            "statistic": self.statistic.value,
            "target": self.target.as_dict(),
            "team_id": self.team_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def build_player_prop_prediction(
    *,
    source_game_id: str,
    player_id: str,
    player_name: str,
    team_id: str,
    opponent_team_id: str,
    role: PlayerPropRole,
    statistic: PlayerPropStatistic,
    source_model_input_checksum: str,
    model_manifest_checksum: str,
    model_id: str,
    model_version: str,
    expected_stat_count: float,
    stat_distribution: DiscreteDistributionV1,
) -> PlayerPropPredictionV1:
    if statistic not in SUPPORTED_PLAYER_PROP_STATISTICS:
        raise PlayerPropPredictionError("unsupported V8 player-prop statistic")
    capability = capability_for_family(PredictionMarketFamily.PLAYER_PROP)
    try:
        target = PredictionTargetV1(
            source_game_id=source_game_id,
            family=PredictionMarketFamily.PLAYER_PROP,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.PLAYER,
            subject_id=player_id,
            statistic=statistic.value,
        )
    except MultiMarketContractError as exc:
        raise PlayerPropPredictionError("invalid V8 player-prop target") from exc
    return PlayerPropPredictionV1(
        source_game_id=source_game_id,
        player_id=player_id,
        player_name=player_name,
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        role=role,
        statistic=statistic,
        target=target,
        rollout_state=capability.rollout_state,
        recommendation_eligible=capability.recommendation_eligible,
        source_model_input_checksum=source_model_input_checksum,
        model_manifest_checksum=model_manifest_checksum,
        model_id=model_id,
        model_version=model_version,
        model_scope=PLAYER_PROP_MODEL_SCOPE,
        expected_stat_count=expected_stat_count,
        stat_distribution=stat_distribution,
    )
