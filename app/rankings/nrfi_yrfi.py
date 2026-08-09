from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.nrfi_yrfi import NrfiYrfiGateGameV1, NrfiYrfiGateOutcomeV1

NRFI_YRFI_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_RANKING_ENTRY_V1"
NRFI_YRFI_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_RANKING_GAME_V1"
NRFI_YRFI_RANKINGS_CONTRACT_VERSION = "DSE_MLB_NRFI_YRFI_RANKINGS_V1"
NRFI_YRFI_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_NRFI_YRFI_REFERENCE_RANKING_V1"


class NrfiYrfiRankingsError(ValueError):
    """Raised when V5C NRFI/YRFI rankings violate reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise NrfiYrfiRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise NrfiYrfiRankingsError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class NrfiYrfiRankingEntryV1:
    source_game_id: str
    provider_event_id: str
    side: str
    total_line: float
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = NRFI_YRFI_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = NRFI_YRFI_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        if self.side not in {"nrfi", "yrfi"}:
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking side must be nrfi or yrfi")
        if self.total_line != 0.5:
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking requires the 0.5 line")
        if self.decision != "pass":
            raise NrfiYrfiRankingsError("V5C rankings may only consume PASS evidence")
        if self.rank_eligible or self.recommendation_rank is not None:
            raise NrfiYrfiRankingsError("reference NRFI/YRFI evidence cannot receive a rank")
        object.__setattr__(self, "upstream_gate_outcome_checksum", _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"))
        reasons = tuple(sorted({_text(item, "exclusion_reason") for item in self.exclusion_reasons}))
        if not reasons:
            raise NrfiYrfiRankingsError("unranked NRFI/YRFI evidence requires exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.contract_version != NRFI_YRFI_RANKING_ENTRY_CONTRACT_VERSION:
            raise NrfiYrfiRankingsError("unsupported NRFI/YRFI ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
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
class NrfiYrfiRankingGameV1:
    ordinal: int
    source_game_id: str
    provider_event_id: str
    upstream_gate_game_checksum: str
    entries: tuple[NrfiYrfiRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = NRFI_YRFI_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking ordinal must be positive")
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(self, "upstream_gate_game_checksum", _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"))
        entries = tuple(self.entries)
        if len({item.side for item in entries}) != len(entries):
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking game contains duplicate sides")
        if any(item.source_game_id != self.source_game_id or item.provider_event_id != self.provider_event_id for item in entries):
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking lineage mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if self.contract_version != NRFI_YRFI_RANKING_GAME_CONTRACT_VERSION:
            raise NrfiYrfiRankingsError("unsupported NRFI/YRFI ranking game contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entries": [item.as_dict() for item in self.entries],
            "ordinal": self.ordinal,
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
class NrfiYrfiReferenceRankingsV1:
    games: tuple[NrfiYrfiRankingGameV1, ...]
    policy_version: str = NRFI_YRFI_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = NRFI_YRFI_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [item.ordinal for item in games] != list(range(1, len(games) + 1)):
            raise NrfiYrfiRankingsError("NRFI/YRFI ranking ordinals must be contiguous")
        if len({(item.source_game_id, item.provider_event_id) for item in games}) != len(games):
            raise NrfiYrfiRankingsError("NRFI/YRFI rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise NrfiYrfiRankingsError("reference NRFI/YRFI rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.contract_version != NRFI_YRFI_RANKINGS_CONTRACT_VERSION:
            raise NrfiYrfiRankingsError("unsupported NRFI/YRFI rankings contract")

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


def _entry(outcome: NrfiYrfiGateOutcomeV1) -> NrfiYrfiRankingEntryV1:
    return NrfiYrfiRankingEntryV1(
        source_game_id=outcome.source_game_id,
        provider_event_id=outcome.provider_event_id,
        side=outcome.side,
        total_line=outcome.total_line,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_nrfi_yrfi_reference_rankings(
    gate_games: tuple[NrfiYrfiGateGameV1, ...],
) -> NrfiYrfiReferenceRankingsV1:
    games = tuple(
        NrfiYrfiRankingGameV1(
            ordinal=ordinal,
            source_game_id=game.source_game_id,
            provider_event_id=game.provider_event_id,
            upstream_gate_game_checksum=game.checksum,
            entries=tuple(_entry(item) for item in game.outcomes),
            warnings=game.warnings,
        )
        for ordinal, game in enumerate(gate_games, 1)
    )
    return NrfiYrfiReferenceRankingsV1(games=games)
