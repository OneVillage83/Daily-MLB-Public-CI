from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from app.artifacts import ArtifactPaths
from app.collectors.nws_weather_collector import NwsWeatherCollector
from app.collectors.odds_collector import OddsCollector
from app.collectors.openweather_collector import OpenWeatherCollector
from app.config import Settings
from app.database import Database
from app.http import HttpClient
from app.identifiers import generate_run_id
from app.pipeline import _persist_raw_capture
from app.raw_payloads import (
    RawPayloadCapture,
    capture_response,
    sanitized_checksum,
    sanitized_json_bytes,
)

SECRET = "provider-secret-value"
FIXTURE_DIR = Path(__file__).with_name("fixtures")


@dataclass
class FakeResponse:
    headers: dict[str, str]


class FakeHttp:
    def __init__(self, responses: list[tuple[dict[str, Any] | list[Any], FakeResponse]]) -> None:
        self.responses = responses

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | list[Any], FakeResponse]:
        del url, params, headers
        return self.responses.pop(0)


def as_http_client(fake: FakeHttp) -> HttpClient:
    return cast(HttpClient, fake)


def test_capture_is_frozen_and_endpoint_categories_are_allowlisted() -> None:
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[],
        retrieved_at="2026-07-11T12:00:00+00:00",
        provider_timestamp=None,
        content_type="application/json",
    )

    with pytest.raises(FrozenInstanceError):
        setattr(capture, "provider", "nws")
    with pytest.raises(ValueError, match="not valid"):
        RawPayloadCapture(
            provider="nws",
            endpoint_category="one_call",
            payload={},
            retrieved_at="2026-07-11T12:00:00+00:00",
            provider_timestamp=None,
            content_type="application/json",
        )


def test_sanitized_serialization_is_stable_and_removes_secrets() -> None:
    first = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        retrieved_at="2026-07-11T12:00:00+00:00",
        provider_timestamp="2026-07-11T11:59:00+00:00",
        content_type="application/json",
        payload={
            "message": f"upstream echoed {SECRET}",
            "apiKey": SECRET,
            "url": f"https://example.test/odds?apiKey={SECRET}&region=us",
        },
    )
    second = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        retrieved_at="2026-07-11T13:00:00+00:00",
        provider_timestamp="2026-07-11T11:59:00+00:00",
        content_type="application/geo+json",
        payload={
            "url": f"https://example.test/odds?apiKey={SECRET}&region=us",
            "apiKey": SECRET,
            "message": f"upstream echoed {SECRET}",
        },
    )

    serialized = sanitized_json_bytes(first, secret_values=(SECRET,))
    assert SECRET.encode() not in serialized
    assert b"request_url" not in serialized
    assert b"request_headers" not in serialized
    assert b"request_query" not in serialized
    assert b'"provider"' not in serialized
    assert b'"retrieved_at"' not in serialized
    assert sanitized_checksum(first, secret_values=(SECRET,)) == hashlib.sha256(serialized).hexdigest()
    assert sanitized_checksum(first, secret_values=(SECRET,)) == sanitized_checksum(
        second, secret_values=(SECRET,)
    )


def test_live_odds_shape_preserves_bookmaker_and_market_identifier_keys() -> None:
    payload = json.loads(
        (FIXTURE_DIR / "odds_live_provider_shape_identifier_keys.json").read_text(
            encoding="utf-8"
        )
    )
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=payload,
        retrieved_at="2026-07-12T21:12:36+00:00",
        provider_timestamp=None,
        content_type="application/json",
    )

    retained = json.loads(sanitized_json_bytes(capture, secret_values=(SECRET,)))

    bookmaker = retained[0]["bookmakers"][0]
    assert bookmaker["key"] == "fanduel"
    assert bookmaker["markets"][0]["key"] == "h2h"


def test_capture_defaults_missing_content_type_without_storing_request_metadata() -> None:
    capture = capture_response(
        provider="openweather",
        endpoint_category="one_call",
        payload={"hourly": []},
        response=FakeResponse({}),
    )

    assert capture.content_type == "application/json"
    assert set(capture.__slots__) == {
        "provider",
        "endpoint_category",
        "payload",
        "retrieved_at",
        "provider_timestamp",
        "content_type",
        "event_id",
    }


def test_odds_collector_returns_quota_and_raw_capture() -> None:
    games: list[dict[str, Any]] = [
        {
            "id": "game-1",
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-11T19:00:00Z",
            "home_team": "Los Angeles Dodgers",
            "away_team": "San Francisco Giants",
            "bookmakers": [],
        }
    ]
    response = FakeResponse(
        {
            "Content-Type": "application/json; charset=utf-8",
            "Date": "Sat, 11 Jul 2026 12:00:00 GMT",
            "x-requests-remaining": "499",
            "x-requests-used": "1",
            "x-requests-last": "1",
        }
    )
    collector = OddsCollector(
        Settings(odds_api_key=SECRET),
        as_http_client(FakeHttp([(games, response)])),
    )

    result = collector.collect()
    returned_games, quota, capture = result

    assert returned_games == games
    assert quota["requests_remaining"] == "499"
    assert capture.provider == "the_odds_api"
    assert capture.endpoint_category == "mlb_odds"
    assert capture.payload == games
    assert capture.provider_timestamp is None
    assert result.request.response_date_utc == "2026-07-11T12:00:00+00:00"
    assert capture.content_type == "application/json; charset=utf-8"
    assert capture.event_id is None
    assert datetime.fromisoformat(capture.retrieved_at).tzinfo == timezone.utc


def test_nws_collector_returns_separate_point_and_forecast_captures() -> None:
    point = {
        "properties": {
            "forecastHourly": "https://api.weather.gov/gridpoints/LOX/1,2/forecast/hourly",
            "cwa": "LOX",
        }
    }
    forecast = {
        "properties": {
            "updated": "2026-07-11T11:45:00Z",
            "periods": [
                {
                    "startTime": "2026-07-11T13:00:00+00:00",
                    "temperature": 71,
                    "temperatureUnit": "F",
                    "relativeHumidity": {"value": 55},
                    "probabilityOfPrecipitation": {"value": 5},
                    "windSpeed": "10 mph",
                    "windDirection": "W",
                    "shortForecast": "Clear",
                }
            ],
        }
    }
    fake = FakeHttp(
        [
            (
                point,
                FakeResponse(
                    {
                        "content-type": "application/geo+json",
                        "date": "Sat, 11 Jul 2026 11:50:00 GMT",
                    }
                ),
            ),
            (
                forecast,
                FakeResponse(
                    {
                        "Content-Type": "application/geo+json",
                        "Date": "Sat, 11 Jul 2026 11:55:00 GMT",
                    }
                ),
            ),
        ]
    )
    collector = NwsWeatherCollector(as_http_client(fake), "Example/1.0 (owner@example.test)")

    normalized, point_capture, forecast_capture = collector.collect(
        34.0,
        -118.0,
        datetime(2026, 7, 11, 13, tzinfo=timezone.utc),
        event_id="game-1",
    )

    assert normalized["forecast_time"] == "2026-07-11T13:00:00+00:00"
    assert point_capture.endpoint_category == "point_lookup"
    assert point_capture.payload == point
    assert point_capture.provider_timestamp == "2026-07-11T11:50:00+00:00"
    assert forecast_capture.endpoint_category == "hourly_forecast"
    assert forecast_capture.payload == forecast
    assert forecast_capture.provider_timestamp == "2026-07-11T11:45:00+00:00"
    assert point_capture.event_id == forecast_capture.event_id == "game-1"
    assert point_capture.content_type == forecast_capture.content_type == "application/geo+json"


def test_openweather_collector_returns_one_call_capture() -> None:
    payload = {
        "hourly": [
            {
                "dt": 1783774800,
                "temp": 75,
                "humidity": 40,
                "pop": 0.2,
                "wind_speed": 8,
                "wind_deg": 225,
                "weather": [{"description": "clear sky"}],
                "clouds": 5,
                "pressure": 1012,
            }
        ]
    }
    response = FakeResponse(
        {
            "Content-Type": "application/json",
            "Date": "Sat, 11 Jul 2026 12:30:00 GMT",
        }
    )
    collector = OpenWeatherCollector(
        as_http_client(FakeHttp([(payload, response)])),
        SECRET,
    )

    normalized, capture = collector.collect(
        34.0,
        -118.0,
        datetime.fromtimestamp(1783774800, timezone.utc),
        event_id="game-1",
    )

    assert normalized["precipitation_probability_pct"] == 20.0
    assert capture.provider == "openweather"
    assert capture.endpoint_category == "one_call"
    assert capture.payload == payload
    assert capture.provider_timestamp == "2026-07-11T12:30:00+00:00"
    assert capture.content_type == "application/json"
    assert capture.event_id == "game-1"


def test_hybrid_raw_persistence_writes_sanitized_file_and_sqlite_metadata(
    tmp_path: Path,
) -> None:
    secret = "raw-persistence-secret-not-real"
    settings = Settings(
        database_path=tmp_path / "raw.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token="test-service-secret-not-real",
        odds_api_key=secret,
        openweather_enabled=False,
    )
    requested_date = date(2026, 7, 11)
    run_id = generate_run_id(requested_date)
    database = Database(settings.database_path)
    database.create_run(run_id, requested_date)
    artifacts = ArtifactPaths(settings.artifact_dir, requested_date, run_id)
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload={"games": [], "apiKey": secret, "message": secret},
        retrieved_at="2026-07-11T12:00:00+00:00",
        provider_timestamp="2026-07-11T11:59:00+00:00",
        content_type="application/json",
    )

    _persist_raw_capture(
        capture,
        run_id=run_id,
        settings=settings,
        database=database,
        artifacts=artifacts,
    )

    with database.connect() as connection:
        row = connection.execute("SELECT * FROM raw_provider_payloads").fetchone()
    assert row is not None
    payload_bytes = (artifacts.root / row["artifact_relpath"]).read_bytes()
    assert hashlib.sha256(payload_bytes).hexdigest() == row["checksum_sha256"]
    assert secret.encode() not in payload_bytes
    assert row["provider"] == "the_odds_api"
    assert row["endpoint_category"] == "mlb_odds"
    assert "?" not in row["endpoint_category"]
