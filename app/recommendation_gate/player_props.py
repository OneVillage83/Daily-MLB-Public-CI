from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionMarketFamily,
    capability_for_family,
)
from app.value_engine.player_props import (
    PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
    PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
    PLAYER_PROP_REFERENCE_REASON,
    PlayerPropOutcomeValueV1,
    PlayerPropsGameValueV1,
)

PLAYER_PROP_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_GATE_OUTCOME_V1"
PLAYER_PROP_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_GATE_GAME_V1"
PLAYER_PROP_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_PLAYER_PROP_REFERENCE_GATE_V1"
PLAYER_PROP_REFERENCE_GATE_REASON = "player_prop_reference_only"
PLAYER_PROP_POLICY_PENDING_REASON = "production_policy_pending"


class PlayerPropsGateError(ValueError):
    """Raised when V8C player-prop Gate evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlayerPropsGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PlayerPropsGateError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlayerPropsGateError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlayerPropsGateError(f"{name} must be finite numeric")
    return result


@dataclass(frozen=True, slots=True)
class PlayerPropGateOutcomeV1:
    source_game_id: str
    provider_event_id: str
    player_id: str
    player_name: str
    provider_player_key: str
    team_id: str
    opponent_team_id: str
    role: str
    statistic: str
    market_key: str
    side: str
    line_key: str
    line: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = PLAYER_PROP_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = PLAYER_PROP_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "player_id",
            "player_name",
            "provider_player_key",
            "team_id",
            "opponent_team_id",
            "role",
            "statistic",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise PlayerPropsGateError("player-prop Gate team and opponent must differ")
        if self.side not in {"over", "under"}:
            raise PlayerPropsGateError("player-prop Gate side must be over or under")
        line = _number(self.line, "line")
        if line < 0.0:
            raise PlayerPropsGateError("player-prop Gate line must be nonnegative")
        object.__setattr__(self, "line", line)
        if self.decision != "pass":
            raise PlayerPropsGateError("V8C reference Gate may only emit PASS")
        object.__setattr__(
            self,
            "source_value_checksum",
            _sha(self.source_value_checksum, "source_value_checksum"),
        )
        reasons = tuple(sorted({_text(item, "reason_code") for item in self.reason_codes}))
        for required in (
            PLAYER_PROP_REFERENCE_REASON,
            PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
            PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
            PLAYER_PROP_REFERENCE_GATE_REASON,
            PLAYER_PROP_POLICY_PENDING_REASON,
        ):
            if required not in reasons:
                raise PlayerPropsGateError("player-prop Gate lost a required reference blocker")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != PLAYER_PROP_REFERENCE_GATE_POLICY_VERSION:
            raise PlayerPropsGateError("unsupported V8C player-prop Gate policy")
        if self.contract_version != PLAYER_PROP_GATE_OUTCOME_CONTRACT_VERSION:
            raise PlayerPropsGateError("unsupported V8C player-prop Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line": self.line,
            "line_key": self.line_key,
            "market_key": self.market_key,
            "opponent_team_id": self.opponent_team_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "provider_player_key": self.provider_player_key,
            "reason_codes": list(self.reason_codes),
            "role": self.role,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
            "statistic": self.statistic,
            "team_id": self.team_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PlayerPropsGateGameV1:
    source_game_id: str
    provider_event_id: str
    source_value_game_checksum: str
    source_prediction_checksums: tuple[str, ...]
    source_normalized_odds_checksum: str
    outcomes: tuple[PlayerPropGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = PLAYER_PROP_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = PLAYER_PROP_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "source_value_game_checksum",
            _sha(self.source_value_game_checksum, "source_value_game_checksum"),
        )
        checksums = tuple(_sha(item, "source_prediction_checksum") for item in self.source_prediction_checksums)
        if len(checksums) != len(set(checksums)):
            raise PlayerPropsGateError("V8C Gate contains duplicate prediction checksums")
        object.__setattr__(self, "source_prediction_checksums", checksums)
        object.__setattr__(
            self,
            "source_normalized_odds_checksum",
            _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"),
        )
        outcomes = tuple(self.outcomes)
        identities = {
            (
                item.player_id,
                item.statistic,
                item.market_key,
                item.side,
                item.line_key,
                item.line,
            )
            for item in outcomes
        }
        if len(identities) != len(outcomes):
            raise PlayerPropsGateError("V8C Gate contains duplicate outcome evidence")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            for item in outcomes
        ):
            raise PlayerPropsGateError("V8C Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if not outcomes and not self.warnings:
            raise PlayerPropsGateError("empty V8C Gate inventory requires a warning")
        if self.policy_version != PLAYER_PROP_REFERENCE_GATE_POLICY_VERSION:
            raise PlayerPropsGateError("unsupported V8C player-prop Gate policy")
        if self.contract_version != PLAYER_PROP_GATE_GAME_CONTRACT_VERSION:
            raise PlayerPropsGateError("unsupported V8C player-prop Gate game contract")

    @property
    def recommendation_count(self) -> int:
        return sum(item.decision == "recommend" for item in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "outcomes": [item.as_dict() for item in self.outcomes],
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "recommendation_count": self.recommendation_count,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksums": list(self.source_prediction_checksums),
            "source_value_game_checksum": self.source_value_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _reason_codes(value: PlayerPropOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise PlayerPropsGateError("V8C received recommendation-eligible Value evidence")
    return tuple(
        sorted(
            {
                *value.ineligibility_reasons,
                PLAYER_PROP_REFERENCE_GATE_REASON,
                PLAYER_PROP_POLICY_PENDING_REASON,
            }
        )
    )


def evaluate_player_props_reference_gate(
    value_game: PlayerPropsGameValueV1,
) -> PlayerPropsGateGameV1:
    capability = capability_for_family(PredictionMarketFamily.PLAYER_PROP)
    if capability.rollout_state is not ModelRolloutState.REFERENCE or capability.recommendation_eligible:
        raise PlayerPropsGateError("V8C cannot run after player-prop lifecycle promotion")
    outcomes = tuple(
        PlayerPropGateOutcomeV1(
            source_game_id=value.source_game_id,
            provider_event_id=value.provider_event_id,
            player_id=value.player_id,
            player_name=value.player_name,
            provider_player_key=value.provider_player_key,
            team_id=value.team_id,
            opponent_team_id=value.opponent_team_id,
            role=value.role,
            statistic=value.statistic,
            market_key=value.market_key,
            side=value.side,
            line_key=value.line_key,
            line=value.line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reason_codes(value),
        )
        for value in value_game.outcomes
    )
    warnings = value_game.warnings or (() if outcomes else ("no_evaluable_player_prop_gate_input",))
    return PlayerPropsGateGameV1(
        source_game_id=value_game.source_game_id,
        provider_event_id=value_game.provider_event_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksums=value_game.source_prediction_checksums,
        source_normalized_odds_checksum=value_game.source_normalized_odds_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
