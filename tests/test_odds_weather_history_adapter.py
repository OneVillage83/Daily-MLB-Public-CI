from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.collectors.odds_collector import OddsCollectionResult
from app.http import HttpRequestDiagnostics
from app.odds_weather.adapters import odds_collection_to_phase4
from app.raw_payloads import RawPayloadCapture


class FakeHistorySource:
    def get_odds_history(self, event_id: str) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "event_id": event_id,
                "run_id": "run-1",
                "retrieved_at": "2026-07-27T13:00:00+00:00",
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price": -110,
                "point": None,
            },
            {
                "id": 2,
                "event_id": event_id,
                "run_id": "run-2",
                "retrieved_at": "2026-07-27T15:00:00+00:00",
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price": -140,
                "point": None,
            },
        ]


def test_odds_adapter_selects_history_at_explicit_phase_cutoff() -> None:
    event = {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-27T23:10:00Z",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "bookmakers": [],
    }
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[event],
        retrieved_at="2026-07-27T14:15:00+00:00",
        provider_timestamp=None,
        content_type="application/json",
    )
    collection = OddsCollectionResult(
        games=[event],
        quota={
            "requests_remaining": None,
            "requests_used": None,
            "requests_last": None,
        },
        capture=capture,
        request=HttpRequestDiagnostics(
            request_status="success",
            status_code=200,
            attempts=1,
            retries_performed=0,
            duration_seconds=0.1,
            response_date_utc="2026-07-27T14:15:00+00:00",
        ),
        warnings=(),
    )

    result = odds_collection_to_phase4(
        collection,
        history_source=FakeHistorySource(),
        history_observed_at=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
    )

    assert len(result.events) == 1
    history = result.events[0].history_rows
    assert len(history) == 1
    assert history[0]["id"] == 1
    assert history[0]["price"] == -110
