from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionMarketFamily,
    capability_for_family,
)
from app.value_engine.game_total import GameTotalGameValueV1, GameTotalOutcomeValueV1

GAME_TOTAL_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_GATE_OUTCOME_V1"
GAME_TOTAL_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_GATE_GAME_V1"
GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_GAME_TOTAL_REFERENCE_GATE_V1"
GAME_TOTAL_REFERENCE_REASON = "game_total_reference_only"
GAME_TOTAL_POLICY_PENDING_REASON = "production_policy_pending"


class GameTotalGateError(ValueError):
    """Raised when V3C game-total Gate evidence violates its reference-safe contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GameTotalGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise GameTotalGateError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GameTotalGateError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GameTotalGateError(f"{name} must be finite numeric")
    return result


@dataclass(frozen=True, slots=True)
class GameTotalGateOutcomeV1:
    source_game_id: str
    side: str
    line_key: str
    total_line: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = GAME_TOTAL_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if self.side not in {"over", "under"}:
            raise GameTotalGateError("game-total Gate side must be over or under")
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        total_line = _number(self.total_line, "total_line")
        if total_line < 0.0:
            raise GameTotalGateError("total_line must be nonnegative")
        object.__setattr__(self, "total_line", total_line)
        if self.decision != "pass":
            raise GameTotalGateError(
                "V3C reference Gate may only emit PASS; production decisions require a versioned promotion"
            )
        object.__setattr__(self, "source_value_checksum", _sha(self.source_value_checksum, "source_value_checksum"))
        reasons = tuple(sorted({_text(reason, "reason_code") for reason in self.reason_codes}))
        if GAME_TOTAL_REFERENCE_REASON not in reasons or GAME_TOTAL_POLICY_PENDING_REASON not in reasons:
            raise GameTotalGateError("reference Gate must retain lifecycle and production-policy blockers")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION:
            raise GameTotalGateError("unsupported game-total reference Gate policy")
        if self.contract_version != GAME_TOTAL_GATE_OUTCOME_CONTRACT_VERSION:
            raise GameTotalGateError("unsupported game-total Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line_key": self.line_key,
            "policy_version": self.policy_version,
            "reason_codes": list(self.reason_codes),
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
            "total_line": self.total_line,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GameTotalGateGameV1:
    source_game_id: str
    away_team_id: str
    home_team_id: str
    source_value_game_checksum: str
    source_prediction_checksum: str
    outcomes: tuple[GameTotalGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = GAME_TOTAL_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "away_team_id", _text(self.away_team_id, "away_team_id"))
        object.__setattr__(self, "home_team_id", _text(self.home_team_id, "home_team_id"))
        if self.away_team_id == self.home_team_id:
            raise GameTotalGateError("game-total Gate teams must differ")
        object.__setattr__(self, "source_value_game_checksum", _sha(self.source_value_game_checksum, "source_value_game_checksum"))
        object.__setattr__(self, "source_prediction_checksum", _sha(self.source_prediction_checksum, "source_prediction_checksum"))
        outcomes = tuple(self.outcomes)
        if len({outcome.checksum for outcome in outcomes}) != len(outcomes):
            raise GameTotalGateError("game-total Gate contains duplicate outcome evidence")
        if any(outcome.source_game_id != self.source_game_id for outcome in outcomes):
            raise GameTotalGateError("game-total Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        warnings = tuple(sorted({_text(warning, "warning") for warning in self.warnings}))
        object.__setattr__(self, "warnings", warnings)
        if not outcomes and not warnings:
            raise GameTotalGateError("empty game-total Gate inventory requires an explicit warning")
        if self.policy_version != GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION:
            raise GameTotalGateError("unsupported game-total reference Gate policy")
        if self.contract_version != GAME_TOTAL_GATE_GAME_CONTRACT_VERSION:
            raise GameTotalGateError("unsupported game-total Gate game contract")

    @property
    def recommendation_count(self) -> int:
        return sum(outcome.decision == "recommend" for outcome in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "policy_version": self.policy_version,
            "source_game_id": self.source_game_id,
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_value_game_checksum": self.source_value_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _reference_reasons(value: GameTotalOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise GameTotalGateError(
            "V3C reference Gate received recommendation-eligible evidence; production promotion must be versioned"
        )
    if "prediction_not_recommendation_eligible" not in value.ineligibility_reasons:
        raise GameTotalGateError("reference game-total Value lost its model-lifecycle blocker")
    return tuple(
        sorted(
            {
                *value.ineligibility_reasons,
                GAME_TOTAL_REFERENCE_REASON,
                GAME_TOTAL_POLICY_PENDING_REASON,
            }
        )
    )


def evaluate_game_total_reference_gate(value_game: GameTotalGameValueV1) -> GameTotalGateGameV1:
    capability = capability_for_family(PredictionMarketFamily.GAME_TOTAL)
    if capability.rollout_state is not ModelRolloutState.REFERENCE or capability.recommendation_eligible:
        raise GameTotalGateError(
            "V3C reference Gate cannot run after Game Total promotion; create a versioned production policy"
        )
    outcomes = tuple(
        GameTotalGateOutcomeV1(
            source_game_id=value.source_game_id,
            side=value.side,
            line_key=value.line_key,
            total_line=value.total_line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reference_reasons(value),
        )
        for value in value_game.values
    )
    warnings = value_game.warnings
    if not outcomes and not warnings:
        warnings = ("no_evaluable_game_total_gate_input",)
    return GameTotalGateGameV1(
        source_game_id=value_game.source_game_id,
        away_team_id=value_game.away_team_id,
        home_team_id=value_game.home_team_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksum=value_game.source_prediction_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
