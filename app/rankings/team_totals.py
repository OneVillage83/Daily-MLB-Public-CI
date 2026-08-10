from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.team_totals import (
    TeamTotalGateOutcomeV1,
    TeamTotalsGateGameV1,
)

TEAM_TOTAL_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_RANKING_ENTRY_V1"
TEAM_TOTAL_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_RANKING_GAME_V1"
TEAM_TOTAL_RANKINGS_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_RANKINGS_V1"
TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_TEAM_TOTAL_REFERENCE_RANKING_V1"


class TeamTotalsRankingsError(ValueError):
    """Raised when V6C team-total rankings violate reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TeamTotalsRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TeamTotalsRankingsError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class TeamTotalRankingEntryV1:
    source_game_id: str
    provider_event_id: str
    team_id: str
    opponent_team_id: str
    side: str
    line_key: str
    total_line: float
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = TEAM_TOTAL_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "team_id",
            "opponent_team_id",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise TeamTotalsRankingsError("team-total ranking subject and opponent must differ")
        if self.side not in {"over", "under"}:
            raise TeamTotalsRankingsError("team-total ranking side must be over or under")
        if self.total_line < 0.0:
            raise TeamTotalsRankingsError("team-total ranking line must be nonnegative")
        if self.decision != "pass":
            raise TeamTotalsRankingsError("V6C rankings may only consume PASS evidence")
        if self.rank_eligible or self.recommendation_rank is not None:
            raise TeamTotalsRankingsError("reference team-total evidence cannot receive a rank")
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"),
        )
        reasons = tuple(
            sorted({_text(item, "exclusion_reason") for item in self.exclusion_reasons})
        )
        if not reasons:
            raise TeamTotalsRankingsError("unranked team-total evidence requires exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total reference ranking policy")
        if self.contract_version != TEAM_TOTAL_RANKING_ENTRY_CONTRACT_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line_key": self.line_key,
            "opponent_team_id": self.opponent_team_id,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "team_id": self.team_id,
            "total_line": self.total_line,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class TeamTotalRankingGameV1:
    ordinal: int
    source_game_id: str
    provider_event_id: str
    away_team_id: str
    home_team_id: str
    upstream_gate_game_checksum: str
    entries: tuple[TeamTotalRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    policy_version: str = TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = TEAM_TOTAL_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise TeamTotalsRankingsError("team-total ranking ordinal must be positive")
        for name in ("source_game_id", "provider_event_id", "away_team_id", "home_team_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.away_team_id == self.home_team_id:
            raise TeamTotalsRankingsError("team-total ranking game teams must differ")
        object.__setattr__(
            self,
            "upstream_gate_game_checksum",
            _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"),
        )
        entries = tuple(self.entries)
        identities = {(item.team_id, item.side, item.line_key, item.total_line) for item in entries}
        if len(identities) != len(entries):
            raise TeamTotalsRankingsError("team-total ranking game contains duplicate entries")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            or item.team_id not in {self.away_team_id, self.home_team_id}
            for item in entries
        ):
            raise TeamTotalsRankingsError("team-total ranking lineage mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if self.policy_version != TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total reference ranking policy")
        if self.contract_version != TEAM_TOTAL_RANKING_GAME_CONTRACT_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total ranking game contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "entries": [item.as_dict() for item in self.entries],
            "home_team_id": self.home_team_id,
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
class TeamTotalsReferenceRankingsV1:
    games: tuple[TeamTotalRankingGameV1, ...]
    policy_version: str = TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = TEAM_TOTAL_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [item.ordinal for item in games] != list(range(1, len(games) + 1)):
            raise TeamTotalsRankingsError("team-total ranking ordinals must be contiguous")
        if len({(item.source_game_id, item.provider_event_id) for item in games}) != len(games):
            raise TeamTotalsRankingsError("team-total rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise TeamTotalsRankingsError("reference team-total rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.policy_version != TEAM_TOTAL_REFERENCE_RANKING_POLICY_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total reference ranking policy")
        if self.contract_version != TEAM_TOTAL_RANKINGS_CONTRACT_VERSION:
            raise TeamTotalsRankingsError("unsupported team-total rankings contract")

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


def _entry(outcome: TeamTotalGateOutcomeV1) -> TeamTotalRankingEntryV1:
    return TeamTotalRankingEntryV1(
        source_game_id=outcome.source_game_id,
        provider_event_id=outcome.provider_event_id,
        team_id=outcome.team_id,
        opponent_team_id=outcome.opponent_team_id,
        side=outcome.side,
        line_key=outcome.line_key,
        total_line=outcome.total_line,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_team_totals_reference_rankings(
    gate_games: tuple[TeamTotalsGateGameV1, ...],
) -> TeamTotalsReferenceRankingsV1:
    games = tuple(
        TeamTotalRankingGameV1(
            ordinal=ordinal,
            source_game_id=game.source_game_id,
            provider_event_id=game.provider_event_id,
            away_team_id=game.away_team_id,
            home_team_id=game.home_team_id,
            upstream_gate_game_checksum=game.checksum,
            entries=tuple(_entry(item) for item in game.outcomes),
            warnings=game.warnings,
        )
        for ordinal, game in enumerate(gate_games, 1)
    )
    return TeamTotalsReferenceRankingsV1(games=games)
