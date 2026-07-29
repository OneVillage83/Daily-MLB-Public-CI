from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256

RECOMMENDATION_POLICY_ID = "DSE_MLB_RECOMMENDATION_GATE_V1"
RECOMMENDATION_POLICY_VERSION = "1.0.0"

_APPROVED_MIN_BET_BOOKS = 4
_APPROVED_MIN_BET_EDGE = 0.03
_APPROVED_MIN_BET_EV = 0.02
_APPROVED_MIN_BET_CONFIDENCE = 80
_APPROVED_MIN_LEAN_BOOKS = 2
_APPROVED_MIN_LEAN_EDGE = 0.01
_APPROVED_MIN_LEAN_EV = 0.0
_APPROVED_MIN_LEAN_CONFIDENCE = 60


class RecommendationPolicyError(ValueError):
    """Raised when a Recommendation Gate policy weakens approved V1 floors."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RecommendationPolicyError(f"{name} must be numeric")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise RecommendationPolicyError(f"{name} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class RecommendationPolicyV1:
    minimum_bet_book_count: int = _APPROVED_MIN_BET_BOOKS
    minimum_bet_no_vig_edge: float = _APPROVED_MIN_BET_EDGE
    minimum_bet_ev_per_unit: float = _APPROVED_MIN_BET_EV
    minimum_bet_confidence_score: int = _APPROVED_MIN_BET_CONFIDENCE
    minimum_lean_book_count: int = _APPROVED_MIN_LEAN_BOOKS
    minimum_lean_no_vig_edge: float = _APPROVED_MIN_LEAN_EDGE
    minimum_lean_ev_per_unit: float = _APPROVED_MIN_LEAN_EV
    minimum_lean_confidence_score: int = _APPROVED_MIN_LEAN_CONFIDENCE
    policy_id: str = RECOMMENDATION_POLICY_ID
    policy_version: str = RECOMMENDATION_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.policy_id != RECOMMENDATION_POLICY_ID:
            raise RecommendationPolicyError("unsupported policy_id")
        if self.policy_version != RECOMMENDATION_POLICY_VERSION:
            raise RecommendationPolicyError("unsupported policy_version")
        for name in (
            "minimum_bet_book_count",
            "minimum_bet_confidence_score",
            "minimum_lean_book_count",
            "minimum_lean_confidence_score",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise RecommendationPolicyError(f"{name} must be an integer")
        if self.minimum_bet_book_count < _APPROVED_MIN_BET_BOOKS:
            raise RecommendationPolicyError("BET book threshold cannot be weakened")
        if self.minimum_lean_book_count < _APPROVED_MIN_LEAN_BOOKS:
            raise RecommendationPolicyError("LEAN book threshold cannot be weakened")
        if self.minimum_bet_confidence_score < _APPROVED_MIN_BET_CONFIDENCE:
            raise RecommendationPolicyError("BET confidence threshold cannot be weakened")
        if self.minimum_lean_confidence_score < _APPROVED_MIN_LEAN_CONFIDENCE:
            raise RecommendationPolicyError("LEAN confidence threshold cannot be weakened")
        for name, floor in (
            ("minimum_bet_no_vig_edge", _APPROVED_MIN_BET_EDGE),
            ("minimum_bet_ev_per_unit", _APPROVED_MIN_BET_EV),
            ("minimum_lean_no_vig_edge", _APPROVED_MIN_LEAN_EDGE),
            ("minimum_lean_ev_per_unit", _APPROVED_MIN_LEAN_EV),
        ):
            value = _finite(getattr(self, name), name)
            object.__setattr__(self, name, value)
            if value < floor:
                raise RecommendationPolicyError(f"{name} cannot be weakened")
        if self.minimum_bet_book_count < self.minimum_lean_book_count:
            raise RecommendationPolicyError("BET book threshold cannot trail LEAN")
        if self.minimum_bet_no_vig_edge < self.minimum_lean_no_vig_edge:
            raise RecommendationPolicyError("BET edge threshold cannot trail LEAN")
        if self.minimum_bet_ev_per_unit <= self.minimum_lean_ev_per_unit:
            raise RecommendationPolicyError("BET EV threshold must exceed LEAN")
        if self.minimum_bet_confidence_score < self.minimum_lean_confidence_score:
            raise RecommendationPolicyError("BET confidence cannot trail LEAN")
        if not 0 <= self.minimum_lean_confidence_score <= 100:
            raise RecommendationPolicyError("LEAN confidence must be in [0,100]")
        if not 0 <= self.minimum_bet_confidence_score <= 100:
            raise RecommendationPolicyError("BET confidence must be in [0,100]")

    def as_dict(self) -> dict[str, object]:
        return {
            "minimum_bet_book_count": self.minimum_bet_book_count,
            "minimum_bet_confidence_score": self.minimum_bet_confidence_score,
            "minimum_bet_ev_per_unit": self.minimum_bet_ev_per_unit,
            "minimum_bet_no_vig_edge": self.minimum_bet_no_vig_edge,
            "minimum_lean_book_count": self.minimum_lean_book_count,
            "minimum_lean_confidence_score": self.minimum_lean_confidence_score,
            "minimum_lean_ev_per_unit": self.minimum_lean_ev_per_unit,
            "minimum_lean_no_vig_edge": self.minimum_lean_no_vig_edge,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


DEFAULT_RECOMMENDATION_POLICY_V1 = RecommendationPolicyV1()
