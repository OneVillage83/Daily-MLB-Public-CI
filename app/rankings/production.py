from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
import math

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.recommendation_gate.production import ProductionRecommendationGateV1
from app.redaction import redact_value

RANKINGS_PRODUCTION_CONTRACT = "DSE_MLB_ML_RANKINGS_V1"
RANKINGS_PHASE_INPUT_CONTRACT = "DSE_MLB_ML_RANKINGS_PHASE_INPUT_V1"
RANKING_POLICY_VERSION = "DSE_MLB_ML_LEXICOGRAPHIC_RANKING_V1"
RANKING_V1_COMPARATOR = (
    "decision_eligibility",
    "ev_desc",
    "edge_desc",
    "lower_bound_clearance_desc",
    "interval_width_asc",
    "data_quality_disposition",
    "bookmaker_count_desc",
    "scheduled_start_time",
    "source_game_id",
    "selected_team_id",
)
DATA_QUALITY_RANK = {"ready": 0, "degraded": 2, "insufficient": 3}


class RankingsProductionError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RankingsProductionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RankingPolicyV1:
    comparator: tuple[str, ...] = RANKING_V1_COMPARATOR
    policy_version: str = RANKING_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.comparator != RANKING_V1_COMPARATOR or self.policy_version != RANKING_POLICY_VERSION:
            raise RankingsProductionError("V1 ranking comparator and version must match the frozen policy")

    def identity_dict(self) -> dict[str, object]:
        return {"comparator": list(self.comparator), "policy_version": self.policy_version}

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class RankingEntryV1:
    ordinal: int
    source_game_id: str
    decision: str
    selected_side: str | None
    selected_team_id: str | None
    rank_eligible: bool
    recommendation_rank: int | None
    upstream_gate_game_checksum: str
    ranking_keys: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.decision not in {"recommend", "pass", "avoid"}:
            raise RankingsProductionError("ranking cannot create a new decision")
        if self.rank_eligible != (self.decision == "recommend"):
            raise RankingsProductionError("rank eligibility must equal recommendation decision")
        if self.rank_eligible != (self.recommendation_rank is not None):
            raise RankingsProductionError("recommendation rank identity mismatch")
        if self.recommendation_rank is not None and (
            isinstance(self.recommendation_rank, bool)
            or not isinstance(self.recommendation_rank, int)
            or self.recommendation_rank < 1
        ):
            raise RankingsProductionError("recommendation rank must be a positive integer")

    def identity_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "ordinal": self.ordinal,
            "rank_eligible": self.rank_eligible,
            "ranking_keys": dict(self.ranking_keys),
            "recommendation_rank": self.recommendation_rank,
            "selected_side": self.selected_side,
            "selected_team_id": self.selected_team_id,
            "source_game_id": self.source_game_id,
            "upstream_gate_game_checksum": self.upstream_gate_game_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ProductionRankingsV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    ranked_at: datetime
    policy: RankingPolicyV1
    upstream_gate_snapshot_id: str
    upstream_gate_checksum: str
    entries: tuple[RankingEntryV1, ...]
    warnings: tuple[Mapping[str, object], ...] = ()
    contract_version: str = RANKINGS_PRODUCTION_CONTRACT
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "ranked_at", _utc(self.ranked_at, "ranked_at"))
        if [entry.ordinal for entry in self.entries] != list(range(1, len(self.entries) + 1)):
            raise RankingsProductionError("ranking ordinals must retain slate order")
        ranks = [
            entry.recommendation_rank
            for entry in self.entries
            if entry.rank_eligible and entry.recommendation_rank is not None
        ]
        if len(ranks) != sum(entry.rank_eligible for entry in self.entries) or any(
            isinstance(rank, bool) or rank < 1 for rank in ranks
        ):
            raise RankingsProductionError("recommendation ranks must be positive integers")
        if len(set(ranks)) != len(ranks) or sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise RankingsProductionError("recommendation ranks must be contiguous")
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(self.identity_dict(), configured) != self.identity_dict():
            raise RankingsProductionError("ranking snapshot contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "entries": [entry.as_dict() for entry in self.entries],
            "policy": self.policy.as_dict(),
            "ranked_at": self.ranked_at.isoformat(),
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_gate_checksum": self.upstream_gate_checksum,
            "upstream_gate_snapshot_id": self.upstream_gate_snapshot_id,
            "warnings": [dict(value) for value in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def build_rankings(
    gate: ProductionRecommendationGateV1,
    *,
    value_metrics_by_game: Mapping[str, Mapping[str, object]],
    scheduled_start_by_game: Mapping[str, datetime],
    policy: RankingPolicyV1,
    ranked_at: datetime,
    secret_values: Iterable[str] = (),
) -> ProductionRankingsV1:
    recommendations = []
    for game in gate.games:
        if game.decision != "recommend":
            continue
        metrics = value_metrics_by_game[game.source_game_id]

        def metric_float(name: str) -> float:
            value = metrics.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RankingsProductionError(f"ranking metric {name} must be numeric")
            result = float(value)
            if not math.isfinite(result):
                raise RankingsProductionError(f"ranking metric {name} must be finite")
            return result

        def metric_int(name: str) -> int:
            value = metrics.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RankingsProductionError(f"ranking metric {name} must be an integer")
            return value

        quality = str(game.quality_disposition)
        if quality not in DATA_QUALITY_RANK:
            raise RankingsProductionError("ranking Data Quality disposition is invalid")
        if game.selected_team_id is None:
            raise RankingsProductionError("recommended game requires selected team identity")

        recommendations.append(
            (
                -metric_float("ev"),
                -metric_float("edge"),
                -metric_float("lower_bound_clearance"),
                metric_float("interval_width"),
                DATA_QUALITY_RANK[quality],
                -metric_int("bookmaker_count"),
                _utc(scheduled_start_by_game[game.source_game_id], "scheduled_start"),
                game.source_game_id,
                game.selected_team_id,
                game.source_game_id,
            )
        )
    ranked_ids = {item[-1]: rank for rank, item in enumerate(sorted(recommendations), 1)}
    entries = tuple(
        RankingEntryV1(
            ordinal=game.ordinal,
            source_game_id=game.source_game_id,
            decision=game.decision,
            selected_side=game.selected_side,
            selected_team_id=game.selected_team_id,
            rank_eligible=game.decision == "recommend",
            recommendation_rank=ranked_ids.get(game.source_game_id),
            upstream_gate_game_checksum=game.checksum,
            ranking_keys=dict(value_metrics_by_game.get(game.source_game_id, {})),
        )
        for game in gate.games
    )
    return ProductionRankingsV1(
        gate.run_id,
        gate.requested_date,
        gate.as_of_time,
        ranked_at,
        policy,
        f"recommendation-gate:{gate.checksum}",
        gate.checksum,
        entries,
        secret_values=secret_values,
    )
