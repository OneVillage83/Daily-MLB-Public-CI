from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionMarketFamily,
    capability_for_family,
)
from app.value_engine.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
    PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
    PITCHER_STRIKEOUT_REFERENCE_REASON,
    PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON,
    PitcherStrikeoutGameValueV1,
    PitcherStrikeoutOutcomeValueV1,
)

PITCHER_STRIKEOUT_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_GATE_OUTCOME_V1"
PITCHER_STRIKEOUT_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_GATE_GAME_V1"
PITCHER_STRIKEOUT_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_REFERENCE_GATE_V1"
PITCHER_STRIKEOUT_REFERENCE_GATE_REASON = "pitcher_strikeout_reference_only"
PITCHER_STRIKEOUT_POLICY_PENDING_REASON = "production_policy_pending"


class PitcherStrikeoutGateError(ValueError):
    """Raised when V7C pitcher-strikeout Gate evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PitcherStrikeoutGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PitcherStrikeoutGateError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutGateOutcomeV1:
    source_game_id: str
    provider_event_id: str
    pitcher_id: str
    pitcher_name: str
    provider_pitcher_key: str
    team_id: str
    opponent_team_id: str
    starter_binding_state: str
    starter_binding_checksum: str
    market_key: str
    side: str
    line_key: str
    line: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = PITCHER_STRIKEOUT_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = PITCHER_STRIKEOUT_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "pitcher_id",
            "pitcher_name",
            "provider_pitcher_key",
            "team_id",
            "opponent_team_id",
            "starter_binding_state",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise PitcherStrikeoutGateError("V7 Gate team and opponent must differ")
        if self.starter_binding_state not in {"expected", "confirmed"}:
            raise PitcherStrikeoutGateError("V7 Gate starter state must be expected or confirmed")
        object.__setattr__(
            self,
            "starter_binding_checksum",
            _sha(self.starter_binding_checksum, "starter_binding_checksum"),
        )
        if self.side not in {"over", "under"}:
            raise PitcherStrikeoutGateError("V7 Gate side must be over or under")
        if self.line < 0.0:
            raise PitcherStrikeoutGateError("V7 Gate line must be nonnegative")
        if self.decision != "pass":
            raise PitcherStrikeoutGateError("V7C reference Gate may only emit PASS")
        object.__setattr__(
            self,
            "source_value_checksum",
            _sha(self.source_value_checksum, "source_value_checksum"),
        )
        reasons = tuple(sorted({_text(item, "reason_code") for item in self.reason_codes}))
        for required in (
            PITCHER_STRIKEOUT_REFERENCE_REASON,
            PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
            PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
            PITCHER_STRIKEOUT_REFERENCE_GATE_REASON,
            PITCHER_STRIKEOUT_POLICY_PENDING_REASON,
        ):
            if required not in reasons:
                raise PitcherStrikeoutGateError("V7 Gate lost a required reference blocker")
        if (
            self.starter_binding_state == "expected"
            and PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON not in reasons
        ):
            raise PitcherStrikeoutGateError("expected starter Gate row lost confirmation blocker")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != PITCHER_STRIKEOUT_REFERENCE_GATE_POLICY_VERSION:
            raise PitcherStrikeoutGateError("unsupported V7 reference Gate policy")
        if self.contract_version != PITCHER_STRIKEOUT_GATE_OUTCOME_CONTRACT_VERSION:
            raise PitcherStrikeoutGateError("unsupported V7 Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line": self.line,
            "line_key": self.line_key,
            "market_key": self.market_key,
            "opponent_team_id": self.opponent_team_id,
            "pitcher_id": self.pitcher_id,
            "pitcher_name": self.pitcher_name,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "provider_pitcher_key": self.provider_pitcher_key,
            "reason_codes": list(self.reason_codes),
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
            "starter_binding_checksum": self.starter_binding_checksum,
            "starter_binding_state": self.starter_binding_state,
            "team_id": self.team_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutGateGameV1:
    source_game_id: str
    provider_event_id: str
    source_value_game_checksum: str
    source_prediction_checksum: str
    source_normalized_odds_checksum: str
    outcomes: tuple[PitcherStrikeoutGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = PITCHER_STRIKEOUT_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = PITCHER_STRIKEOUT_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        for name in (
            "source_value_game_checksum",
            "source_prediction_checksum",
            "source_normalized_odds_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        outcomes = tuple(self.outcomes)
        identities = {
            (item.pitcher_id, item.market_key, item.side, item.line_key, item.line)
            for item in outcomes
        }
        if len(identities) != len(outcomes):
            raise PitcherStrikeoutGateError("V7 Gate contains duplicate outcome evidence")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            for item in outcomes
        ):
            raise PitcherStrikeoutGateError("V7 Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if not outcomes and not self.warnings:
            raise PitcherStrikeoutGateError("empty V7 Gate inventory requires a warning")
        if self.policy_version != PITCHER_STRIKEOUT_REFERENCE_GATE_POLICY_VERSION:
            raise PitcherStrikeoutGateError("unsupported V7 reference Gate policy")
        if self.contract_version != PITCHER_STRIKEOUT_GATE_GAME_CONTRACT_VERSION:
            raise PitcherStrikeoutGateError("unsupported V7 Gate game contract")

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
            "source_prediction_checksum": self.source_prediction_checksum,
            "source_value_game_checksum": self.source_value_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _reasons(value: PitcherStrikeoutOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise PitcherStrikeoutGateError("V7C received recommendation-eligible Value evidence")
    return tuple(
        sorted(
            {
                *value.ineligibility_reasons,
                PITCHER_STRIKEOUT_REFERENCE_GATE_REASON,
                PITCHER_STRIKEOUT_POLICY_PENDING_REASON,
            }
        )
    )


def evaluate_pitcher_strikeout_reference_gate(
    value_game: PitcherStrikeoutGameValueV1,
) -> PitcherStrikeoutGateGameV1:
    capability = capability_for_family(PredictionMarketFamily.PITCHER_STRIKEOUTS)
    if (
        capability.rollout_state is not ModelRolloutState.REFERENCE
        or capability.recommendation_eligible
    ):
        raise PitcherStrikeoutGateError("V7C cannot run after pitcher-strikeout lifecycle promotion")
    outcomes = tuple(
        PitcherStrikeoutGateOutcomeV1(
            source_game_id=value.source_game_id,
            provider_event_id=value.provider_event_id,
            pitcher_id=value.pitcher_id,
            pitcher_name=value.pitcher_name,
            provider_pitcher_key=value.provider_pitcher_key,
            team_id=value.team_id,
            opponent_team_id=value.opponent_team_id,
            starter_binding_state=value.starter_binding_state,
            starter_binding_checksum=value.starter_binding_checksum,
            market_key=value.market_key,
            side=value.side,
            line_key=value.line_key,
            line=value.line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reasons(value),
        )
        for value in value_game.outcomes
    )
    warnings = value_game.warnings or (() if outcomes else ("no_evaluable_pitcher_strikeout_gate_input",))
    return PitcherStrikeoutGateGameV1(
        source_game_id=value_game.source_game_id,
        provider_event_id=value_game.provider_event_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksum=value_game.source_prediction_checksum,
        source_normalized_odds_checksum=value_game.source_normalized_odds_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
