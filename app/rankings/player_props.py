from __future__ import annotations

import math
from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.player_props import (
    PlayerPropGateOutcomeV1,
    PlayerPropsGateGameV1,
)

PLAYER_PROP_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_RANKING_ENTRY_V1"
PLAYER_PROP_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_RANKING_GAME_V1"
PLAYER_PROP_RANKINGS_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_RANKINGS_V1"
PLAYER_PROP_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_PLAYER_PROP_REFERENCE_RANKING_V1"


class PlayerPropsRankingsError(ValueError):
    """Raised when V8C player-prop Rankings violate reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlayerPropsRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PlayerPropsRankingsError(f"{name} must be lowercase SHA-256")
    return text


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlayerPropsRankingsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlayerPropsRankingsError(f"{name} must be finite numeric")
    return result


@dataclass(frozen=True, slots=True)
class PlayerPropRankingEntryV1:
    source_game_id: str
    provider_event_id: str
    player_id: str
    player_name: str
    provider_player_key: str
    team_id: str
    opponent_team_id: str
    role: str
    statistic: str
    market_key: str
    side: str
    line_key: str
    line: float
    decision: str
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = PLAYER_PROP_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = PLAYER_PROP_RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "source_game_id",
            "provider_event_id",
            "player_id",
            "player_name",
            "provider_player_key",
            "team_id",
            "opponent_team_id",
            "role",
            "statistic",
            "market_key",
            "side",
            "line_key",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.team_id == self.opponent_team_id:
            raise PlayerPropsRankingsError("player-prop ranking team and opponent must differ")
        if self.side not in {"over", "under"}:
            raise PlayerPropsRankingsError("player-prop ranking side must be over or under")
        line = _number(self.line, "line")
        if line < 0.0:
            raise PlayerPropsRankingsError("player-prop ranking line must be nonnegative")
        object.__setattr__(self, "line", line)
        if self.decision != "pass":
            raise PlayerPropsRankingsError("V8C Rankings may only consume PASS evidence")
        if self.rank_eligible or self.recommendation_rank is not None:
            raise PlayerPropsRankingsError("reference player-prop evidence cannot receive a rank")
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"),
        )
        reasons = tuple(sorted({_text(item, "exclusion_reason") for item in self.exclusion_reasons}))
        if not reasons:
            raise PlayerPropsRankingsError("unranked player-prop evidence requires exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != PLAYER_PROP_REFERENCE_RANKING_POLICY_VERSION:
            raise PlayerPropsRankingsError("unsupported V8C player-prop ranking policy")
        if self.contract_version != PLAYER_PROP_RANKING_ENTRY_CONTRACT_VERSION:
            raise PlayerPropsRankingsError("unsupported V8C player-prop ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line": self.line,
            "line_key": self.line_key,
            "market_key": self.market_key,
            "opponent_team_id": self.opponent_team_id,
            "player_id": self.player_id,
            "player_name": self.player_name,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "provider_player_key": self.provider_player_key,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "role": self.role,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "statistic": self.statistic,
            "team_id": self.team_id,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PlayerPropsRankingGameV1:
    ordinal: int
    source_game_id: str
    provider_event_id: str
    upstream_gate_game_checksum: str
    entries: tuple[PlayerPropRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = PLAYER_PROP_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise PlayerPropsRankingsError("player-prop ranking ordinal must be positive")
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        object.__setattr__(
            self,
            "upstream_gate_game_checksum",
            _sha(self.upstream_gate_game_checksum, "upstream_gate_game_checksum"),
        )
        entries = tuple(self.entries)
        identities = {
            (
                item.player_id,
                item.statistic,
                item.market_key,
                item.side,
                item.line_key,
                item.line,
            )
            for item in entries
        }
        if len(identities) != len(entries):
            raise PlayerPropsRankingsError("player-prop ranking game contains duplicate entries")
        if any(
            item.source_game_id != self.source_game_id
            or item.provider_event_id != self.provider_event_id
            for item in entries
        ):
            raise PlayerPropsRankingsError("player-prop ranking game lineage mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "warnings", tuple(sorted({_text(item, "warning") for item in self.warnings})))
        if self.contract_version != PLAYER_PROP_RANKING_GAME_CONTRACT_VERSION:
            raise PlayerPropsRankingsError("unsupported V8C player-prop ranking game contract")

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
class PlayerPropsReferenceRankingsV1:
    games: tuple[PlayerPropsRankingGameV1, ...]
    policy_version: str = PLAYER_PROP_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = PLAYER_PROP_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [item.ordinal for item in games] != list(range(1, len(games) + 1)):
            raise PlayerPropsRankingsError("player-prop ranking ordinals must be contiguous")
        if len({(item.source_game_id, item.provider_event_id) for item in games}) != len(games):
            raise PlayerPropsRankingsError("player-prop rankings contain duplicate games")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise PlayerPropsRankingsError("reference player-prop rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.policy_version != PLAYER_PROP_REFERENCE_RANKING_POLICY_VERSION:
            raise PlayerPropsRankingsError("unsupported V8C player-prop ranking policy")
        if self.contract_version != PLAYER_PROP_RANKINGS_CONTRACT_VERSION:
            raise PlayerPropsRankingsError("unsupported V8C player-prop rankings contract")

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


def _entry(outcome: PlayerPropGateOutcomeV1) -> PlayerPropRankingEntryV1:
    return PlayerPropRankingEntryV1(
        source_game_id=outcome.source_game_id,
        provider_event_id=outcome.provider_event_id,
        player_id=outcome.player_id,
        player_name=outcome.player_name,
        provider_player_key=outcome.provider_player_key,
        team_id=outcome.team_id,
        opponent_team_id=outcome.opponent_team_id,
        role=outcome.role,
        statistic=outcome.statistic,
        market_key=outcome.market_key,
        side=outcome.side,
        line_key=outcome.line_key,
        line=outcome.line,
        decision=outcome.decision,
        rank_eligible=False,
        recommendation_rank=None,
        upstream_gate_outcome_checksum=outcome.checksum,
        exclusion_reasons=outcome.reason_codes,
    )


def build_player_props_reference_rankings(
    gate_games: tuple[PlayerPropsGateGameV1, ...],
) -> PlayerPropsReferenceRankingsV1:
    games = tuple(
        PlayerPropsRankingGameV1(
            ordinal=ordinal,
            source_game_id=game.source_game_id,
            provider_event_id=game.provider_event_id,
            upstream_gate_game_checksum=game.checksum,
            entries=tuple(_entry(item) for item in game.outcomes),
            warnings=game.warnings,
        )
        for ordinal, game in enumerate(gate_games, 1)
    )
    return PlayerPropsReferenceRankingsV1(games=games)
