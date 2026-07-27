from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.collectors.odds_collector import OddsCollectionResult
from app.odds_weather.contracts import (
    OddsProviderEventV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
)
from app.raw_payloads import RawPayloadCapture, sanitized_checksum


class OddsWeatherAdapterError(ValueError):
    """Raised when retained collector output cannot enter the phase-4 contract."""


@dataclass(frozen=True, slots=True)
class OddsCollectionAdapterResultV1:
    events: tuple[OddsProviderEventV1, ...]
    warnings: tuple[OddsWeatherWarningV1, ...]


def _capture_time(capture: RawPayloadCapture) -> datetime:
    try:
        parsed = datetime.fromisoformat(capture.retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsWeatherAdapterError("raw capture retrieved_at is malformed") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OddsWeatherAdapterError("raw capture retrieved_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _require_capture(
    capture: RawPayloadCapture,
    *,
    provider: str,
    endpoint_category: str,
) -> None:
    if capture.provider != provider or capture.endpoint_category != endpoint_category:
        raise OddsWeatherAdapterError(
            "raw capture provider/endpoint does not match the requested phase-4 adapter"
        )


def odds_collection_to_phase4(
    collection: OddsCollectionResult,
    *,
    secret_values: Iterable[str] = (),
    history_rows_by_event: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> OddsCollectionAdapterResultV1:
    """Convert the existing validated OddsCollector result into canonical phase-4 inputs."""

    _require_capture(
        collection.capture,
        provider="the_odds_api",
        endpoint_category="mlb_odds",
    )
    retrieved_at = _capture_time(collection.capture)
    checksum = sanitized_checksum(collection.capture, secret_values=secret_values)
    history = history_rows_by_event or {}
    events: list[OddsProviderEventV1] = []
    for index, event in enumerate(collection.games):
        provider_event_id = str(event.get("id") or "").strip()
        if not provider_event_id:
            raise OddsWeatherAdapterError(
                f"validated OddsCollector game {index} lacks provider event id"
            )
        rows = tuple(dict(row) for row in history.get(provider_event_id, ()))
        events.append(
            OddsProviderEventV1(
                provider_event_id=provider_event_id,
                retrieved_at=retrieved_at,
                raw_capture_checksum=checksum,
                event=event,
                history_rows=rows,
            )
        )

    warnings = tuple(
        OddsWeatherWarningV1(
            code=warning.code,
            domain=OddsWeatherWarningDomain.ODDS,
            message=warning.message,
            provider=warning.bookmaker or "the_odds_api",
            provider_event_id=warning.event_id,
        )
        for warning in collection.warnings
    )
    return OddsCollectionAdapterResultV1(
        events=tuple(events),
        warnings=warnings,
    )


def nws_forecast_to_phase4(
    *,
    source_game_id: str,
    forecast: Mapping[str, Any],
    point_capture: RawPayloadCapture,
    forecast_capture: RawPayloadCapture,
    secret_values: Iterable[str] = (),
) -> WeatherForecastEvidenceV1:
    """Convert existing NWS point/hourly collector output into canonical phase-4 input."""

    _require_capture(point_capture, provider="nws", endpoint_category="point_lookup")
    _require_capture(
        forecast_capture,
        provider="nws",
        endpoint_category="hourly_forecast",
    )
    retrieved_at = max(_capture_time(point_capture), _capture_time(forecast_capture))
    return WeatherForecastEvidenceV1(
        source_game_id=source_game_id,
        provider=WeatherProvider.NWS,
        retrieved_at=retrieved_at,
        raw_capture_checksums=(
            sanitized_checksum(point_capture, secret_values=secret_values),
            sanitized_checksum(forecast_capture, secret_values=secret_values),
        ),
        forecast=dict(forecast),
    )


def openweather_forecast_to_phase4(
    *,
    source_game_id: str,
    forecast: Mapping[str, Any],
    capture: RawPayloadCapture,
    secret_values: Iterable[str] = (),
) -> WeatherForecastEvidenceV1:
    """Convert existing OpenWeather One Call 3.0 output into canonical phase-4 input."""

    _require_capture(
        capture,
        provider="openweather",
        endpoint_category="one_call",
    )
    return WeatherForecastEvidenceV1(
        source_game_id=source_game_id,
        provider=WeatherProvider.OPENWEATHER,
        retrieved_at=_capture_time(capture),
        raw_capture_checksums=(
            sanitized_checksum(capture, secret_values=secret_values),
        ),
        forecast=dict(forecast),
    )
