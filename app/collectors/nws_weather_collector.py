from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Mapping
from urllib.parse import urlparse

from app.http import HttpClient
from app.raw_payloads import RawPayloadCapture, capture_response

CARDINAL_DEGREES = {
    "N": 0,
    "NNE": 22.5,
    "NE": 45,
    "ENE": 67.5,
    "E": 90,
    "ESE": 112.5,
    "SE": 135,
    "SSE": 157.5,
    "S": 180,
    "SSW": 202.5,
    "SW": 225,
    "WSW": 247.5,
    "W": 270,
    "WNW": 292.5,
    "NW": 315,
    "NNW": 337.5,
}
MAX_GAME_FORECAST_OFFSET_SECONDS = 60 * 60
_NWS_API_HOST = "api.weather.gov"
_NWS_HOURLY_PATH = re.compile(
    r"^/gridpoints/[A-Z0-9]{3}/[0-9]+,[0-9]+/forecast/hourly$"
)


def parse_wind_speed(text: str | None) -> float | None:
    if not text:
        return None
    numbers = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", text)]
    if not numbers:
        return None
    return sum(numbers[:2]) / min(len(numbers), 2)


def _aware_datetime(value: object, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"NWS hourly forecast period is missing {field}")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"NWS hourly forecast period has invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"NWS hourly forecast period {field} must be timezone-aware")
    return parsed


def _quantity_value(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    raw = value.get("value")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    numeric = float(raw)
    return numeric if math.isfinite(numeric) else None


def _validated_hourly_forecast_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("NWS point lookup is missing forecastHourly")
    url = value.strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != _NWS_API_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or _NWS_HOURLY_PATH.fullmatch(parsed.path) is None
    ):
        raise RuntimeError("NWS point lookup returned an untrusted forecastHourly URL")
    return url


class NwsWeatherCollector:
    def __init__(self, http: HttpClient, user_agent: str):
        self.http = http
        self.headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}

    def _nearest_period(
        self, periods: list[dict[str, Any]], game_time: datetime
    ) -> dict[str, Any]:
        if game_time.tzinfo is None or game_time.utcoffset() is None:
            raise ValueError("game_time must be timezone-aware")

        def distance(period: dict[str, Any]) -> float:
            start = _aware_datetime(period.get("startTime"), field="startTime")
            return abs((start - game_time.astimezone(start.tzinfo)).total_seconds())

        period = min(periods, key=distance)
        offset_seconds = distance(period)
        if offset_seconds > MAX_GAME_FORECAST_OFFSET_SECONDS:
            raise RuntimeError(
                "NWS hourly forecast does not cover first pitch within 60 minutes"
            )
        return period

    def collect(
        self,
        latitude: float,
        longitude: float,
        game_time: datetime,
        *,
        event_id: str | None = None,
    ) -> tuple[dict[str, Any], RawPayloadCapture, RawPayloadCapture]:
        point_url = f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}"
        point, point_response = self.http.get_json(point_url, headers=self.headers)
        if not isinstance(point, dict):
            raise RuntimeError("NWS point lookup returned a non-object response")
        point_capture = capture_response(
            provider="nws",
            endpoint_category="point_lookup",
            payload=point,
            response=point_response,
            event_id=event_id,
        )
        point_properties = point.get("properties")
        if not isinstance(point_properties, dict):
            raise RuntimeError("NWS point lookup is missing properties")
        forecast_url = _validated_hourly_forecast_url(
            point_properties.get("forecastHourly")
        )
        forecast, forecast_response = self.http.get_json(
            forecast_url, headers=self.headers
        )
        if not isinstance(forecast, dict):
            raise RuntimeError("NWS hourly forecast returned a non-object response")
        forecast_properties = forecast.get("properties")
        if not isinstance(forecast_properties, dict):
            raise RuntimeError("NWS hourly forecast is missing properties")
        forecast_capture = capture_response(
            provider="nws",
            endpoint_category="hourly_forecast",
            payload=forecast,
            response=forecast_response,
            provider_timestamp=forecast_properties.get("updated"),
            event_id=event_id,
        )
        raw_periods = forecast_properties.get("periods")
        if not isinstance(raw_periods, list):
            raise RuntimeError("NWS hourly forecast is missing periods")
        periods = [period for period in raw_periods if isinstance(period, dict)]
        if not periods:
            raise RuntimeError("NWS hourly forecast contains no valid periods")
        period = self._nearest_period(periods, game_time)
        period_start = _aware_datetime(period.get("startTime"), field="startTime")
        offset_minutes = abs(
            (period_start - game_time.astimezone(period_start.tzinfo)).total_seconds()
        ) / 60.0
        direction_value = period.get("windDirection")
        direction = (
            direction_value.strip().upper()
            if isinstance(direction_value, str) and direction_value.strip()
            else None
        )
        temperature = period.get("temperature")
        normalized = {
            "forecast_time": period_start.isoformat(),
            "forecast_offset_minutes": round(offset_minutes, 2),
            "forecast_generated_at": forecast_capture.provider_timestamp,
            "temperature_f": (
                temperature
                if period.get("temperatureUnit") == "F"
                and isinstance(temperature, int | float)
                and not isinstance(temperature, bool)
                else None
            ),
            "humidity_pct": _quantity_value(period.get("relativeHumidity")),
            "precipitation_probability_pct": _quantity_value(
                period.get("probabilityOfPrecipitation")
            ),
            "wind_speed_mph": parse_wind_speed(period.get("windSpeed")),
            "wind_direction_deg": (
                CARDINAL_DEGREES.get(direction) if direction is not None else None
            ),
            "short_forecast": period.get("shortForecast"),
            "wind_speed_text": period.get("windSpeed"),
            "wind_direction_cardinal": direction,
            "forecast_office": point_properties.get("cwa"),
        }
        return normalized, point_capture, forecast_capture
