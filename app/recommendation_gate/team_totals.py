from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import ModelRolloutState, PredictionMarketFamily, capability_for_family
from app.value_engine.team_totals import (
    TEAM_TOTAL_BINDING_PENDING_REASON,
    TEAM_TOTAL_REFERENCE_REASON,
    TeamTotalOutcomeValueV1,
    TeamTotalsGameValueV1,
)

TEAM_TOTAL_GATE_OUTCOME_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_GATE_OUTCOME_V1"
TEAM_TOTAL_GATE_GAME_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_GATE_GAME_V1"
TEAM_TOTAL_REFERENCE_GATE_POLICY_VERSION = "DSE_MLB_TEAM_TOTAL_REFERENCE_GATE_V1"
TEAM_TOTAL_REFERENCE_GATE_REASON = "team_total_reference_only"
TEAM_TOTAL_POLICY_PENDING_REASON = "production_policy_pending"


class TeamTotalsGateError(ValueError):
    """Raised when V6C team-total Gate evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TeamTotalsGateError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TeamTotalsGateError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class TeamTotalGateOutcomeV1:
    source_game_id: str
    provider_event_id: str
    team_id: str
    opponent_team_id: str
    side: str
    line_key: str
    total_line: float
    decision: str
    source_value_checksum: str
    reason_codes: tuple[str, ...]
    policy_version: str = TEAM_TOTAL_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = TEAM_TOTAL_GATE_OUTCOME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("source_game_id", "provider_event_id", "team_id", "opponent_team_id", "line_key"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise TeamTotalsGateError("team-total Gate subject and opponent must differ")
        if self.side not in {"over", "under"}:
            raise TeamTotalsGateError("team-total Gate side must be over or under")
        if self.total_line < 0.0:
            raise TeamTotalsGateError("team-total Gate line must be nonnegative")
        if self.decision != "pass":
            raise TeamTotalsGateError("V6C reference Gate may only emit PASS")
        object.__setattr__(self, "source_value_checksum", _sha(self.source_value_checksum, "source_value_checksum"))
        reasons = tuple(sorted({_text(item, "reason_code") for item in self.reason_codes}))
        for required in (
            TEAM_TOTAL_REFERENCE_REASON,
            TEAM_TOTAL_BINDING_PENDING_REASON,
            TEAM_TOTAL_REFERENCE_GATE_REASON,
            TEAM_TOTAL_POLICY_PENDING_REASON,
        ):
            if required not in reasons:
                raise TeamTotalsGateError("team-total Gate lost a required reference blocker")
        object.__setattr__(self, "reason_codes", reasons)
        if self.policy_version != TEAM_TOTAL_REFERENCE_GATE_POLICY_VERSION:
            raise TeamTotalsGateError("unsupported team-total reference Gate policy")
        if self.contract_version != TEAM_TOTAL_GATE_OUTCOME_CONTRACT_VERSION:
            raise TeamTotalsGateError("unsupported team-total Gate outcome contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "line_key": self.line_key,
            "opponent_team_id": self.opponent_team_id,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "reason_codes": list(self.reason_codes),
            "side": self.side,
            "source_game_id": self.source_game_id,
            "source_value_checksum": self.source_value_checksum,
            "team_id": self.team_id,
            "total_line": self.total_line,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class TeamTotalsGateGameV1:
    source_game_id: str
    provider_event_id: str
    away_team_id: str
    home_team_id: str
    source_value_game_checksum: str
    source_prediction_checksums: tuple[str, str]
    source_normalized_odds_checksum: str
    outcomes: tuple[TeamTotalGateOutcomeV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = TEAM_TOTAL_REFERENCE_GATE_POLICY_VERSION
    contract_version: str = TEAM_TOTAL_GATE_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in ("source_game_id", "provider_event_id", "away_team_id", "home_team_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.away_team_id == self.home_team_id:
            raise TeamTotalsGateError("team-total Gate game teams must differ")
        object.__setattr__(self, "source_value_game_checksum", _sha(self.source_value_game_checksum, "source_value_game_checksum"))
        checksums = tuple(self.source_prediction_checksums)
        if len(checksums) != 2:
            raise TeamTotalsGateError("team-total Gate requires away/home prediction checksums")
        object.__setattr__(self, "source_prediction_checksums", (_sha(checksums[0], "away prediction checksum"), _sha(checksums[1], "home prediction checksum")))
        object.__setattr__(self, "source_normalized_odds_checksum", _sha(self.source_normalized_odds_checksum, "source_normalized_odds_checksum"))
        outcomes = tuple(self.outcomes)
        identities = {(item.team_id, item.side, item.line_key, item.total_line) for item in outcomes}
        if len(identities) != len(outcomes):
            raise TeamTotalsGateError("team-total Gate contains duplicate outcome evidence")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.team_id not in {self.away_team_id, self.home_team_id}
            for item in outcomes
        ):
            raise TeamTotalsGateError("team-total Gate outcome lineage mismatch")
        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if not outcomes and not self.warnings:
            raise TeamTotalsGateError("empty team-total Gate inventory requires a warning")
        if self.policy_version != TEAM_TOTAL_REFERENCE_GATE_POLICY_VERSION:
            raise TeamTotalsGateError("unsupported team-total reference Gate policy")
        if self.contract_version != TEAM_TOTAL_GATE_GAME_CONTRACT_VERSION:
            raise TeamTotalsGateError("unsupported team-total Gate game contract")

    @property
    def recommendation_count(self) -> int:
        return sum(item.decision == "recommend" for item in self.outcomes)

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
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


def _reasons(value: TeamTotalOutcomeValueV1) -> tuple[str, ...]:
    if value.prediction_recommendation_eligible or value.recommendation_gate_input_eligible:
        raise TeamTotalsGateError("V6C received recommendation-eligible Value evidence")
    return tuple(sorted({*value.ineligibility_reasons, TEAM_TOTAL_REFERENCE_GATE_REASON, TEAM_TOTAL_POLICY_PENDING_REASON}))


def evaluate_team_totals_reference_gate(value_game: TeamTotalsGameValueV1) -> TeamTotalsGateGameV1:
    capability = capability_for_family(PredictionMarketFamily.TEAM_TOTAL)
    if capability.rollout_state is not ModelRolloutState.REFERENCE or capability.recommendation_eligible:
        raise TeamTotalsGateError("V6C cannot run after team-total lifecycle promotion")
    outcomes = tuple(
        TeamTotalGateOutcomeV1(
            source_game_id=value.source_game_id,
            provider_event_id=value.provider_event_id,
            team_id=value.team_id,
            opponent_team_id=value.opponent_team_id,
            side=value.side,
            line_key=value.line_key,
            total_line=value.total_line,
            decision="pass",
            source_value_checksum=value.checksum,
            reason_codes=_reasons(value),
        )
        for value in value_game.outcomes
    )
    warnings = value_game.warnings or (() if outcomes else ("no_evaluable_team_total_gate_input",))
    return TeamTotalsGateGameV1(
        source_game_id=value_game.source_game_id,
        provider_event_id=value_game.provider_event_id,
        away_team_id=value_game.away_team_id,
        home_team_id=value_game.home_team_id,
        source_value_game_checksum=value_game.checksum,
        source_prediction_checksums=value_game.source_prediction_checksums,
        source_normalized_odds_checksum=value_game.source_normalized_odds_checksum,
        outcomes=outcomes,
        warnings=warnings,
    )
