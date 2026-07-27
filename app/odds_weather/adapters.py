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
from app.odds_weather.history import (
    OddsHistorySelectionError,
    OddsHistorySource,
    select_odds_history_at,
)
from app.raw_payloads import RawPayloadCapture, sanitized_checksum


class OddsWeatherAdapterError(ValueError):
    """Raised when retained collector output cannot enter the phase-4 contract."""


@dataclass(frozen=True, slots=True)
class OddsCollectionAdapterResultV1:
    events: tuple[OddsProviderEventV1, ...]
    warnings: tuple[OddsWeatherWarningV1, ...]


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OddsWeatherAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _capture_time(capture: RawPayloadCapture) -> datetime:
    try:
        parsed = datetime.fromisoformat(capture.retrieved_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsWeatherAdapterError("raw capture retrieved_at is malformed") from exc
    return _aware_utc(parsed, "raw capture retrieved_at")


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
    history_source: OddsHistorySource | None = None,
    history_observed_at: datetime | None = None,
) -> OddsCollectionAdapterResultV1:
    """Convert the validated OddsCollector result into canonical phase-4 inputs.

    Callers may supply already-selected ``history_rows_by_event`` or a retained history
    source. When a history source is supplied, rows are filtered at the explicit phase
    cutoff (or the collector retrieval time by default) before line movement is computed.
    """

    if history_rows_by_event is not None and history_source is not None:
        raise OddsWeatherAdapterError(
            "provide either history_rows_by_event or history_source, not both"
        )
    if history_observed_at is not None and history_source is None:
        raise OddsWeatherAdapterError(
            "history_observed_at requires history_source"
        )
    _require_capture(
        collection.capture,
        provider="the_odds_api",
        endpoint_category="mlb_odds",
    )
    retrieved_at = _capture_time(collection.capture)
    history_cutoff = (
        retrieved_at
        if history_observed_at is None
        else _aware_utc(history_observed_at, "history_observed_at")
    )
    checksum = sanitized_checksum(collection.capture, secret_values=secret_values)
    provided_history = history_rows_by_event or {}
    events: list[OddsProviderEventV1] = []
    for index, event in enumerate(collection.games):
        provider_event_id = str(event.get("id") or "").strip()
        if not provider_event_id:
            raise OddsWeatherAdapterError(
                f"validated OddsCollector game {index} lacks provider event id"
            )
        if history_source is not None:
            try:
                rows = select_odds_history_at(
                    history_source,
                    provider_event_id=provider_event_id,
                    observed_at=history_cutoff,
                )
            except OddsHistorySelectionError as exc:
                raise OddsWeatherAdapterError(
                    f"retained odds history selection failed for {provider_event_id}: {exc}"
                ) from exc
        else:
            rows = tuple(
                dict(row) for row in provided_history.get(provider_event_id, ())
            )
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
