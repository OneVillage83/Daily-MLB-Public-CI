from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.collectors.odds_collector import OddsCollectionResult, OddsCollectorWarning
from app.http import HttpRequestDiagnostics
from app.odds_weather.adapters import (
    OddsWeatherAdapterError,
    odds_collection_to_phase4,
    openweather_forecast_to_phase4,
)
from app.odds_weather.contracts import (
    OddsProviderEventV1,
    OddsWeatherContractError,
)
from app.raw_payloads import RawPayloadCapture, sanitized_json_bytes
from app.redaction import REDACTED, redact_value

RETRIEVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
CHECKSUM = "a" * 64
CONFIGURED_SECRET = "configured-odds-secret-value"


def _event() -> dict[str, Any]:
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


def _history_row() -> dict[str, Any]:
    return {
        "event_id": "event-1",
        "bookmaker_key": "book-a",
        "market_key": "h2h",
        "outcome_name": "Los Angeles Dodgers",
        "price": -125,
        "point": None,
        "provider_last_update": "2026-07-27T13:00:00+00:00",
        "retrieved_at": "2026-07-27T13:01:00+00:00",
    }


def _evidence(
    event: dict[str, Any] | None = None,
    *,
    history_rows: tuple[dict[str, Any], ...] = (),
    secret_values: tuple[str, ...] = (),
) -> OddsProviderEventV1:
    return OddsProviderEventV1(
        provider_event_id="event-1",
        retrieved_at=RETRIEVED,
        raw_capture_checksum=CHECKSUM,
        event=_event() if event is None else event,
        history_rows=history_rows,
        secret_values=secret_values,
    )


def _collection(
    event: dict[str, Any],
    *,
    warnings: tuple[OddsCollectorWarning, ...] = (),
) -> OddsCollectionResult:
    return OddsCollectionResult(
        games=[event],
        quota={
            "requests_remaining": "99",
            "requests_used": "1",
            "requests_last": "1",
        },
        capture=RawPayloadCapture(
            provider="the_odds_api",
            endpoint_category="mlb_odds",
            payload=[event],
            retrieved_at=RETRIEVED.isoformat(),
            provider_timestamp=None,
            content_type="application/json",
        ),
        request=HttpRequestDiagnostics(
            request_status="success",
            status_code=200,
            attempts=1,
            retries_performed=0,
            duration_seconds=0.1,
            response_date_utc=RETRIEVED.isoformat(),
        ),
        warnings=warnings,
    )


def _forecast() -> dict[str, Any]:
    return {
        "forecast_time": "2026-07-27T23:10:00+00:00",
        "forecast_offset_minutes": 0.0,
        "temperature_f": 72.0,
        "humidity_pct": 45.0,
        "precipitation_probability_pct": 10.0,
        "wind_speed_mph": 8.0,
        "wind_direction_deg": 270.0,
        "short_forecast": "Clear",
    }


def test_semantic_bookmaker_and_market_keys_survive_canonical_json_exactly() -> None:
    evidence = _evidence(history_rows=(_history_row(),))

    payload = evidence.as_dict()
    canonical = json.loads(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )

    assert canonical["event"]["bookmakers"][0]["key"] == "book-a"
    assert canonical["event"]["bookmakers"][0]["markets"][0]["key"] == "h2h"
    assert canonical["history_rows"][0]["bookmaker_key"] == "book-a"
    assert canonical["history_rows"][0]["market_key"] == "h2h"


@pytest.mark.parametrize(
    "path",
    ("top_level", "unknown_nested"),
)
def test_unrelated_generic_key_is_rejected(path: str) -> None:
    event = _event()
    if path == "top_level":
        event["key"] = "not-a-semantic-identifier"
    else:
        event["bookmakers"][0]["markets"][0]["outcomes"][0]["key"] = "unsafe"

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        _evidence(event)


@pytest.mark.parametrize(
    "field_name",
    (
        "api_key",
        "apiKey",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
        "bearer",
        "password",
        "passwd",
        "client_secret",
        "secret",
        "credential",
        "credentials",
        "signature",
        "sig",
        "service_auth_token",
    ),
)
def test_sensitive_field_names_are_rejected_at_any_depth(field_name: str) -> None:
    event = _event()
    event["bookmakers"][0]["markets"][0]["outcomes"][0][field_name] = "unsafe"

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        _evidence(event)


@pytest.mark.parametrize("field_name", ("key", "api_key", "token", "authorization"))
def test_credential_bearing_history_rows_are_rejected(field_name: str) -> None:
    row = _history_row()
    row[field_name] = "unsafe"

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        _evidence(history_rows=(row,))


def test_history_row_cross_event_and_future_unsafe_shape_fail_closed() -> None:
    row = _history_row()
    row["event_id"] = "event-2"
    with pytest.raises(OddsWeatherContractError, match="disagrees"):
        _evidence(history_rows=(row,))

    row = _history_row()
    row["retrieved_at"] = "2026-07-27T13:01:00"
    with pytest.raises(OddsWeatherContractError, match="timezone-aware"):
        _evidence(history_rows=(row,))


def test_configured_secret_is_rejected_without_entering_canonical_output() -> None:
    event = _event()
    event["bookmakers"][0]["title"] = f"Book {CONFIGURED_SECRET}"

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        _evidence(event, secret_values=(CONFIGURED_SECRET,))

    safe = _evidence(secret_values=(CONFIGURED_SECRET,))
    baseline = _evidence()
    assert safe == baseline
    assert safe.as_dict() == baseline.as_dict()
    assert CONFIGURED_SECRET not in json.dumps(safe.as_dict(), sort_keys=True)


def test_configured_secret_is_rejected_in_history_values() -> None:
    row = _history_row()
    row["outcome_name"] = f"Los Angeles {CONFIGURED_SECRET}"

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        _evidence(
            history_rows=(row,),
            secret_values=(CONFIGURED_SECRET,),
        )


def test_odds_adapter_applies_configured_secret_boundary_to_events_and_warnings() -> None:
    event = _event()
    event["bookmakers"][0]["title"] = f"Book {CONFIGURED_SECRET}"
    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        odds_collection_to_phase4(
            _collection(event),
            secret_values=(CONFIGURED_SECRET,),
        )

    warning = OddsCollectorWarning(
        code="malformed_bookmaker",
        message=f"Excluded {CONFIGURED_SECRET}",
        created_at=RETRIEVED.isoformat(),
        bookmaker="bad-book",
    )
    with pytest.raises(OddsWeatherAdapterError, match="credential-bearing"):
        odds_collection_to_phase4(
            _collection(_event(), warnings=(warning,)),
            secret_values=(CONFIGURED_SECRET,),
        )


def test_openweather_adapter_rejects_configured_secret_in_forecast() -> None:
    forecast = _forecast()
    forecast["short_forecast"] = f"Clear {CONFIGURED_SECRET}"
    capture = RawPayloadCapture(
        provider="openweather",
        endpoint_category="one_call",
        payload={"hourly": []},
        retrieved_at=RETRIEVED.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
    )

    with pytest.raises(OddsWeatherContractError, match="credential-bearing"):
        openweather_forecast_to_phase4(
            source_game_id="900001",
            forecast=forecast,
            capture=capture,
            secret_values=(CONFIGURED_SECRET,),
        )


def test_request_url_or_transport_metadata_cannot_enter_event_contract() -> None:
    event = _event()
    event["request_url"] = (
        f"https://api.example.test/odds?apiKey={CONFIGURED_SECRET}"
    )

    with pytest.raises(OddsWeatherContractError, match="transport metadata"):
        _evidence(event, secret_values=(CONFIGURED_SECRET,))


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("blank_bookmaker_title", "title"),
        ("naive_bookmaker_update", "timezone-aware"),
        ("unsupported_market", "not supported"),
        ("non_numeric_price", "finite numeric"),
    ),
)
def test_provider_event_closed_structure_fails_on_malformed_input(
    mutation: str,
    message: str,
) -> None:
    event = _event()
    if mutation == "blank_bookmaker_title":
        event["bookmakers"][0]["title"] = ""
    elif mutation == "naive_bookmaker_update":
        event["bookmakers"][0]["last_update"] = "2026-07-27T14:14:00"
    elif mutation == "unsupported_market":
        event["bookmakers"][0]["markets"][0]["key"] = "player_props"
    else:
        event["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = "-120"

    with pytest.raises(OddsWeatherContractError, match=message):
        _evidence(event)


def test_raw_odds_capture_preserves_only_schema_owned_key_paths() -> None:
    event = _event()
    event["bookmakers"][0]["markets"][0]["outcomes"][0]["key"] = "unsafe"
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[event],
        retrieved_at=RETRIEVED.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
    )

    retained = json.loads(sanitized_json_bytes(capture))

    assert retained[0]["bookmakers"][0]["key"] == "book-a"
    assert retained[0]["bookmakers"][0]["markets"][0]["key"] == "h2h"
    assert (
        retained[0]["bookmakers"][0]["markets"][0]["outcomes"][0]["key"]
        == REDACTED
    )


def test_generic_redaction_behavior_remains_strict_outside_phase4() -> None:
    payload = {"key": "sportsbook-identifier", "nested": {"key": "another"}}

    assert redact_value(copy.deepcopy(payload)) == {
        "key": REDACTED,
        "nested": {"key": REDACTED},
    }
