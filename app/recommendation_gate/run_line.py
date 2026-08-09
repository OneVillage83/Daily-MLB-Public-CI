from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionMarketFamily,
    capability_for_family,
)
from app.value_engine.run_line import RunLineGameValueV1, RunLineOutcomeValueV1

RUN_LINE_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_GATE_OUTCOME_V1"
RUN_LINE_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_GATE_GAME_V1"
RUN_LINE_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_RUN_LINE_REFERENCE_GATE_V1"
RUN_LINE_REFERENCE_REASON = "run_line_reference_only"
RUN_LINE_POLICY_PENDING_REASON = "production_policy_pending"


class RunLineGateError(ValueError):
    """Raised when V2C run-line Gate evidence violates its reference-safe contract."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RunLineGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RunLineGateError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class RunLineGateOutcomeV1:
    """Reference-only Gate evidence for one retained run-line side/point."""

    source_game_id: str
    outcome_team_id: str
    side: str
    line_key: str
    side_spread: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = RUN_LINE_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = RUN_LINE_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "outcome_team_id", _required_text(self.outcome_team_id, "outcome_team_id"))
        if self.side not in {"home", "away"}:
            raise RunLineGateError("run-line Gate side must be home or away")
        object.__setattr__(self, "line_key", _required_text(self.line_key, "line_key"))
        if isinstance(self.side_spread, bool) or not isinstance(self.side_spread, int | float):
            raise RunLineGateError("side_spread must be numeric")
        object.__setattr__(self, "side_spread", float(self.side_spread))
        if self.decision != "pass":
            raise RunLineGateError(
                "V2C reference Gate may only emit PASS; production decisions require a versioned promotion"
            )
        object.__setattr__(self, "source_value_checksum", _sha(self.source_value_checksum, "source_value_checksum"))
        reasons = tuple(sorted({_required_text(reason, "reason_code") for reason in self.reason_codes}))
        if RUN_LINE_REFERENCE_REASON not in reasons or RUN_LINE_POLICY_PENDING_REASON not in reasons:
            raise RunLineGateError("reference Gate must retain lifecycle and production-policy blockers")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != RUN_LINE_REFERENCE_GATE_POLICY_VERSION:
            raise RunLineGateError("unsupported run-line reference Gate policy")
        if self.contract_version != RUN_LINE_GATE_OUTCOME_CONTRACT_VERSION:
            raise RunLineGateError("unsupported run-line Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line_key": self.line_key,
            "outcome_team_id": self.outcome_team_id,
            "policy_version": self.policy_version,
            "reason_codes": list(self.reason_codes),
            "side": self.side,
            "side_spread": self.side_spread,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RunLineGateGameV1:
    """V2C Gate inventory for one game; every evaluable V2B value row is retained."""

    source_game_id: str
    away_team_id: str
    home_team_id: str
    source_value_game_checksum: str
    source_prediction_checksum: str
    outcomes: tuple[RunLineGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = RUN_LINE_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = RUN_LINE_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "away_team_id", _required_text(self.away_team_id, "away_team_id"))
        object.__setattr__(self, "home_team_id", _required_text(self.home_team_id, "home_team_id"))
        if self.away_team_id == self.home_team_id:
            raise RunLineGateError("run-line Gate teams must differ")
        object.__setattr__(
            self,
            "source_value_game_checksum",
            _sha(self.source_value_game_checksum, "source_value_game_checksum"),
        )
        object.__setattr__(
            self,
            "source_prediction_checksum",
            _sha(self.source_prediction_checksum, "source_prediction_checksum"),
        )
        outcomes = tuple(self.outcomes)
        if len({outcome.checksum for outcome in outcomes}) != len(outcomes):
            raise RunLineGateError("run-line Gate contains duplicate outcome evidence")
        if any(
            outcome.source_game_id != self.source_game_id
            or outcome.outcome_team_id not in {self.away_team_id, self.home_team_id}
            for outcome in outcomes
        ):
            raise RunLineGateError("run-line Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        warnings = tuple(sorted({_required_text(warning, "warning") for warning in self.warnings}))
        object.__setattr__(self, "warnings", warnings)
        if not outcomes and not warnings:
            raise RunLineGateError("empty run-line Gate inventory requires an explicit warning")
        if self.policy_version != RUN_LINE_REFERENCE_GATE_POLICY_VERSION:
            raise RunLineGateError("unsupported run-line reference Gate policy")
        if self.contract_version != RUN_LINE_GATE_GAME_CONTRACT_VERSION:
            raise RunLineGateError("unsupported run-line Gate game contract")

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


def _reference_reasons(value: RunLineOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise RunLineGateError(
            "V2C reference Gate received recommendation-eligible evidence; production promotion must be versioned"
        )
    if "prediction_not_recommendation_eligible" not in value.ineligibility_reasons:
        raise RunLineGateError("reference run-line value lost its model-lifecycle blocker")
    return tuple(
        sorted(
            {
                *value.ineligibility_reasons,
                RUN_LINE_REFERENCE_REASON,
                RUN_LINE_POLICY_PENDING_REASON,
            }
        )
    )


def evaluate_run_line_reference_gate(value_game: RunLineGameValueV1) -> RunLineGateGameV1:
    """Convert V2B value evidence to durable PASS evidence without inventing production thresholds."""

    capability = capability_for_family(PredictionMarketFamily.RUN_LINE)
    if (
        capability.rollout_state is not ModelRolloutState.REFERENCE
        or capability.recommendation_eligible
    ):
        raise RunLineGateError(
            "V2C reference Gate cannot run after Run Line is promoted; create a versioned production Gate policy"
        )
    outcomes = tuple(
        RunLineGateOutcomeV1(
            source_game_id=value.source_game_id,
            outcome_team_id=value.outcome_team_id,
            side=value.side,
            line_key=value.line_key,
            side_spread=value.side_spread,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reference_reasons(value),
        )
        for value in value_game.values
    )
    warnings = value_game.warnings
    if not outcomes and not warnings:
        warnings = ("no_evaluable_run_line_gate_input",)
    return RunLineGateGameV1(
        source_game_id=value_game.source_game_id,
        away_team_id=value_game.away_team_id,
        home_team_id=value_game.home_team_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksum=value_game.source_prediction_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
