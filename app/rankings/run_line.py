from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.run_line import RunLineGateGameV1, RunLineGateOutcomeV1

RUN_LINE_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_RANKING_ENTRY_V1"
RUN_LINE_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_RANKING_GAME_V1"
RUN_LINE_RANKINGS_CONTRACT_VERSION = "DSE_MLB_RUN_LINE_RANKINGS_V1"
RUN_LINE_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_RUN_LINE_REFERENCE_RANKING_V1"


class RunLineRankingsError(ValueError):
    """Raised when V2C run-line ranking evidence violates its reference-safe contract."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RunLineRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RunLineRankingsError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class RunLineRankingEntryV1:
    source_game_id: str
    outcome_team_id: str
    side: str
    line_key: str
    side_spread: float
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = RUN_LINE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = RUN_LINE_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "outcome_team_id", _required_text(self.outcome_team_id, "outcome_team_id"))
        if self.side not in {"home", "away"}:
            raise RunLineRankingsError("run-line ranking side must be home or away")
        object.__setattr__(self, "line_key", _required_text(self.line_key, "line_key"))
        if isinstance(self.side_spread, bool) or not isinstance(self.side_spread, int | float):
            raise RunLineRankingsError("side_spread must be numeric")
        object.__setattr__(self, "side_spread", float(self.side_spread))
        if self.decision != "pass":
            raise RunLineRankingsError(
                "V2C reference rankings may only consume PASS evidence; production ranking requires a version bump"
            )
        if self.rank_eligible or self.recommendation_rank is not None:
            raise RunLineRankingsError("reference run-line evidence cannot receive a recommendation rank")
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"),
        )
        reasons = tuple(sorted({_required_text(reason, "exclusion_reason") for reason in self.exclusion_reasons}))
        if not reasons:
            raise RunLineRankingsError("unranked reference evidence requires explicit exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != RUN_LINE_REFERENCE_RANKING_POLICY_VERSION:
            raise RunLineRankingsError("unsupported run-line reference ranking policy")
        if self.contract_version != RUN_LINE_RANKING_ENTRY_CONTRACT_VERSION:
            raise RunLineRankingsError("unsupported run-line ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line_key": self.line_key,
            "outcome_team_id": self.outcome_team_id,
            "policy_version": self.policy_version,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "side": self.side,
            "side_spread": self.side_spread,
            "source_game_id": self.source_game_id,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RunLineRankingGameV1:
    ordinal: int
    source_game_id: str
    upstream_gate_game_checksum: str
    entries: tuple[RunLineRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = RUN_LINE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = RUN_LINE_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise RunLineRankingsError("run-line ranking game ordinal must be a positive integer")
        object.__setattr__(self, "source_game_id", _required_text(self.source_game_id, "source_game_id"))
        object.__setattr__(
            self,
            "upstream_gate_game_checksum",
            _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"),
        )
        entries = tuple(self.entries)
        if len({entry.checksum for entry in entries}) != len(entries):
            raise RunLineRankingsError("run-line ranking game contains duplicate entries")
        if any(entry.source_game_id != self.source_game_id for entry in entries):
            raise RunLineRankingsError("run-line ranking entry game identity mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_required_text(warning, "warning") for warning in self.warnings})),
        )
        if self.policy_version != RUN_LINE_REFERENCE_RANKING_POLICY_VERSION:
            raise RunLineRankingsError("unsupported run-line reference ranking policy")
        if self.contract_version != RUN_LINE_RANKING_GAME_CONTRACT_VERSION:
            raise RunLineRankingsError("unsupported run-line ranking game contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entries": [entry.as_dict() for entry in self.entries],
            "ordinal": self.ordinal,
            "policy_version": self.policy_version,
            "source_game_id": self.source_game_id,
            "upstream_gate_game_checksum": self.upstream_gate_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RunLineReferenceRankingsV1:
    games: tuple[RunLineRankingGameV1, ...]
    policy_version: str = RUN_LINE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = RUN_LINE_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [game.ordinal for game in games] != list(range(1, len(games) + 1)):
            raise RunLineRankingsError("run-line ranking game ordinals must be contiguous")
        if len({game.source_game_id for game in games}) != len(games):
            raise RunLineRankingsError("run-line rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise RunLineRankingsError("reference run-line rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.policy_version != RUN_LINE_REFERENCE_RANKING_POLICY_VERSION:
            raise RunLineRankingsError("unsupported run-line reference ranking policy")
        if self.contract_version != RUN_LINE_RANKINGS_CONTRACT_VERSION:
            raise RunLineRankingsError("unsupported run-line rankings contract")

    @property
    def recommendation_count(self) -> int:
        return sum(entry.rank_eligible for game in self.games for entry in game.entries)

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "policy_version": self.policy_version,
            "recommendation_count": self.recommendation_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _ranking_entry(outcome: RunLineGateOutcomeV1) -> RunLineRankingEntryV1:
    return RunLineRankingEntryV1(
        source_game_id=outcome.source_game_id,
        outcome_team_id=outcome.outcome_team_id,
        side=outcome.side,
        line_key=outcome.line_key,
        side_spread=outcome.side_spread,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_run_line_reference_rankings(
    gate_games: tuple[RunLineGateGameV1, ...],
) -> RunLineReferenceRankingsV1:
    """Retain every V2C Gate row in slate order without inventing a recommendation rank."""

    games = tuple(
        RunLineRankingGameV1(
            ordinal=ordinal,
            source_game_id=gate_game.source_game_id,
            upstream_gate_game_checksum=gate_game.checksum,
            entries=tuple(_ranking_entry(outcome) for outcome in gate_game.outcomes),
            warnings=gate_game.warnings,
        )
        for ordinal, gate_game in enumerate(gate_games, 1)
    )
    return RunLineReferenceRankingsV1(games=games)
