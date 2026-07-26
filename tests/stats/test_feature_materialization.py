from __future__ import annotations

from datetime import date

import pytest

from app.stats.feature_materialization import (
    FeatureMaterializationError,
    FeatureMaterializationRequest,
    FeatureMaterializationService,
    _inning_half,
)


def test_materialization_request_rejects_non_service_stats_run_id() -> None:
    with pytest.raises(FeatureMaterializationError):
        FeatureMaterializationRequest(
            feature_as_of=date(2026, 7, 23),
            source_stats_run_id="not-a-stats-run",
        )


def test_materialization_completeness_preserves_partial_source_boundary() -> None:
    source = {
        "requested_through_date": "2026-07-23",
        "latest_ingested_completed_game_date": "2026-07-22",
        "contiguous_regular_season_complete_through_date": "2026-07-22",
        "partial_date": "2026-07-23",
        "partial_date_reason": "scheduled_regular_season_game_not_final",
    }

    result = FeatureMaterializationService._completeness(
        source,
        date(2026, 7, 23),
    )

    assert result.latest_ingested_completed_game_date == date(2026, 7, 22)
    assert result.contiguous_regular_season_complete_through_date == date(
        2026, 7, 22
    )
    assert result.partial_date == date(2026, 7, 23)
    assert result.partial_date_reason == "scheduled_regular_season_game_not_final"


def test_materialization_completeness_fails_closed_on_inconsistent_partial() -> None:
    source = {
        "requested_through_date": "2026-07-23",
        "latest_ingested_completed_game_date": "2026-07-22",
        "contiguous_regular_season_complete_through_date": "2026-07-22",
        "partial_date": "2026-07-23",
        "partial_date_reason": None,
    }

    with pytest.raises(FeatureMaterializationError):
        FeatureMaterializationService._completeness(source, date(2026, 7, 23))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Top", "top"), ("Bot", "bottom"), ("bottom", "bottom"), ("", None)],
)
def test_inning_half_matches_existing_statcast_contract(
    raw: str, expected: str | None
) -> None:
    assert _inning_half(raw) == expected
