from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Iterable

from app.stats.normalization import GameStatus


class ConflictingGameStateError(ValueError):
    """Raised when one canonical game has incompatible acquisition states."""


@dataclass(frozen=True, slots=True)
class GameAcquisitionState:
    game_id: str
    game_date: date
    status: GameStatus
    checksums_valid: bool = False
    normalized: bool = False
    validated: bool = False
    source_complete: bool = False

    @property
    def final_and_valid(self) -> bool:
        return (
            self.status is GameStatus.FINAL
            and self.checksums_valid
            and self.normalized
            and self.validated
            and self.source_complete
        )


@dataclass(frozen=True, slots=True)
class CompletenessResult:
    requested_through_date: date
    contiguous_regular_season_complete_through_date: date | None
    latest_ingested_completed_game_date: date | None
    partial_date: date | None
    partial_date_reason: str | None


COMPLETENESS_REASON_CONTRACT_VERSION = "DSE_STATS_COMPLETENESS_REASON_V1"


class CompletenessDisposition(StrEnum):
    RETRYABLE_PARTIAL = "retryable_partial"
    REVIEW_PARTIAL = "review_partial"
    HARD_FAILURE = "hard_failure"


_COMPLETENESS_REASON_DISPOSITIONS: dict[str, CompletenessDisposition] = {
    "scheduled_regular_season_game_not_final": CompletenessDisposition.RETRYABLE_PARTIAL,
    "regular_season_game_in_progress": CompletenessDisposition.RETRYABLE_PARTIAL,
    "postponed_regular_season_game_unresolved": CompletenessDisposition.RETRYABLE_PARTIAL,
    "suspended_regular_season_game_pending": CompletenessDisposition.RETRYABLE_PARTIAL,
    "abandoned_regular_season_game_unresolved": CompletenessDisposition.REVIEW_PARTIAL,
    "regular_season_game_validation_failed": CompletenessDisposition.HARD_FAILURE,
    "regular_season_game_source_incomplete": CompletenessDisposition.HARD_FAILURE,
    "final_regular_season_game_not_fully_validated": CompletenessDisposition.HARD_FAILURE,
}


def classify_completeness_reason(
    reason: str | None,
) -> CompletenessDisposition | None:
    if reason is None:
        return None

    # Unknown future reasons must never silently become success.
    return _COMPLETENESS_REASON_DISPOSITIONS.get(
        reason,
        CompletenessDisposition.HARD_FAILURE,
    )


_RESOLVED_NO_GAME = frozenset({GameStatus.CANCELLED_NO_GAME})


def _blocking_reason(games: list[GameAcquisitionState]) -> str:
    statuses = {game.status for game in games}
    precedence = (
        (GameStatus.SUSPENDED_PENDING, "suspended_regular_season_game_pending"),
        (GameStatus.POSTPONED_RESCHEDULED, "postponed_regular_season_game_unresolved"),
        (GameStatus.IN_PROGRESS, "regular_season_game_in_progress"),
        (GameStatus.SCHEDULED, "scheduled_regular_season_game_not_final"),
        (GameStatus.ABANDONED, "abandoned_regular_season_game_unresolved"),
        (GameStatus.VALIDATION_FAILED, "regular_season_game_validation_failed"),
        (GameStatus.SOURCE_INCOMPLETE, "regular_season_game_source_incomplete"),
    )
    for status, reason in precedence:
        if status in statuses:
            return reason
    return "final_regular_season_game_not_fully_validated"


def calculate_completeness(
    requested_through_date: date,
    states: Iterable[GameAcquisitionState],
) -> CompletenessResult:
    canonical_states: dict[str, GameAcquisitionState] = {}
    for state in states:
        if state.game_date > requested_through_date:
            continue
        prior = canonical_states.get(state.game_id)
        if prior is None:
            canonical_states[state.game_id] = state
        elif prior != state:
            raise ConflictingGameStateError(
                f"Conflicting acquisition states for canonical game {state.game_id!r}"
            )

    by_date: dict[date, list[GameAcquisitionState]] = {}
    for state in canonical_states.values():
        by_date.setdefault(state.game_date, []).append(state)
    validated_dates = [
        state.game_date
        for games in by_date.values()
        for state in games
        if state.final_and_valid
    ]
    latest = max(validated_dates, default=None)
    contiguous: date | None = None
    partial: date | None = None
    partial_reason: str | None = None
    for game_date in sorted(by_date):
        games = by_date[game_date]
        resolved = all(
            game.final_and_valid or game.status in _RESOLVED_NO_GAME for game in games
        )
        if not resolved:
            if partial is None:
                partial = game_date
                partial_reason = _blocking_reason(games)
            continue
        if partial is None:
            contiguous = game_date
    return CompletenessResult(
        requested_through_date=requested_through_date,
        contiguous_regular_season_complete_through_date=contiguous,
        latest_ingested_completed_game_date=latest,
        partial_date=partial,
        partial_date_reason=partial_reason,
    )
