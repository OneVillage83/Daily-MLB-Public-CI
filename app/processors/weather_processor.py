from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Mapping

MAX_COMPARISON_TIME_SKEW_SECONDS = 60 * 60


def angular_difference(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _forecast_time(value: Mapping[str, Any]) -> datetime | None:
    raw = value.get("forecast_time")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def wind_impact(
    wind_from_deg: float | None,
    speed_mph: float | None,
    outfield_bearing_deg: float | None,
    *,
    bearing_verification_state: str | None = None,
) -> dict[str, Any]:
    """Calculate field-relative wind only from an explicitly verified bearing."""
    unavailable = {
        "classification": "unknown",
        "outfield_component_mph": None,
        "crosswind_component_mph": None,
        "wind_to_degrees": None,
    }
    verification_state = str(bearing_verification_state or "").strip().upper()
    if verification_state == "UNVERIFIED":
        return {**unavailable, "reason_code": "outfield_bearing_unverified"}
    if verification_state != "VERIFIED":
        return {**unavailable, "reason_code": "outfield_bearing_unknown"}
    if outfield_bearing_deg is None or not math.isfinite(outfield_bearing_deg):
        return {**unavailable, "reason_code": "outfield_bearing_invalid"}
    if not 0.0 <= outfield_bearing_deg < 360.0:
        return {**unavailable, "reason_code": "outfield_bearing_invalid"}
    if wind_from_deg is None:
        return {**unavailable, "reason_code": "wind_direction_unknown"}
    if not math.isfinite(wind_from_deg) or not 0.0 <= wind_from_deg < 360.0:
        return {**unavailable, "reason_code": "wind_direction_invalid"}
    if speed_mph is None:
        return {**unavailable, "reason_code": "wind_speed_unknown"}
    if not math.isfinite(speed_mph) or speed_mph < 0.0:
        return {**unavailable, "reason_code": "wind_speed_invalid"}
    # Meteorological wind direction is where wind comes FROM. Convert to where it blows TO.
    wind_to = (wind_from_deg + 180.0) % 360.0
    diff = math.radians(angular_difference(wind_to, outfield_bearing_deg))
    out_component = speed_mph * math.cos(diff)
    cross_component = speed_mph * math.sin(diff)
    if abs(out_component) < 3:
        label = "mostly_crosswind"
    elif out_component > 0:
        label = "blowing_out"
    else:
        label = "blowing_in"
    return {
        "classification": label,
        "outfield_component_mph": round(out_component, 2),
        "crosswind_component_mph": round(cross_component, 2),
        "wind_to_degrees": round(wind_to, 1),
        "reason_code": None,
    }


def compare(
    nws: dict[str, Any] | None, owm: dict[str, Any] | None
) -> dict[str, Any]:
    if not nws or not owm:
        return {"agreement": "single_source"}

    nws_time = _forecast_time(nws)
    owm_time = _forecast_time(owm)
    if nws_time is None or owm_time is None:
        return {
            "agreement": "not_comparable_timestamp_missing",
            "forecast_time_difference_minutes": None,
        }
    time_skew_seconds = abs((nws_time - owm_time).total_seconds())
    time_skew_minutes = round(time_skew_seconds / 60.0, 2)
    if time_skew_seconds > MAX_COMPARISON_TIME_SKEW_SECONDS:
        return {
            "agreement": "not_comparable_time_skew",
            "forecast_time_difference_minutes": time_skew_minutes,
        }

    def delta(key: str) -> float | None:
        first = _finite_number(nws.get(key))
        second = _finite_number(owm.get(key))
        return round(abs(first - second), 2) if first is not None and second is not None else None

    temp = delta("temperature_f")
    rain = delta("precipitation_probability_pct")
    wind = delta("wind_speed_mph")
    score = 0
    if temp is not None and temp > 5:
        score += 1
    if rain is not None and rain > 25:
        score += 1
    if wind is not None and wind > 6:
        score += 1
    agreement = "strong" if score == 0 else "moderate" if score == 1 else "weak"
    return {
        "agreement": agreement,
        "forecast_time_difference_minutes": time_skew_minutes,
        "temperature_difference_f": temp,
        "rain_probability_difference_points": rain,
        "wind_speed_difference_mph": wind,
    }
