from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping

from app.http import HttpClient
from app.raw_payloads import RawPayloadCapture, capture_response

MAX_GAME_FORECAST_OFFSET_SECONDS = 60 * 60
SUPPORTED_OPENWEATHER_API_VERSION = "3.0"


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RuntimeError(f"OpenWeather hourly forecast has invalid {field}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise RuntimeError(f"OpenWeather hourly forecast has invalid {field}")
    return numeric


class OpenWeatherCollector:
    def __init__(self, http: HttpClient, api_key: str, version: str = "3.0"):
        if not api_key.strip():
            raise ValueError("OpenWeather API key must not be empty")
        if version.strip() != SUPPORTED_OPENWEATHER_API_VERSION:
            raise ValueError(
                "OpenWeather collector supports only One Call API version 3.0"
            )
        self.http = http
        self.api_key = api_key
        self.version = version.strip()

    def _nearest_hour(
        self, hourly: list[dict[str, Any]], game_time: datetime
    ) -> dict[str, Any]:
        if game_time.tzinfo is None or game_time.utcoffset() is None:
            raise ValueError("game_time must be timezone-aware")
        if not hourly:
            raise RuntimeError("OpenWeather response contains no hourly forecasts")
        target = game_time.astimezone(timezone.utc).timestamp()

        def distance(hour: dict[str, Any]) -> float:
            return abs(_finite_number(hour.get("dt"), field="dt") - target)

        nearest = min(hourly, key=distance)
        offset_seconds = distance(nearest)
        if offset_seconds > MAX_GAME_FORECAST_OFFSET_SECONDS:
            raise RuntimeError(
                "OpenWeather hourly forecast does not cover first pitch within 60 minutes"
            )
        return nearest

    def collect(
        self,
        latitude: float,
        longitude: float,
        game_time: datetime,
        *,
        event_id: str | None = None,
    ) -> tuple[dict[str, Any], RawPayloadCapture]:
        if not -90.0 <= float(latitude) <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= float(longitude) <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        url = f"https://api.openweathermap.org/data/{self.version}/onecall"
        params = {
            "lat": latitude,
            "lon": longitude,
            "appid": self.api_key,
            "units": "imperial",
            "exclude": "minutely,daily,alerts",
        }
        payload, response = self.http.get_json(url, params=params)
        if not isinstance(payload, dict):
            raise RuntimeError("OpenWeather returned a non-object response")
        raw_hourly = payload.get("hourly")
        if not isinstance(raw_hourly, list):
            raise RuntimeError("OpenWeather response is missing hourly forecasts")
        if not all(isinstance(item, dict) for item in raw_hourly):
            raise RuntimeError("OpenWeather hourly forecast contains a non-object entry")
        hourly = list(raw_hourly)
        hour = self._nearest_hour(hourly, game_time)
        weather_items = hour.get("weather")
        if weather_items is None:
            weather: Mapping[str, Any] = {}
        elif (
            isinstance(weather_items, list)
            and weather_items
            and isinstance(weather_items[0], Mapping)
        ):
            weather = weather_items[0]
        else:
            raise RuntimeError("OpenWeather hourly forecast has invalid weather details")
        pop_value = hour.get("pop")
        if pop_value is None:
            precipitation_probability = None
        else:
            pop = _finite_number(pop_value, field="pop")
            if not 0.0 <= pop <= 1.0:
                raise RuntimeError("OpenWeather hourly forecast pop must be between 0 and 1")
            precipitation_probability = pop * 100.0
        forecast_timestamp = _finite_number(hour.get("dt"), field="dt")
        forecast_time = datetime.fromtimestamp(forecast_timestamp, timezone.utc)
        game_utc = game_time.astimezone(timezone.utc)
        offset_minutes = abs((forecast_time - game_utc).total_seconds()) / 60.0
        capture = capture_response(
            provider="openweather",
            endpoint_category="one_call",
            payload=payload,
            response=response,
            event_id=event_id,
        )
        normalized = {
            "forecast_time": forecast_time.isoformat(),
            "forecast_offset_minutes": round(offset_minutes, 2),
            "forecast_generated_at": capture.provider_timestamp,
            "temperature_f": hour.get("temp"),
            "humidity_pct": hour.get("humidity"),
            "precipitation_probability_pct": precipitation_probability,
            "wind_speed_mph": hour.get("wind_speed"),
            "wind_direction_deg": hour.get("wind_deg"),
            "wind_gust_mph": hour.get("wind_gust"),
            "short_forecast": weather.get("description"),
            "clouds_pct": hour.get("clouds"),
            "pressure_hpa": hour.get("pressure"),
        }
        return normalized, capture
