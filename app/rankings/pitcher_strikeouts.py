from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256
from app.recommendation_gate.pitcher_strikeouts import (
    PitcherStrikeoutGateGameV1,
    PitcherStrikeoutGateOutcomeV1,
)

PITCHER_STRIKEOUT_RANKING_ENTRY_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_RANKING_ENTRY_V1"
PITCHER_STRIKEOUT_RANKING_GAME_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_RANKING_GAME_V1"
PITCHER_STRIKEOUT_RANKINGS_CONTRACT_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_RANKINGS_V1"
PITCHER_STRIKEOUT_REFERENCE_RANKING_POLICY_VERSION = "DSE_MLB_PITCHER_STRIKEOUT_REFERENCE_RANKING_V1"


class PitcherStrikeoutRankingsError(ValueError):
    """Raised when V7C pitcher-strikeout Rankings violate reference-safe rules."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PitcherStrikeoutRankingsError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PitcherStrikeoutRankingsError(f"{name} must be lowercase SHA-256")
    return text


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutRankingEntryV1:
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
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_outcome_checksum: str
    exclusion_reasons: tuple[str, ...]
    policy_version: str = PITCHER_STRIKEOUT_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = PITCHER_STRIKEOUT_RANKING_ENTRY_CONTRACT_VERSION

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
            raise PitcherStrikeoutRankingsError("V7 ranking team and opponent must differ")
        if self.starter_binding_state not in {"expected", "confirmed"}:
            raise PitcherStrikeoutRankingsError("V7 ranking starter state must be expected or confirmed")
        object.__setattr__(
            self,
            "starter_binding_checksum",
            _sha(self.starter_binding_checksum, "starter_binding_checksum"),
        )
        if self.side not in {"over", "under"}:
            raise PitcherStrikeoutRankingsError("V7 ranking side must be over or under")
        if self.line < 0.0:
            raise PitcherStrikeoutRankingsError("V7 ranking line must be nonnegative")
        if self.decision != "pass":
            raise PitcherStrikeoutRankingsError("V7C rankings may only consume PASS evidence")
        if self.rank_eligible or self.recommendation_rank is not None:
            raise PitcherStrikeoutRankingsError("reference V7 evidence cannot receive a rank")
        object.__setattr__(
            self,
            "upstream_gate_outcome_checksum",
            _sha(self.upstream_gate_outcome_checksum, "upstream_gate_outcome_checksum"),
        )
        reasons = tuple(sorted({_text(item, "exclusion_reason") for item in self.exclusion_reasons}))
        if not reasons:
            raise PitcherStrikeoutRankingsError("unranked V7 evidence requires exclusion reasons")
        object.__setattr__(self, "exclusion_reasons", reasons)
        if self.policy_version != PITCHER_STRIKEOUT_REFERENCE_RANKING_POLICY_VERSION:
            raise PitcherStrikeoutRankingsError("unsupported V7 reference ranking policy")
        if self.contract_version != PITCHER_STRIKEOUT_RANKING_ENTRY_CONTRACT_VERSION:
            raise PitcherStrikeoutRankingsError("unsupported V7 ranking entry contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision,
            "exclusion_reasons": list(self.exclusion_reasons),
            "line": self.line,
            "line_key": self.line_key,
            "market_key": self.market_key,
            "opponent_team_id": self.opponent_team_id,
            "pitcher_id": self.pitcher_id,
            "pitcher_name": self.pitcher_name,
            "policy_version": self.policy_version,
            "provider_event_id": self.provider_event_id,
            "provider_pitcher_key": self.provider_pitcher_key,
            "rank_eligible": self.rank_eligible,
            "recommendation_rank": self.recommendation_rank,
            "side": self.side,
            "source_game_id": self.source_game_id,
            "starter_binding_checksum": self.starter_binding_checksum,
            "starter_binding_state": self.starter_binding_state,
            "team_id": self.team_id,
            "upstream_gate_outcome_checksum": self.upstream_gate_outcome_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutRankingGameV1:
    ordinal: int
    source_game_id: str
    provider_event_id: str
    upstream_gate_game_checksum: str
    source_prediction_checksum: str
    source_normalized_odds_checksum: str
    entries: tuple[PitcherStrikeoutRankingEntryV1, ...]
    warnings: tuple[str, ...] = ()
    contract_version: str = PITCHER_STRIKEOUT_RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise PitcherStrikeoutRankingsError("V7 ranking ordinal must be positive")
        object.__setattr__(self, "source_game_id", _text(self.source_game_id, "source_game_id"))
        object.__setattr__(self, "provider_event_id", _text(self.provider_event_id, "provider_event_id"))
        for name in (
            "upstream_gate_game_checksum",
            "source_prediction_checksum",
            "source_normalized_odds_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        entries = tuple(self.entries)
        identities = {
            (entry.pitcher_id, entry.market_key, entry.side, entry.line_key, entry.line)
            for entry in entries
        }
        if len(identities) != len(entries):
            raise PitcherStrikeoutRankingsError("V7 ranking game contains duplicate entries")
        if any(
            entry.source_game_id != self.source_game_id
            or entry.provider_event_id != self.provider_event_id
            for entry in entries
        ):
            raise PitcherStrikeoutRankingsError("V7 ranking entry lineage mismatch")
        object.__setattr__(self, "entries", entries)
        object.__setattr__(
            self,
            "warnings",
            tuple(sorted({_text(item, "warning") for item in self.warnings})),
        )
        if self.contract_version != PITCHER_STRIKEOUT_RANKING_GAME_CONTRACT_VERSION:
            raise PitcherStrikeoutRankingsError("unsupported V7 ranking game contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "entries": [item.as_dict() for item in self.entries],
            "ordinal": self.ordinal,
            "provider_event_id": self.provider_event_id,
            "source_game_id": self.source_game_id,
            "source_normalized_odds_checksum": self.source_normalized_odds_checksum,
            "source_prediction_checksum": self.source_prediction_checksum,
            "upstream_gate_game_checksum": self.upstream_gate_game_checksum,
            "warnings": list(self.warnings),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PitcherStrikeoutReferenceRankingsV1:
    games: tuple[PitcherStrikeoutRankingGameV1, ...]
    policy_version: str = PITCHER_STRIKEOUT_REFERENCE_RANKING_POLICY_VERSION
    contract_version: str = PITCHER_STRIKEOUT_RANKINGS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        games = tuple(self.games)
        if [item.ordinal for item in games] != list(range(1, len(games) + 1)):
            raise PitcherStrikeoutRankingsError("V7 ranking ordinals must be contiguous")
        gate_checksums = [item.upstream_gate_game_checksum for item in games]
        if len(gate_checksums) != len(set(gate_checksums)):
            raise PitcherStrikeoutRankingsError("V7 rankings contain duplicate Gate evidence")
        if any(entry.rank_eligible for game in games for entry in game.entries):
            raise PitcherStrikeoutRankingsError("reference V7 rankings cannot contain rank-eligible entries")
        object.__setattr__(self, "games", games)
        if self.policy_version != PITCHER_STRIKEOUT_REFERENCE_RANKING_POLICY_VERSION:
            raise PitcherStrikeoutRankingsError("unsupported V7 reference ranking policy")
        if self.contract_version != PITCHER_STRIKEOUT_RANKINGS_CONTRACT_VERSION:
            raise PitcherStrikeoutRankingsError("unsupported V7 rankings contract")

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


def _entry(outcome: PitcherStrikeoutGateOutcomeV1) -> PitcherStrikeoutRankingEntryV1:
    return PitcherStrikeoutRankingEntryV1(
        source_game_id=outcome.source_game_id,
        provider_event_id=outcome.provider_event_id,
        pitcher_id=outcome.pitcher_id,
        pitcher_name=outcome.pitcher_name,
        provider_pitcher_key=outcome.provider_pitcher_key,
        team_id=outcome.team_id,
        opponent_team_id=outcome.opponent_team_id,
        starter_binding_state=outcome.starter_binding_state,
        starter_binding_checksum=outcome.starter_binding_checksum,
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


def build_pitcher_strikeout_reference_rankings(
    gate_games: tuple[PitcherStrikeoutGateGameV1, ...],
) -> PitcherStrikeoutReferenceRankingsV1:
    games = tuple(
        PitcherStrikeoutRankingGameV1(
            ordinal=ordinal,
            source_game_id=game.source_game_id,
            provider_event_id=game.provider_event_id,
            upstream_gate_game_checksum=game.checksum,
            source_prediction_checksum=game.source_prediction_checksum,
            source_normalized_odds_checksum=game.source_normalized_odds_checksum,
            entries=tuple(_entry(item) for item in game.outcomes),
            warnings=game.warnings,
        )
        for ordinal, game in enumerate(gate_games, 1)
    )
    return PitcherStrikeoutReferenceRankingsV1(games=games)
