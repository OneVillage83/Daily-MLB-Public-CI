from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionMarketFamily,
    capabilities_for_version,
)
from app.value_engine.first_five import (
    FIRST_FIVE_BINDING_PENDING_REASON,
    FIRST_FIVE_REFERENCE_REASON,
    FirstFiveGameValueV1,
    FirstFiveOutcomeValueV1,
)

FIRST_FIVE_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_GATE_OUTCOME_V1"
FIRST_FIVE_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_GATE_GAME_V1"
FIRST_FIVE_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_FIRST_FIVE_REFERENCE_GATE_V1"
FIRST_FIVE_REFERENCE_GATE_REASON = "first_five_reference_only"
FIRST_FIVE_POLICY_PENDING_REASON = "production_policy_pending"

_SUPPORTED_FAMILIES = {
    PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value,
    PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value,
    PredictionMarketFamily.FIRST_FIVE_TOTAL.value,
}


class FirstFiveGateError(ValueError):
    """Raised when V4C First Five Gate evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstFiveGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstFiveGateError(f"{name} must be lowercase SHA-256")
    return text


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstFiveGateError(f"{name} must be finite numeric when present")
    result = float(value)
    if not math.isfinite(result):
        raise FirstFiveGateError(f"{name} must be finite numeric when present")
    return result


@dataclass(frozen=True, slots=True)
class FirstFiveGateOutcomeV1:
    source_game_id: str
    provider_event_id: str
    market_family: str
    market_key: str
    side: str
    line_key: str
    market_line: float | None
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = FIRST_FIVE_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = FIRST_FIVE_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.market_family not in _SUPPORTED_FAMILIES:
            raise FirstFiveGateError("unsupported First Five Gate market family")
        object.__setattr__(
            self,
            "market_line",
            _optional_number(self.market_line, "market_line"),
        )
        if self.decision != "pass":
            raise FirstFiveGateError(
                "V4C reference Gate may only emit PASS; production decisions require a version bump"
            )
        object.__setattr__(
            self,
            "source_value_checksum",
            _sha(self.source_value_checksum, "source_value_checksum"),
        )
        reasons = tuple(sorted({_text(item, "reason_code") for item in self.reason_codes}))
        for required in (
            FIRST_FIVE_REFERENCE_GATE_REASON,
            FIRST_FIVE_POLICY_PENDING_REASON,
            FIRST_FIVE_REFERENCE_REASON,
            FIRST_FIVE_BINDING_PENDING_REASON,
        ):
            if required not in reasons:
                raise FirstFiveGateError(
                    "reference First Five Gate lost a required lifecycle or binding blocker"
                )
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != FIRST_FIVE_REFERENCE_GATE_POLICY_VERSION:
            raise FirstFiveGateError("unsupported First Five reference Gate policy")
        if self.contract_version != FIRST_FIVE_GATE_OUTCOME_CONTRACT_VERSION:
            raise FirstFiveGateError("unsupported First Five Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line_key": self.line_key,
            "market_family": self.market_family,
            "market_key": self.market_key,
            "market_line": self.market_line,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "reason_codes": list(self.reason_codes),
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class FirstFiveGateGameV1:
    source_game_id: str
    provider_event_id: str
    source_value_game_checksum: str
    upstream_first_five_scoring_checksum: str
    outcomes: tuple[FirstFiveGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = FIRST_FIVE_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = FIRST_FIVE_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "source_value_game_checksum",
            _sha(self.source_value_game_checksum, "source_value_game_checksum"),
        )
        object.__setattr__(
            self,
            "upstream_first_five_scoring_checksum",
            _sha(
                self.upstream_first_five_scoring_checksum,
                "upstream_first_five_scoring_checksum",
            ),
        )
        outcomes = tuple(self.outcomes)
        if len({item.checksum for item in outcomes}) != len(outcomes):
            raise FirstFiveGateError("First Five Gate contains duplicate outcome evidence")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            for item in outcomes
        ):
            raise FirstFiveGateError("First Five Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        warnings = tuple(sorted({_text(item, "warning") for item in self.warnings}))
        object.__setattr__(self, "warnings", warnings)
        if not outcomes and not warnings:
            raise FirstFiveGateError("empty First Five Gate inventory requires a warning")
        if self.policy_version != FIRST_FIVE_REFERENCE_GATE_POLICY_VERSION:
            raise FirstFiveGateError("unsupported First Five reference Gate policy")
        if self.contract_version != FIRST_FIVE_GATE_GAME_CONTRACT_VERSION:
            raise FirstFiveGateError("unsupported First Five Gate game contract")

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
            "source_value_game_checksum": self.source_value_game_checksum,
            "upstream_first_five_scoring_checksum": self.upstream_first_five_scoring_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _reference_reasons(value: FirstFiveOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise FirstFiveGateError(
            "V4C reference Gate received recommendation-eligible evidence"
        )
    for required in (FIRST_FIVE_REFERENCE_REASON, FIRST_FIVE_BINDING_PENDING_REASON):
        if required not in value.ineligibility_reasons:
            raise FirstFiveGateError("First Five Value lost a required reference blocker")
    return tuple(
        sorted(
            {
                *value.ineligibility_reasons,
                FIRST_FIVE_REFERENCE_GATE_REASON,
                FIRST_FIVE_POLICY_PENDING_REASON,
            }
        )
    )


def evaluate_first_five_reference_gate(
    value_game: FirstFiveGameValueV1,
) -> FirstFiveGateGameV1:
    capabilities = capabilities_for_version(4)
    if len(capabilities) != 3 or any(
        item.rollout_state is not ModelRolloutState.REFERENCE
        or item.recommendation_eligible
        for item in capabilities
    ):
        raise FirstFiveGateError(
            "V4C reference Gate cannot run after First Five lifecycle promotion"
        )
    outcomes = tuple(
        FirstFiveGateOutcomeV1(
            source_game_id=value.source_game_id,
            provider_event_id=value.provider_event_id,
            market_family=value.market_family,
            market_key=value.market_key,
            side=value.side,
            line_key=value.line_key,
            market_line=value.market_line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reference_reasons(value),
        )
        for value in value_game.outcomes
    )
    warnings = value_game.warnings
    if not outcomes and not warnings:
        warnings = ("no_evaluable_first_five_gate_input",)
    return FirstFiveGateGameV1(
        source_game_id=value_game.source_game_id,
        provider_event_id=value_game.provider_event_id,
        source_value_game_checksum=value_game.checksum,
        upstream_first_five_scoring_checksum=(
            value_game.upstream_first_five_scoring_checksum
        ),
        outcomes=outcomes,
        warnings=warnings,
    )
