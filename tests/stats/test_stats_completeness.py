from __future__ import annotations

from datetime import date

import pytest

from app.stats.completeness import (
    ConflictingGameStateError,
    GameAcquisitionState,
    calculate_completeness,
)
from app.stats.normalization import GameStatus


def _state(day: int, status: GameStatus, *, valid: bool = False, game_id: str | None = None) -> GameAcquisitionState:
    return GameAcquisitionState(
        game_id=game_id or f"game-{day}",
        game_date=date(2026, 7, day),
        status=status,
        checksums_valid=valid,
        normalized=valid,
        validated=valid,
        source_complete=valid,
    )


def test_july_16_nonfinal_does_not_advance_either_watermark() -> None:
    result = calculate_completeness(
        date(2026, 7, 16),
        [_state(12, GameStatus.FINAL, valid=True), _state(16, GameStatus.SCHEDULED)],
    )
    assert result.contiguous_regular_season_complete_through_date == date(2026, 7, 12)
    assert result.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert result.partial_date == date(2026, 7, 16)
    assert result.partial_date_reason == "scheduled_regular_season_game_not_final"


def test_july_16_advances_only_after_full_validation() -> None:
    failed = calculate_completeness(
        date(2026, 7, 16),
        [_state(12, GameStatus.FINAL, valid=True), _state(16, GameStatus.FINAL)],
    )
    assert failed.contiguous_regular_season_complete_through_date == date(2026, 7, 12)
    assert failed.partial_date_reason == "final_regular_season_game_not_fully_validated"

    passed = calculate_completeness(
        date(2026, 7, 16),
        [_state(12, GameStatus.FINAL, valid=True), _state(16, GameStatus.FINAL, valid=True)],
    )
    assert passed.contiguous_regular_season_complete_through_date == date(2026, 7, 16)
    assert passed.latest_ingested_completed_game_date == date(2026, 7, 16)
    assert passed.partial_date is None


def test_later_final_game_advances_latest_but_not_contiguous_watermark() -> None:
    result = calculate_completeness(
        date(2026, 7, 18),
        [
            _state(12, GameStatus.FINAL, valid=True),
            _state(16, GameStatus.SUSPENDED_PENDING),
            _state(18, GameStatus.FINAL, valid=True),
        ],
    )
    assert result.contiguous_regular_season_complete_through_date == date(2026, 7, 12)
    assert result.latest_ingested_completed_game_date == date(2026, 7, 18)
    assert result.partial_date == date(2026, 7, 16)


def test_cancelled_only_resolved_date_advances_contiguous_not_latest() -> None:
    result = calculate_completeness(
        date(2026, 7, 16),
        [
            _state(12, GameStatus.FINAL, valid=True),
            _state(16, GameStatus.CANCELLED_NO_GAME),
        ],
    )
    assert result.contiguous_regular_season_complete_through_date == date(2026, 7, 16)
    assert result.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert result.partial_date is None


def test_identical_canonical_game_states_are_deduplicated() -> None:
    state = _state(16, GameStatus.FINAL, valid=True, game_id="canonical-1")
    result = calculate_completeness(date(2026, 7, 16), [state, state])
    assert result.contiguous_regular_season_complete_through_date == date(2026, 7, 16)
    assert result.latest_ingested_completed_game_date == date(2026, 7, 16)


def test_conflicting_canonical_game_states_fail_closed() -> None:
    with pytest.raises(ConflictingGameStateError, match="canonical-1"):
        calculate_completeness(
            date(2026, 7, 16),
            [
                _state(16, GameStatus.SCHEDULED, game_id="canonical-1"),
                _state(16, GameStatus.FINAL, valid=True, game_id="canonical-1"),
            ],
        )
