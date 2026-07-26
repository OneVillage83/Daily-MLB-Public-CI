from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.collectors.nws_weather_collector import NwsWeatherCollector
from app.collectors.openweather_collector import OpenWeatherCollector
from app.config import DEFAULT_NWS_USER_AGENT, Settings
from app.processors.weather_processor import compare, wind_impact


@dataclass
class _Response:
    headers: dict[str, str]


class _FakeHttp:
    def __init__(self, responses: list[tuple[object, _Response]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, url: str, **kwargs: Any) -> tuple[object, _Response]:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _response() -> _Response:
    return _Response(
        {
            "Content-Type": "application/geo+json",
            "Date": "Wed, 22 Jul 2026 18:00:00 GMT",
        }
    )


def test_collection_rejects_placeholder_nws_identity() -> None:
    configured = Settings(
        service_auth_token="service-token",
        odds_api_key="odds-key",
        nws_user_agent=DEFAULT_NWS_USER_AGENT,
        openweather_enabled=False,
    )

    with pytest.raises(RuntimeError, match="NWS_USER_AGENT"):
        configured.validate_for_collection()


def test_collection_rejects_unvalidated_openweather_version() -> None:
    configured = Settings(
        service_auth_token="service-token",
        odds_api_key="odds-key",
        nws_user_agent="Daily-MLB/1.0 (ops@onevillage.example)",
        openweather_enabled=True,
        openweather_api_key="weather-key",
        openweather_api_version="4.0",
    )

    with pytest.raises(RuntimeError, match="OPENWEATHER_API_VERSION"):
        configured.validate_for_collection()


def test_nws_rejects_untrusted_linked_forecast_origin() -> None:
    http = _FakeHttp(
        [
            (
                {"properties": {"forecastHourly": "https://example.test/hourly"}},
                _response(),
            )
        ]
    )
    collector = NwsWeatherCollector(http, "Daily-MLB/1.0 (ops@example.org)")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="untrusted forecastHourly URL"):
        collector.collect(
            38.5804,
            -121.5138,
            datetime(2026, 7, 22, 19, 10, tzinfo=timezone(timedelta(hours=-7))),
        )


def test_nws_rejects_hourly_period_too_far_from_first_pitch() -> None:
    game_time = datetime(2026, 7, 22, 19, 10, tzinfo=timezone(timedelta(hours=-7)))
    http = _FakeHttp(
        [
            (
                {
                    "properties": {
                        "forecastHourly": (
                            "https://api.weather.gov/gridpoints/STO/10,20/forecast/hourly"
                        ),
                        "cwa": "STO",
                    }
                },
                _response(),
            ),
            (
                {
                    "properties": {
                        "updated": "2026-07-22T18:00:00+00:00",
                        "periods": [
                            {
                                "startTime": "2026-07-22T14:00:00-07:00",
                                "temperature": 90,
                                "temperatureUnit": "F",
                                "windSpeed": "5 mph",
                                "windDirection": "W",
                            }
                        ],
                    }
                },
                _response(),
            ),
        ]
    )
    collector = NwsWeatherCollector(http, "Daily-MLB/1.0 (ops@example.org)")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="does not cover first pitch"):
        collector.collect(38.5804, -121.5138, game_time)


def test_nws_records_selected_forecast_offset() -> None:
    game_time = datetime(2026, 7, 22, 19, 10, tzinfo=timezone(timedelta(hours=-7)))
    http = _FakeHttp(
        [
            (
                {
                    "properties": {
                        "forecastHourly": (
                            "https://api.weather.gov/gridpoints/STO/10,20/forecast/hourly"
                        ),
                        "cwa": "STO",
                    }
                },
                _response(),
            ),
            (
                {
                    "properties": {
                        "updated": "2026-07-22T18:00:00+00:00",
                        "periods": [
                            {
                                "startTime": "2026-07-22T19:00:00-07:00",
                                "temperature": 88,
                                "temperatureUnit": "F",
                                "relativeHumidity": {"value": 31},
                                "probabilityOfPrecipitation": {"value": 2},
                                "windSpeed": "5 to 10 mph",
                                "windDirection": "WSW",
                                "shortForecast": "Clear",
                            }
                        ],
                    }
                },
                _response(),
            ),
        ]
    )
    collector = NwsWeatherCollector(http, "Daily-MLB/1.0 (ops@example.org)")  # type: ignore[arg-type]

    row, _, _ = collector.collect(38.5804, -121.5138, game_time)

    assert row["forecast_offset_minutes"] == 10.0
    assert row["wind_speed_mph"] == 7.5
    assert row["wind_direction_deg"] == 247.5


def test_openweather_rejects_game_outside_hourly_horizon() -> None:
    base_time = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    http = _FakeHttp(
        [
            (
                {
                    "hourly": [
                        {
                            "dt": int(base_time.timestamp()),
                            "temp": 80,
                            "humidity": 30,
                            "pop": 0.0,
                            "wind_speed": 5,
                            "wind_deg": 180,
                            "weather": [{"description": "clear"}],
                        }
                    ]
                },
                _response(),
            )
        ]
    )
    collector = OpenWeatherCollector(http, "weather-key")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="does not cover first pitch"):
        collector.collect(38.5804, -121.5138, base_time + timedelta(hours=4))


def test_openweather_rejects_invalid_precipitation_probability() -> None:
    game_time = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)
    http = _FakeHttp(
        [
            (
                {
                    "hourly": [
                        {
                            "dt": int(game_time.timestamp()),
                            "temp": 80,
                            "humidity": 30,
                            "pop": 1.5,
                            "wind_speed": 5,
                            "wind_deg": 180,
                            "weather": [{"description": "clear"}],
                        }
                    ]
                },
                _response(),
            )
        ]
    )
    collector = OpenWeatherCollector(http, "weather-key")  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="pop must be between 0 and 1"):
        collector.collect(38.5804, -121.5138, game_time)


def test_weather_comparison_rejects_misaligned_forecast_hours() -> None:
    result = compare(
        {
            "forecast_time": "2026-07-22T19:00:00-07:00",
            "temperature_f": 80,
        },
        {
            "forecast_time": "2026-07-22T21:00:00-07:00",
            "temperature_f": 80,
        },
    )

    assert result == {
        "agreement": "not_comparable_time_skew",
        "forecast_time_difference_minutes": 120.0,
    }


def test_weather_comparison_tolerates_invalid_optional_values() -> None:
    result = compare(
        {
            "forecast_time": "2026-07-22T19:00:00-07:00",
            "temperature_f": "not-a-number",
            "wind_speed_mph": 5,
        },
        {
            "forecast_time": "2026-07-22T19:00:00-07:00",
            "temperature_f": 80,
            "wind_speed_mph": 6,
        },
    )

    assert result["agreement"] == "strong"
    assert result["temperature_difference_f"] is None
    assert result["wind_speed_difference_mph"] == 1.0


def test_wind_impact_rejects_invalid_provider_values() -> None:
    direction = wind_impact(
        360,
        10,
        0,
        bearing_verification_state="VERIFIED",
    )
    speed = wind_impact(
        180,
        -1,
        0,
        bearing_verification_state="VERIFIED",
    )

    assert direction["reason_code"] == "wind_direction_invalid"
    assert speed["reason_code"] == "wind_speed_invalid"
