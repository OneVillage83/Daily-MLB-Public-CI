from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.daily_slate.contracts import (
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.rankings.policy import RankingsPolicyV1
from app.recommendation_gate.contracts import (
    EvidenceConfidenceBand,
    OperationalRiskLevel,
    RecommendationDecision,
    RecommendationReason,
)
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS
from app.value_engine.contracts import ValueMarket, ValueSide

RANKINGS_CONTRACT_VERSION = "DSE_RANKINGS_V1"
RANKING_GAME_CONTRACT_VERSION = "DSE_RANKING_GAME_V1"
RANKING_ENTRY_CONTRACT_VERSION = "DSE_RANKING_ENTRY_V1"
RANKING_BOARD_CONTRACT_VERSION = "DSE_RANKING_BOARD_V1"
RANKING_PLACEMENT_CONTRACT_VERSION = "DSE_RANKING_PLACEMENT_V1"


class RankingsContractError(ValueError):
    """Raised when canonical Rankings V1 evidence is invalid."""


class RankingBoardType(StrEnum):
    TOP_CONFIDENCE = "top_confidence"
    BEST_VALUE = "best_value"


class RankingExclusionReason(StrEnum):
    RECOMMENDATION_NOT_ACTIONABLE = "recommendation_not_actionable"


class RankingGameWarning(StrEnum):
    NO_RECOMMENDATION_ROWS = "no_recommendation_rows"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RankingsContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise RankingsContractError(f"{name} must be lowercase SHA-256")
    return text


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise RankingsContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RankingsContractError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RankingsContractError(f"{name} must be finite numeric")
    return result


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _probability(value: object, name: str) -> float:
    result = _number(value, name)
    if not 0.0 <= result <= 1.0:
        raise RankingsContractError(f"{name} must be in [0,1]")
    return result


def _optional_probability(value: object, name: str) -> float | None:
    return None if value is None else _probability(value, name)


def _score(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise RankingsContractError(f"{name} must be an integer in [0,100]")
    return value


def _rank(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RankingsContractError(f"{name} must be a positive integer")
    return value


def _optional_rank(value: object, name: str) -> int | None:
    return None if value is None else _rank(value, name)


@dataclass(frozen=True, slots=True)
class RankingEntryV1:
    upstream_recommendation_checksum: str
    upstream_recommendation_game_checksum: str
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    market: ValueMarket
    line_key: str
    side: ValueSide
    market_line: float | None
    american_price: float
    bookmaker_count: int
    freshness_status: str
    decision: RecommendationDecision
    reasons: tuple[RecommendationReason, ...]
    conditional_model_probability: float
    no_vig_probability: float | None
    no_vig_probability_edge: float | None
    expected_value_per_unit: float
    expected_roi_percent: float
    evidence_confidence_score: int
    evidence_confidence_band: EvidenceConfidenceBand
    operational_risk_score: int
    operational_risk_level: OperationalRiskLevel
    review_required: bool
    publication_candidate: bool
    manual_promotion_permitted: bool
    ranking_eligible: bool
    exclusion_reason: RankingExclusionReason | None
    top_confidence_rank: int | None
    best_value_rank: int | None
    recommendation_evaluated_at: datetime
    policy_checksum: str
    contract_version: str = RANKING_ENTRY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "upstream_recommendation_checksum",
            _sha(self.upstream_recommendation_checksum, "upstream_recommendation_checksum"),
        )
        object.__setattr__(
            self,
            "upstream_recommendation_game_checksum",
            _sha(
                self.upstream_recommendation_game_checksum,
                "upstream_recommendation_game_checksum",
            ),
        )
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise RankingsContractError("edge_event_id mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise RankingsContractError("daily_mlb_game_id mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise RankingsContractError("invalid canonical MLB teams")
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(
            self,
            "market_line",
            _optional_number(self.market_line, "market_line"),
        )
        if self.market is ValueMarket.MONEYLINE and self.market_line is not None:
            raise RankingsContractError("moneyline cannot contain a point")
        if self.market is not ValueMarket.MONEYLINE and self.market_line is None:
            raise RankingsContractError("spread/total requires a point")
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise RankingsContractError("invalid American price")
        object.__setattr__(self, "american_price", price)
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count <= 0
        ):
            raise RankingsContractError("bookmaker_count must be positive")
        object.__setattr__(
            self,
            "freshness_status",
            _text(self.freshness_status, "freshness_status"),
        )
        reasons = tuple(self.reasons)
        if not reasons:
            raise RankingsContractError("recommendation reasons cannot be empty")
        object.__setattr__(self, "reasons", reasons)
        object.__setattr__(
            self,
            "conditional_model_probability",
            _probability(
                self.conditional_model_probability,
                "conditional_model_probability",
            ),
        )
        object.__setattr__(
            self,
            "no_vig_probability",
            _optional_probability(self.no_vig_probability, "no_vig_probability"),
        )
        object.__setattr__(
            self,
            "no_vig_probability_edge",
            _optional_number(
                self.no_vig_probability_edge,
                "no_vig_probability_edge",
            ),
        )
        for name in ("expected_value_per_unit", "expected_roi_percent"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not math.isclose(
            self.expected_roi_percent,
            self.expected_value_per_unit * 100.0,
            abs_tol=1e-10,
        ):
            raise RankingsContractError("ROI disagrees with EV")
        object.__setattr__(
            self,
            "evidence_confidence_score",
            _score(self.evidence_confidence_score, "evidence_confidence_score"),
        )
        object.__setattr__(
            self,
            "operational_risk_score",
            _score(self.operational_risk_score, "operational_risk_score"),
        )
        if self.operational_risk_score != 100 - self.evidence_confidence_score:
            raise RankingsContractError("operational risk score mismatch")
        top_rank = _optional_rank(self.top_confidence_rank, "top_confidence_rank")
        value_rank = _optional_rank(self.best_value_rank, "best_value_rank")
        object.__setattr__(self, "top_confidence_rank", top_rank)
        object.__setattr__(self, "best_value_rank", value_rank)
        actionable = self.decision in {
            RecommendationDecision.BET,
            RecommendationDecision.LEAN,
        }
        if self.ranking_eligible != actionable:
            raise RankingsContractError("ranking eligibility must follow decision")
        if self.review_required != actionable:
            raise RankingsContractError("review_required mismatch")
        if self.publication_candidate != (self.decision is RecommendationDecision.BET):
            raise RankingsContractError("publication_candidate mismatch")
        if self.manual_promotion_permitted:
            raise RankingsContractError("manual promotion remains prohibited")
        if actionable:
            if self.exclusion_reason is not None:
                raise RankingsContractError("eligible entry cannot have exclusion reason")
            if top_rank is None or value_rank is None:
                raise RankingsContractError("eligible entry requires both ranks")
            if self.no_vig_probability is None or self.no_vig_probability_edge is None:
                raise RankingsContractError("eligible entry requires no-vig evidence")
        else:
            if self.exclusion_reason is not RankingExclusionReason.RECOMMENDATION_NOT_ACTIONABLE:
                raise RankingsContractError("ineligible entry requires exclusion reason")
            if top_rank is not None or value_rank is not None:
                raise RankingsContractError("ineligible entry cannot have ranks")
        object.__setattr__(
            self,
            "recommendation_evaluated_at",
            _utc(self.recommendation_evaluated_at, "recommendation_evaluated_at"),
        )
        object.__setattr__(
            self,
            "policy_checksum",
            _sha(self.policy_checksum, "policy_checksum"),
        )
        if self.contract_version != RANKING_ENTRY_CONTRACT_VERSION:
            raise RankingsContractError("unsupported ranking-entry contract")

    @property
    def canonical_identity_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.source_game_id,
            self.market.value,
            self.line_key,
            self.side.value,
            self.upstream_recommendation_checksum,
        )

    def _content_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "away_team_id": self.away_team_id,
            "best_value_rank": self.best_value_rank,
            "bookmaker_count": self.bookmaker_count,
            "conditional_model_probability": self.conditional_model_probability,
            "contract_version": self.contract_version,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "decision": self.decision.value,
            "edge_event_id": self.edge_event_id,
            "evidence_confidence_band": self.evidence_confidence_band.value,
            "evidence_confidence_score": self.evidence_confidence_score,
            "exclusion_reason": (
                None if self.exclusion_reason is None else self.exclusion_reason.value
            ),
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "freshness_status": self.freshness_status,
            "home_team_id": self.home_team_id,
            "line_key": self.line_key,
            "manual_promotion_permitted": self.manual_promotion_permitted,
            "market": self.market.value,
            "market_line": self.market_line,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "operational_risk_level": self.operational_risk_level.value,
            "operational_risk_score": self.operational_risk_score,
            "policy_checksum": self.policy_checksum,
            "publication_candidate": self.publication_candidate,
            "ranking_eligible": self.ranking_eligible,
            "reasons": [reason.value for reason in self.reasons],
            "recommendation_evaluated_at": self.recommendation_evaluated_at.isoformat(),
            "review_required": self.review_required,
            "side": self.side.value,
            "source_game_id": self.source_game_id,
            "top_confidence_rank": self.top_confidence_rank,
            "upstream_recommendation_checksum": self.upstream_recommendation_checksum,
            "upstream_recommendation_game_checksum": (
                self.upstream_recommendation_game_checksum
            ),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RankingPlacementV1:
    board: RankingBoardType
    rank: int
    ranking_entry_checksum: str
    upstream_recommendation_checksum: str
    decision: RecommendationDecision
    primary_metric_name: str
    primary_metric_value: float
    policy_checksum: str
    contract_version: str = RANKING_PLACEMENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "rank", _rank(self.rank, "rank"))
        object.__setattr__(
            self,
            "ranking_entry_checksum",
            _sha(self.ranking_entry_checksum, "ranking_entry_checksum"),
        )
        object.__setattr__(
            self,
            "upstream_recommendation_checksum",
            _sha(self.upstream_recommendation_checksum, "upstream_recommendation_checksum"),
        )
        if self.decision not in {
            RecommendationDecision.BET,
            RecommendationDecision.LEAN,
        }:
            raise RankingsContractError("placement decision must be actionable")
        expected_metric = {
            RankingBoardType.TOP_CONFIDENCE: "conditional_model_probability",
            RankingBoardType.BEST_VALUE: "expected_value_per_unit",
        }[self.board]
        if self.primary_metric_name != expected_metric:
            raise RankingsContractError("placement primary metric mismatch")
        object.__setattr__(
            self,
            "primary_metric_value",
            _number(self.primary_metric_value, "primary_metric_value"),
        )
        if self.board is RankingBoardType.TOP_CONFIDENCE:
            _probability(self.primary_metric_value, "primary_metric_value")
        object.__setattr__(
            self,
            "policy_checksum",
            _sha(self.policy_checksum, "policy_checksum"),
        )
        if self.contract_version != RANKING_PLACEMENT_CONTRACT_VERSION:
            raise RankingsContractError("unsupported ranking-placement contract")

    def _content_dict(self) -> dict[str, object]:
        return {
            "board": self.board.value,
            "contract_version": self.contract_version,
            "decision": self.decision.value,
            "policy_checksum": self.policy_checksum,
            "primary_metric_name": self.primary_metric_name,
            "primary_metric_value": self.primary_metric_value,
            "rank": self.rank,
            "ranking_entry_checksum": self.ranking_entry_checksum,
            "upstream_recommendation_checksum": self.upstream_recommendation_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RankingBoardV1:
    board: RankingBoardType
    placements: tuple[RankingPlacementV1, ...]
    policy_checksum: str
    contract_version: str = RANKING_BOARD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        placements = tuple(self.placements)
        if any(item.board is not self.board for item in placements):
            raise RankingsContractError("board contains placement for another board")
        if [item.rank for item in placements] != list(range(1, len(placements) + 1)):
            raise RankingsContractError("board ranks must be consecutive from 1")
        if len({item.ranking_entry_checksum for item in placements}) != len(placements):
            raise RankingsContractError("board contains duplicate ranking entries")
        object.__setattr__(
            self,
            "policy_checksum",
            _sha(self.policy_checksum, "policy_checksum"),
        )
        if any(item.policy_checksum != self.policy_checksum for item in placements):
            raise RankingsContractError("placement policy checksum mismatch")
        object.__setattr__(self, "placements", placements)
        if self.contract_version != RANKING_BOARD_CONTRACT_VERSION:
            raise RankingsContractError("unsupported ranking-board contract")

    @property
    def candidate_count(self) -> int:
        return len(self.placements)

    def _content_dict(self) -> dict[str, object]:
        return {
            "board": self.board.value,
            "candidate_count": self.candidate_count,
            "contract_version": self.contract_version,
            "placements": [item.as_dict() for item in self.placements],
            "policy_checksum": self.policy_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RankingGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_recommendation_game_checksum: str
    entries: tuple[RankingEntryV1, ...]
    warnings: tuple[RankingGameWarning, ...]
    contract_version: str = RANKING_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise RankingsContractError("edge_event_id mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise RankingsContractError("daily_mlb_game_id mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise RankingsContractError("invalid canonical MLB teams")
        object.__setattr__(
            self,
            "upstream_recommendation_game_checksum",
            _sha(
                self.upstream_recommendation_game_checksum,
                "upstream_recommendation_game_checksum",
            ),
        )
        entries = tuple(self.entries)
        if len({item.upstream_recommendation_checksum for item in entries}) != len(entries):
            raise RankingsContractError("duplicate ranking entries")
        for item in entries:
            if item.source_game_id != self.source_game_id:
                raise RankingsContractError("entry source game mismatch")
            if item.edge_event_id != self.edge_event_id:
                raise RankingsContractError("entry edge event mismatch")
            if item.daily_mlb_game_id != self.daily_mlb_game_id:
                raise RankingsContractError("entry Daily MLB game mismatch")
            if item.away_team_id != self.away_team_id or item.home_team_id != self.home_team_id:
                raise RankingsContractError("entry teams mismatch")
            if (
                item.upstream_recommendation_game_checksum
                != self.upstream_recommendation_game_checksum
            ):
                raise RankingsContractError("entry recommendation-game lineage mismatch")
        object.__setattr__(self, "entries", entries)
        warnings = tuple(sorted(set(self.warnings), key=lambda item: item.value))
        object.__setattr__(self, "warnings", warnings)
        if not entries and warnings != (RankingGameWarning.NO_RECOMMENDATION_ROWS,):
            raise RankingsContractError("empty ranking game requires warning")
        if entries and warnings:
            raise RankingsContractError("ranking game with entries cannot carry warning")
        if self.contract_version != RANKING_GAME_CONTRACT_VERSION:
            raise RankingsContractError("unsupported ranking-game contract")

    @property
    def eligible_count(self) -> int:
        return sum(item.ranking_eligible for item in self.entries)

    @property
    def unranked_count(self) -> int:
        return len(self.entries) - self.eligible_count

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "edge_event_id": self.edge_event_id,
            "eligible_count": self.eligible_count,
            "entries": [item.as_dict() for item in self.entries],
            "home_team_id": self.home_team_id,
            "source_game_id": self.source_game_id,
            "unranked_count": self.unranked_count,
            "upstream_recommendation_game_checksum": (
                self.upstream_recommendation_game_checksum
            ),
            "warnings": [item.value for item in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RankingsV1:
    requested_date: str
    as_of_time: datetime
    ranked_at: datetime
    upstream_recommendation_gate_checksum: str
    policy: RankingsPolicyV1
    games: tuple[RankingGameV1, ...]
    top_confidence: RankingBoardV1
    best_value: RankingBoardV1
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = RANKINGS_CONTRACT_VERSION
    sport: str = "MLB"
    league: str = "MLB"

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "ranked_at", _utc(self.ranked_at, "ranked_at"))
        object.__setattr__(
            self,
            "upstream_recommendation_gate_checksum",
            _sha(
                self.upstream_recommendation_gate_checksum,
                "upstream_recommendation_gate_checksum",
            ),
        )
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise RankingsContractError("duplicate ranking games")
        object.__setattr__(self, "games", games)
        if self.top_confidence.board is not RankingBoardType.TOP_CONFIDENCE:
            raise RankingsContractError("top_confidence board type mismatch")
        if self.best_value.board is not RankingBoardType.BEST_VALUE:
            raise RankingsContractError("best_value board type mismatch")
        if self.top_confidence.policy_checksum != self.policy.checksum:
            raise RankingsContractError("Top Confidence policy checksum mismatch")
        if self.best_value.policy_checksum != self.policy.checksum:
            raise RankingsContractError("Best Value policy checksum mismatch")

        entries = [entry for game in games for entry in game.entries]
        if len({entry.upstream_recommendation_checksum for entry in entries}) != len(entries):
            raise RankingsContractError("duplicate recommendation lineage across games")
        if any(entry.policy_checksum != self.policy.checksum for entry in entries):
            raise RankingsContractError("ranking-entry policy checksum mismatch")
        eligible = {entry.checksum: entry for entry in entries if entry.ranking_eligible}
        top_placements = {
            item.ranking_entry_checksum: item for item in self.top_confidence.placements
        }
        value_placements = {
            item.ranking_entry_checksum: item for item in self.best_value.placements
        }
        if set(top_placements) != set(eligible):
            raise RankingsContractError("Top Confidence candidate inventory mismatch")
        if set(value_placements) != set(eligible):
            raise RankingsContractError("Best Value candidate inventory mismatch")
        for checksum, entry in eligible.items():
            top = top_placements[checksum]
            value = value_placements[checksum]
            if entry.top_confidence_rank != top.rank:
                raise RankingsContractError("Top Confidence embedded rank mismatch")
            if entry.best_value_rank != value.rank:
                raise RankingsContractError("Best Value embedded rank mismatch")
            if top.upstream_recommendation_checksum != entry.upstream_recommendation_checksum:
                raise RankingsContractError("Top Confidence recommendation lineage mismatch")
            if value.upstream_recommendation_checksum != entry.upstream_recommendation_checksum:
                raise RankingsContractError("Best Value recommendation lineage mismatch")
            if top.decision is not entry.decision or value.decision is not entry.decision:
                raise RankingsContractError("placement decision mismatch")
        if self.contract_version != RANKINGS_CONTRACT_VERSION:
            raise RankingsContractError("unsupported Rankings V1 contract")
        if self.sport != "MLB" or self.league != "MLB":
            raise RankingsContractError("sport and league must be MLB")
        payload = self._content_dict()
        configured = tuple(str(item) for item in secret_values if str(item))
        if redact_value(payload, configured, preserve_field_names=("key",)) != payload:
            raise RankingsContractError("Rankings V1 contains credential-bearing material")

    @property
    def entry_count(self) -> int:
        return sum(len(game.entries) for game in self.games)

    @property
    def eligible_count(self) -> int:
        return sum(game.eligible_count for game in self.games)

    @property
    def unranked_count(self) -> int:
        return self.entry_count - self.eligible_count

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "best_value": self.best_value.as_dict(),
            "contract_version": self.contract_version,
            "eligible_count": self.eligible_count,
            "entry_count": self.entry_count,
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "ranked_at": self.ranked_at.isoformat(),
            "requested_date": self.requested_date,
            "sport": self.sport,
            "top_confidence": self.top_confidence.as_dict(),
            "unranked_count": self.unranked_count,
            "upstream_recommendation_gate_checksum": (
                self.upstream_recommendation_gate_checksum
            ),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
