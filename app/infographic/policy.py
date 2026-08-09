from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256

INFOGRAPHIC_POLICY_ID = "DSE_MLB_INFOGRAPHIC_V1"
INFOGRAPHIC_POLICY_VERSION = "1.0.0"


class InfographicPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InfographicPolicyV1:
    policy_id: str = INFOGRAPHIC_POLICY_ID
    policy_version: str = INFOGRAPHIC_POLICY_VERSION
    feed_width: int = 1080
    feed_height: int = 1350
    story_width: int = 1080
    story_height: int = 1920
    feed_recommendation_limit: int = 3
    story_recommendation_limit: int = 5
    background_hex: str = "#07111F"
    panel_hex: str = "#111C2D"
    primary_text_hex: str = "#F5F7FA"
    secondary_text_hex: str = "#94A3B8"
    accent_hex: str = "#3B82F6"
    recommend_hex: str = "#22C55E"
    allow_value_recalculation: bool = False
    allow_decision_rewrite: bool = False
    allow_rank_rewrite: bool = False
    allow_missing_data_fabrication: bool = False
    allow_publication_approval: bool = False
    allow_stake_sizing: bool = False

    def __post_init__(self) -> None:
        if self.policy_id != INFOGRAPHIC_POLICY_ID or self.policy_version != INFOGRAPHIC_POLICY_VERSION:
            raise InfographicPolicyError("unsupported Infographic V1 policy")
        if (self.feed_width, self.feed_height) != (1080, 1350) or (self.story_width, self.story_height) != (1080, 1920):
            raise InfographicPolicyError("Infographic V1 canvas dimensions are frozen")
        for name in ("feed_recommendation_limit", "story_recommendation_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InfographicPolicyError(f"{name} must be positive")
        if any(
            (
                self.allow_value_recalculation,
                self.allow_decision_rewrite,
                self.allow_rank_rewrite,
                self.allow_missing_data_fabrication,
                self.allow_publication_approval,
                self.allow_stake_sizing,
            )
        ):
            raise InfographicPolicyError("Infographic V1 prohibited behavior cannot be enabled")

    def as_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())
