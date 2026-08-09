from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.game_total import GameTotalGateGameV1, GameTotalGateOutcomeV1

GAME_TOTAL_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_RANKING_ENTRY_V1"
GAME_TOTAL_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_RANKING_GAME_V1"
GAME_TOTAL_RANKINGS_CONTRACT_VERSION = "DSE_MLB_GAME_TOTAL_RANKINGS_V1"
GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_GAME_TOTAL_REFERENCE_RANKING_V1"


class GameTotalRankingsError(ValueError):
    """Raised when V3C game-total ranking evidence violates its reference-safe contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GameTotalRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise GameTotalRankingsError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GameTotalRankingsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise GameTotalRankingsError(f"{name} must be finite numeric")
    return result


@dataclass(frozen=True, slots=True)
class GameTotalRankingEntryV1:
    source_game_id: str
    side: str
    line_key: str
    total_line: float
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = GAME_TOTAL_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        if self.side not in {"over", "under"}:
            raise GameTotalRankingsError("game-total ranking side must be over or under")
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(self, "total_line", _number(self.total_line, "total_line"))
        if self.decision != "pass":
            raise GameTotalRankingsError(
                "V3C reference rankings may only consume PASS evidence; production ranking requires a version bump"
            )
        if self.rank_eligible or self.recommendation_rank is not None:
            raise GameTotalRankingsError("reference game-total evidence cannot receive a recommendation rank")
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"),
        )
        reasons = tuple(sorted({_text(reason, "exclusion_reason") for reason in self.exclusion_reasons}))
        if not reasons:
            raise GameTotalRankingsError("unranked reference evidence requires explicit exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise GameTotalRankingsError("unsupported game-total reference ranking policy")
        if self.contract_version != GAME_TOTAL_RANKING_ENTRY_CONTRACT_VERSION:
            raise GameTotalRankingsError("unsupported game-total ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line_key": self.line_key,
            "policy_version": self.policy_version,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "total_line": self.total_line,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GameTotalRankingGameV1:
    ordinal: int
    source_game_id: str
    upstream_gate_game_checksum: str
    entries: tuple[GameTotalRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = GAME_TOTAL_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise GameTotalRankingsError("game-total ranking game ordinal must be a positive integer")
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(
            self,
            "upstream_gate_game_checksum",
            _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"),
        )
        entries = tuple(self.entries)
        if len({entry.checksum for entry in entries}) != len(entries):
            raise GameTotalRankingsError("game-total ranking game contains duplicate entries")
        if any(entry.source_game_id != self.source_game_id for entry in entries):
            raise GameTotalRankingsError("game-total ranking entry game identity mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(warning, "warning") for warning in self.warnings})),
        )
        if self.policy_version != GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise GameTotalRankingsError("unsupported game-total reference ranking policy")
        if self.contract_version != GAME_TOTAL_RANKING_GAME_CONTRACT_VERSION:
            raise GameTotalRankingsError("unsupported game-total ranking game contract")

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
class GameTotalReferenceRankingsV1:
    games: tuple[GameTotalRankingGameV1, ...]
    policy_version: str = GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = GAME_TOTAL_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [game.ordinal for game in games] != list(range(1, len(games) + 1)):
            raise GameTotalRankingsError("game-total ranking game ordinals must be contiguous")
        if len({game.source_game_id for game in games}) != len(games):
            raise GameTotalRankingsError("game-total rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise GameTotalRankingsError("reference game-total rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.policy_version != GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise GameTotalRankingsError("unsupported game-total reference ranking policy")
        if self.contract_version != GAME_TOTAL_RANKINGS_CONTRACT_VERSION:
            raise GameTotalRankingsError("unsupported game-total rankings contract")

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


def _ranking_entry(outcome: GameTotalGateOutcomeV1) -> GameTotalRankingEntryV1:
    return GameTotalRankingEntryV1(
        source_game_id=outcome.source_game_id,
        side=outcome.side,
        line_key=outcome.line_key,
        total_line=outcome.total_line,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_game_total_reference_rankings(
    gate_games: tuple[GameTotalGateGameV1, ...],
) -> GameTotalReferenceRankingsV1:
    """Retain every V3C Gate row in slate order without inventing a recommendation rank."""

    games = tuple(
        GameTotalRankingGameV1(
            ordinal=ordinal,
            source_game_id=gate_game.source_game_id,
            upstream_gate_game_checksum=gate_game.checksum,
            entries=tuple(_ranking_entry(outcome) for outcome in gate_game.outcomes),
            warnings=gate_game.warnings,
        )
        for ordinal, gate_game in enumerate(gate_games, 1)
    )
    return GameTotalReferenceRankingsV1(games=games)
