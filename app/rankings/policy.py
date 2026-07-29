from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256

RANKINGS_POLICY_ID = "DSE_MLB_RANKINGS_POLICY_V1"
RANKINGS_POLICY_VERSION = "1.0.0"

_TOP_CONFIDENCE_SORT = (
    "decision_tier_asc",
    "conditional_model_probability_desc",
    "evidence_confidence_score_desc",
    "expected_value_per_unit_desc",
    "no_vig_probability_edge_desc",
    "bookmaker_count_desc",
    "operational_risk_score_asc",
    "canonical_identity_asc",
    "upstream_recommendation_checksum_asc",
)

_BEST_VALUE_SORT = (
    "decision_tier_asc",
    "expected_value_per_unit_desc",
    "no_vig_probability_edge_desc",
    "evidence_confidence_score_desc",
    "conditional_model_probability_desc",
    "bookmaker_count_desc",
    "operational_risk_score_asc",
    "canonical_identity_asc",
    "upstream_recommendation_checksum_asc",
)


class RankingsPolicyError(ValueError):
    """Raised when the immutable Rankings V1 policy is altered."""


@dataclass(frozen=True, slots=True)
class RankingsPolicyV1:
    policy_id: str = RANKINGS_POLICY_ID
    policy_version: str = RANKINGS_POLICY_VERSION
    eligible_decisions: tuple[str, ...] = ("BET", "LEAN")
    decision_priority: tuple[str, ...] = ("BET", "LEAN")
    top_confidence_sort: tuple[str, ...] = _TOP_CONFIDENCE_SORT
    best_value_sort: tuple[str, ...] = _BEST_VALUE_SORT
    rank_all_eligible: bool = True
    maximum_ranked_candidates: int | None = None
    allow_forced_minimum: bool = False
    allow_decision_promotion: bool = False
    composite_score_enabled: bool = False

    def __post_init__(self) -> None:
        if self.policy_id != RANKINGS_POLICY_ID:
            raise RankingsPolicyError("unsupported rankings policy_id")
        if self.policy_version != RANKINGS_POLICY_VERSION:
            raise RankingsPolicyError("unsupported rankings policy_version")
        if self.eligible_decisions != ("BET", "LEAN"):
            raise RankingsPolicyError("eligible decisions are immutable")
        if self.decision_priority != ("BET", "LEAN"):
            raise RankingsPolicyError("decision priority is immutable")
        if self.top_confidence_sort != _TOP_CONFIDENCE_SORT:
            raise RankingsPolicyError("Top Confidence ordering is immutable")
        if self.best_value_sort != _BEST_VALUE_SORT:
            raise RankingsPolicyError("Best Value ordering is immutable")
        if not self.rank_all_eligible:
            raise RankingsPolicyError("Rankings V1 must rank every eligible candidate")
        if self.maximum_ranked_candidates is not None:
            raise RankingsPolicyError("Rankings V1 cannot impose a candidate quota")
        if self.allow_forced_minimum:
            raise RankingsPolicyError("forced minimum rankings are prohibited")
        if self.allow_decision_promotion:
            raise RankingsPolicyError("decision promotion is prohibited")
        if self.composite_score_enabled:
            raise RankingsPolicyError("opaque composite scores are prohibited")

    def as_dict(self) -> dict[str, object]:
        return {
            "allow_decision_promotion": self.allow_decision_promotion,
            "allow_forced_minimum": self.allow_forced_minimum,
            "best_value_sort": list(self.best_value_sort),
            "composite_score_enabled": self.composite_score_enabled,
            "decision_priority": list(self.decision_priority),
            "eligible_decisions": list(self.eligible_decisions),
            "maximum_ranked_candidates": self.maximum_ranked_candidates,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "rank_all_eligible": self.rank_all_eligible,
            "top_confidence_sort": list(self.top_confidence_sort),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


DEFAULT_RANKINGS_POLICY_V1 = RankingsPolicyV1()
