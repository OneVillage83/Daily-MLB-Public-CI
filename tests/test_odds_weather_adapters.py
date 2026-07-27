from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.collectors.odds_collector import (
    OddsCollectionResult,
    OddsCollectorWarning,
)
from app.http import HttpRequestDiagnostics
from app.odds_weather.adapters import (
    OddsWeatherAdapterError,
    nws_forecast_to_phase4,
    odds_collection_to_phase4,
    openweather_forecast_to_phase4,
)
from app.odds_weather.contracts import WeatherProvider
from app.raw_payloads import RawPayloadCapture, sanitized_checksum


def _event() -> dict[str, object]:
    return {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-27T23:10:00Z",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": "2026-07-27T14:14:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": "2026-07-27T14:14:00Z",
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": -120},
                            {"name": "San Francisco Giants", "price": 110},
                        ],
                    }
                ],
            }
        ],
    }


def _request() -> HttpRequestDiagnostics:
    return HttpRequestDiagnostics(
        request_status="success",
        status_code=200,
        attempts=1,
        retries_performed=0,
        duration_seconds=0.1,
        response_date_utc="2026-07-27T14:15:00+00:00",
    )


def _forecast() -> dict[str, object]:
    return {
        "forecast_time": "2026-07-27T23:10:00+00:00",
        "forecast_offset_minutes": 0.0,
        "forecast_generated_at": "2026-07-27T14:10:00+00:00",
        "temperature_f": 72.0,
        "humidity_pct": 45.0,
        "precipitation_probability_pct": 10.0,
        "wind_speed_mph": 8.0,
        "wind_direction_deg": 270.0,
        "short_forecast": "Clear",
    }


def test_odds_collection_adapter_preserves_raw_checksum_history_and_warnings() -> None:
    event = _event()
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[event],
        retrieved_at="2026-07-27T14:15:00+00:00",
        provider_timestamp=None,
        content_type="application/json",
    )
    warning = OddsCollectorWarning(
        code="malformed_bookmaker",
        message="Excluded malformed bookmaker",
        created_at="2026-07-27T14:15:00+00:00",
        event_id="event-1",
        bookmaker="bad-book",
    )
    collection = OddsCollectionResult(
        games=[dict(event)],
        quota={
            "requests_remaining": "99",
            "requests_used": "1",
            "requests_last": "1",
        },
        capture=capture,
        request=_request(),
        warnings=(warning,),
    )
    history = {
        "event-1": (
            {
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
                "price_american": -125,
                "point": None,
                "provider_last_update": "2026-07-27T13:00:00+00:00",
                "retrieved_at": "2026-07-27T13:01:00+00:00",
            },
        )
    }

    result = odds_collection_to_phase4(
        collection,
        history_rows_by_event=history,
    )

    assert len(result.events) == 1
    evidence = result.events[0]
    assert evidence.provider_event_id == "event-1"
    assert evidence.raw_capture_checksum == sanitized_checksum(capture)
    assert evidence.retrieved_at == datetime(
        2026, 7, 27, 14, 15, tzinfo=timezone.utc
    )
    assert len(evidence.history_rows) == 1
    assert result.warnings[0].code == "malformed_bookmaker"
    assert result.warnings[0].provider == "bad-book"
    assert result.warnings[0].provider_event_id == "event-1"


def test_nws_adapter_uses_both_raw_captures_and_latest_retrieval_time() -> None:
    point_capture = RawPayloadCapture(
        provider="nws",
        endpoint_category="point_lookup",
        payload={"properties": {"forecastHourly": "https://example.test/hourly"}},
        retrieved_at="2026-07-27T14:14:00+00:00",
        provider_timestamp="2026-07-27T14:13:00+00:00",
        content_type="application/geo+json",
    )
    forecast_capture = RawPayloadCapture(
        provider="nws",
        endpoint_category="hourly_forecast",
        payload={"properties": {"periods": []}},
        retrieved_at="2026-07-27T14:16:00+00:00",
        provider_timestamp="2026-07-27T14:15:00+00:00",
        content_type="application/geo+json",
    )

    evidence = nws_forecast_to_phase4(
        source_game_id="900001",
        forecast=_forecast(),
        point_capture=point_capture,
        forecast_capture=forecast_capture,
    )

    assert evidence.provider is WeatherProvider.NWS
    assert evidence.retrieved_at == datetime(
        2026, 7, 27, 14, 16, tzinfo=timezone.utc
    )
    assert set(evidence.raw_capture_checksums) == {
        sanitized_checksum(point_capture),
        sanitized_checksum(forecast_capture),
    }


def test_openweather_adapter_uses_one_call_capture() -> None:
    capture = RawPayloadCapture(
        provider="openweather",
        endpoint_category="one_call",
        payload={"hourly": []},
        retrieved_at="2026-07-27T14:16:00+00:00",
        provider_timestamp="2026-07-27T14:15:00+00:00",
        content_type="application/json",
    )
    forecast = _forecast()
    forecast.update(
        {
            "wind_gust_mph": 11.0,
            "clouds_pct": 10.0,
            "pressure_hpa": 1012.0,
        }
    )

    evidence = openweather_forecast_to_phase4(
        source_game_id="900001",
        forecast=forecast,
        capture=capture,
    )

    assert evidence.provider is WeatherProvider.OPENWEATHER
    assert evidence.raw_capture_checksums == (sanitized_checksum(capture),)


def test_adapter_rejects_wrong_raw_capture_provider_or_endpoint() -> None:
    wrong = RawPayloadCapture(
        provider="openweather",
        endpoint_category="one_call",
        payload={"hourly": []},
        retrieved_at="2026-07-27T14:16:00+00:00",
        provider_timestamp=None,
        content_type="application/json",
    )
    forecast_capture = RawPayloadCapture(
        provider="nws",
        endpoint_category="hourly_forecast",
        payload={"properties": {"periods": []}},
        retrieved_at="2026-07-27T14:16:00+00:00",
        provider_timestamp=None,
        content_type="application/geo+json",
    )

    with pytest.raises(OddsWeatherAdapterError, match="provider/endpoint"):
        nws_forecast_to_phase4(
            source_game_id="900001",
            forecast=_forecast(),
            point_capture=wrong,
            forecast_capture=forecast_capture,
        )
