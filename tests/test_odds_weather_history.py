from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.odds_weather.history import (
    OddsHistorySelectionError,
    select_odds_history_at,
)


class FakeHistorySource:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requested_event_ids: list[str] = []

    def get_odds_history(self, event_id: str) -> list[dict[str, Any]]:
        self.requested_event_ids.append(event_id)
        return list(self.rows)


def test_history_selector_excludes_rows_after_point_in_time_cutoff() -> None:
    source = FakeHistorySource(
        [
            {
                "id": 3,
                "event_id": "event-1",
                "retrieved_at": "2026-07-27T15:00:00+00:00",
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price": -130,
            },
            {
                "id": 1,
                "event_id": "event-1",
                "retrieved_at": "2026-07-27T13:00:00+00:00",
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price": -110,
            },
            {
                "id": 2,
                "event_id": "event-1",
                "retrieved_at": "2026-07-27T14:00:00Z",
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price": -120,
            },
        ]
    )

    selected = select_odds_history_at(
        source,
        provider_event_id="event-1",
        observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
    )

    assert source.requested_event_ids == ["event-1"]
    assert [row["id"] for row in selected] == [1, 2]
    assert [row["price"] for row in selected] == [-110, -120]


def test_history_selector_rejects_malformed_or_naive_retrieval_time() -> None:
    malformed = FakeHistorySource(
        [{"event_id": "event-1", "retrieved_at": "not-a-time"}]
    )
    with pytest.raises(OddsHistorySelectionError, match="malformed"):
        select_odds_history_at(
            malformed,
            provider_event_id="event-1",
            observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        )

    naive = FakeHistorySource(
        [{"event_id": "event-1", "retrieved_at": "2026-07-27T13:00:00"}]
    )
    with pytest.raises(OddsHistorySelectionError, match="timezone-aware"):
        select_odds_history_at(
            naive,
            provider_event_id="event-1",
            observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        )


def test_history_selector_rejects_cross_event_row_and_naive_cutoff() -> None:
    source = FakeHistorySource(
        [
            {
                "event_id": "event-2",
                "retrieved_at": "2026-07-27T13:00:00+00:00",
            }
        ]
    )
    with pytest.raises(OddsHistorySelectionError, match="event_id disagrees"):
        select_odds_history_at(
            source,
            provider_event_id="event-1",
            observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        )

    with pytest.raises(OddsHistorySelectionError, match="timezone-aware"):
        select_odds_history_at(
            FakeHistorySource([]),
            provider_event_id="event-1",
            observed_at=datetime(2026, 7, 27, 14),
        )


def test_history_selector_rejects_empty_event_id_and_invalid_row_id() -> None:
    with pytest.raises(OddsHistorySelectionError, match="non-empty"):
        select_odds_history_at(
            FakeHistorySource([]),
            provider_event_id="   ",
            observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        )

    source = FakeHistorySource(
        [
            {
                "id": True,
                "event_id": "event-1",
                "retrieved_at": "2026-07-27T13:00:00+00:00",
            }
        ]
    )
    with pytest.raises(OddsHistorySelectionError, match="id must be an integer"):
        select_odds_history_at(
            source,
            provider_event_id="event-1",
            observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        )
