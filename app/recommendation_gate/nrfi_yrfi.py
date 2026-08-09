from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import ModelRolloutState, PredictionMarketFamily, capability_for_family
from app.value_engine.nrfi_yrfi import (
    NRFI_YRFI_BINDING_PENDING_REASON,
    NRFI_YRFI_REFERENCE_REASON,
    NrfiYrfiGameValueV1,
    NrfiYrfiOutcomeValueV1,
)

NRFI_YRFI_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_GATE_OUTCOME_V1"
NRFI_YRFI_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_GATE_GAME_V1"
NRFI_YRFI_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_NRFI_YRFI_REFERENCE_GATE_V1"
NRFI_YRFI_REFERENCE_GATE_REASON = "nrfi_yrfi_reference_only"
NRFI_YRFI_POLICY_PENDING_REASON = "production_policy_pending"


class NrfiYrfiGateError(ValueError):
    """Raised when V5C NRFI/YRFI Gate evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NrfiYrfiGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise NrfiYrfiGateError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class NrfiYrfiGateOutcomeV1:
    source_game_id: str
    provider_event_id: str
    side: str
    total_line: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = NRFI_YRFI_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = NRFI_YRFI_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        if self.side not in {"nrfi", "yrfi"}:
            raise NrfiYrfiGateError("NRFI/YRFI Gate side must be nrfi or yrfi")
        if self.total_line != 0.5:
            raise NrfiYrfiGateError("NRFI/YRFI Gate requires the 0.5 first-inning total")
        if self.decision != "pass":
            raise NrfiYrfiGateError("V5C reference Gate may only emit PASS")
        object.__setattr__(self, "source_value_checksum", _sha(self.source_value_checksum, "source_value_checksum"))
        reasons = tuple(sorted({_text(item, "reason_code") for item in self.reason_codes}))
        for required in (
            NRFI_YRFI_REFERENCE_REASON,
            NRFI_YRFI_BINDING_PENDING_REASON,
            NRFI_YRFI_REFERENCE_GATE_REASON,
            NRFI_YRFI_POLICY_PENDING_REASON,
        ):
            if required not in reasons:
                raise NrfiYrfiGateError("NRFI/YRFI Gate lost a required reference blocker")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != NRFI_YRFI_REFERENCE_GATE_POLICY_VERSION:
            raise NrfiYrfiGateError("unsupported NRFI/YRFI reference Gate policy")
        if self.contract_version != NRFI_YRFI_GATE_OUTCOME_CONTRACT_VERSION:
            raise NrfiYrfiGateError("unsupported NRFI/YRFI Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
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
class NrfiYrfiGateGameV1:
    source_game_id: str
    provider_event_id: str
    source_value_game_checksum: str
    source_prediction_checksum: str
    outcomes: tuple[NrfiYrfiGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = NRFI_YRFI_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = NRFI_YRFI_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(self, "source_value_game_checksum", _sha(self.source_value_game_checksum, "source_value_game_checksum"))
        object.__setattr__(self, "source_prediction_checksum", _sha(self.source_prediction_checksum, "source_prediction_checksum"))
        outcomes = tuple(self.outcomes)
        if len({item.side for item in outcomes}) != len(outcomes):
            raise NrfiYrfiGateError("NRFI/YRFI Gate contains duplicate sides")
        if any(item.source_game_id != self.source_game_id or item.provider_event_id != self.provider_event_id for item in outcomes):
            raise NrfiYrfiGateError("NRFI/YRFI Gate lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if not outcomes and not self.warnings:
            raise NrfiYrfiGateError("empty NRFI/YRFI Gate inventory requires a warning")
        if self.contract_version != NRFI_YRFI_GATE_GAME_CONTRACT_VERSION:
            raise NrfiYrfiGateError("unsupported NRFI/YRFI Gate game contract")

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
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_value_game_checksum": self.source_value_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _reasons(value: NrfiYrfiOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise NrfiYrfiGateError("V5C received recommendation-eligible Value evidence")
    return tuple(sorted({
        *value.ineligibility_reasons,
        NRFI_YRFI_REFERENCE_GATE_REASON,
        NRFI_YRFI_POLICY_PENDING_REASON,
    }))


def evaluate_nrfi_yrfi_reference_gate(value_game: NrfiYrfiGameValueV1) -> NrfiYrfiGateGameV1:
    capability = capability_for_family(PredictionMarketFamily.NRFI_YRFI)
    if capability.rollout_state is not ModelRolloutState.REFERENCE or capability.recommendation_eligible:
        raise NrfiYrfiGateError("V5C cannot run after NRFI/YRFI lifecycle promotion")
    outcomes = tuple(
        NrfiYrfiGateOutcomeV1(
            source_game_id=value.source_game_id,
            provider_event_id=value.provider_event_id,
            side=value.side,
            total_line=value.total_line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reasons(value),
        )
        for value in value_game.outcomes
    )
    warnings = value_game.warnings or (() if outcomes else ("no_evaluable_nrfi_yrfi_gate_input",))
    return NrfiYrfiGateGameV1(
        source_game_id=value_game.source_game_id,
        provider_event_id=value_game.provider_event_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksum=value_game.source_prediction_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
