from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.first_five import (
    FirstFiveGateGameV1,
    FirstFiveGateOutcomeV1,
)

FIRST_FIVE_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_RANKING_ENTRY_V1"
FIRST_FIVE_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_RANKING_GAME_V1"
FIRST_FIVE_RANKINGS_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_RANKINGS_V1"
FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION = (
    "DSE_MLB_FIRST_FIVE_REFERENCE_RANKING_V1"
)


class FirstFiveRankingsError(ValueError):
    """Raised when V4C First Five ranking evidence violates reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstFiveRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstFiveRankingsError(f"{name} must be lowercase SHA-256")
    return text


def _optional_number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstFiveRankingsError(f"{name} must be finite numeric when present")
    result = float(value)
    if not math.isfinite(result):
        raise FirstFiveRankingsError(f"{name} must be finite numeric when present")
    return result


@dataclass(frozen=True, slots=True)
class FirstFiveRankingEntryV1:
    source_game_id: str
    provider_event_id: str
    market_family: str
    market_key: str
    side: str
    line_key: str
    market_line: float | None
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = FIRST_FIVE_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "market_family",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "market_line",
            _optional_number(self.market_line, "market_line"),
        )
        if self.decision != "pass":
            raise FirstFiveRankingsError(
                "V4C reference rankings may only consume PASS evidence"
            )
        if self.rank_eligible or self.recommendation_rank is not None:
            raise FirstFiveRankingsError(
                "reference First Five evidence cannot receive a recommendation rank"
            )
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(
                self.upstream_gate_outcome_checksum,
                "upstream_gate_outcome_checksum",
            ),
        )
        reasons = tuple(
            sorted({_text(item, "exclusion_reason") for item in self.exclusion_reasons})
        )
        if not reasons:
            raise FirstFiveRankingsError(
                "unranked First Five reference evidence requires exclusion reasons"
            )
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION:
            raise FirstFiveRankingsError("unsupported First Five reference ranking policy")
        if self.contract_version != FIRST_FIVE_RANKING_ENTRY_CONTRACT_VERSION:
            raise FirstFiveRankingsError("unsupported First Five ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line_key": self.line_key,
            "market_family": self.market_family,
            "market_key": self.market_key,
            "market_line": self.market_line,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class FirstFiveRankingGameV1:
    ordinal: int
    source_game_id: str
    provider_event_id: str
    upstream_gate_game_checksum: str
    entries: tuple[FirstFiveRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = FIRST_FIVE_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise FirstFiveRankingsError("First Five ranking ordinal must be positive")
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "upstream_gate_game_checksum",
            _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"),
        )
        entries = tuple(self.entries)
        if len({item.checksum for item in entries}) != len(entries):
            raise FirstFiveRankingsError("First Five ranking game contains duplicates")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            for item in entries
        ):
            raise FirstFiveRankingsError("First Five ranking entry lineage mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if self.policy_version != FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION:
            raise FirstFiveRankingsError("unsupported First Five reference ranking policy")
        if self.contract_version != FIRST_FIVE_RANKING_GAME_CONTRACT_VERSION:
            raise FirstFiveRankingsError("unsupported First Five ranking game contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entries": [item.as_dict() for item in self.entries],
            "ordinal": self.ordinal,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
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
class FirstFiveReferenceRankingsV1:
    games: tuple[FirstFiveRankingGameV1, ...]
    policy_version: str = FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = FIRST_FIVE_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [item.ordinal for item in games] != list(range(1, len(games) + 1)):
            raise FirstFiveRankingsError("First Five ranking ordinals must be contiguous")
        identities = {(item.source_game_id, item.provider_event_id) for item in games}
        if len(identities) != len(games):
            raise FirstFiveRankingsError("First Five rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise FirstFiveRankingsError(
                "reference First Five rankings cannot contain rank-eligible entries"
            )
        object.__setattr__(self, "games", games)
        if self.policy_version != FIRST_FIVE_REFERENCE_RANKING_POLICY_VERSION:
            raise FirstFiveRankingsError("unsupported First Five reference ranking policy")
        if self.contract_version != FIRST_FIVE_RANKINGS_CONTRACT_VERSION:
            raise FirstFiveRankingsError("unsupported First Five rankings contract")

    @property
    def recommendation_count(self) -> int:
        return sum(entry.rank_eligible for game in self.games for entry in game.entries)

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "games": [item.as_dict() for item in self.games],
            "policy_version": self.policy_version,
            "recommendation_count": self.recommendation_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _entry(outcome: FirstFiveGateOutcomeV1) -> FirstFiveRankingEntryV1:
    return FirstFiveRankingEntryV1(
        source_game_id=outcome.source_game_id,
        provider_event_id=outcome.provider_event_id,
        market_family=outcome.market_family,
        market_key=outcome.market_key,
        side=outcome.side,
        line_key=outcome.line_key,
        market_line=outcome.market_line,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_first_five_reference_rankings(
    gate_games: tuple[FirstFiveGateGameV1, ...],
) -> FirstFiveReferenceRankingsV1:
    """Retain every V4C Gate row in input order without inventing a rank."""

    games = tuple(
        FirstFiveRankingGameV1(
            ordinal=ordinal,
            source_game_id=game.source_game_id,
            provider_event_id=game.provider_event_id,
            upstream_gate_game_checksum=game.checksum,
            entries=tuple(_entry(item) for item in game.outcomes),
            warnings=game.warnings,
        )
        for ordinal, game in enumerate(gate_games, 1)
    )
    return FirstFiveReferenceRankingsV1(games=games)
