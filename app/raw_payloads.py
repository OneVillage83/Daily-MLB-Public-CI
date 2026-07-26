from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

from app.redaction import redact_value

ProviderName = Literal["the_odds_api", "nws", "openweather"]
EndpointCategory = Literal["mlb_odds", "point_lookup", "hourly_forecast", "one_call"]
JsonPayload = dict[str, Any] | list[Any]

_ALLOWED_ENDPOINTS: dict[ProviderName, frozenset[EndpointCategory]] = {
    "the_odds_api": frozenset({"mlb_odds"}),
    "nws": frozenset({"point_lookup", "hourly_forecast"}),
    "openweather": frozenset({"one_call"}),
}


@dataclass(frozen=True, slots=True)
class RawPayloadCapture:
    """In-memory capture of a parsed provider response without request metadata."""

    provider: ProviderName
    endpoint_category: EndpointCategory
    payload: JsonPayload
    retrieved_at: str
    provider_timestamp: str | None
    content_type: str
    event_id: str | None = None

    def __post_init__(self) -> None:
        if self.endpoint_category not in _ALLOWED_ENDPOINTS.get(self.provider, frozenset()):
            raise ValueError(
                f"Endpoint category {self.endpoint_category!r} is not valid for provider {self.provider!r}"
            )
        parsed_retrieved_at = datetime.fromisoformat(self.retrieved_at.replace("Z", "+00:00"))
        if parsed_retrieved_at.tzinfo is None or parsed_retrieved_at.utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError("retrieved_at must be a timezone-aware UTC timestamp")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        return None
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value) if value is not None else None
    return None


def normalize_provider_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    source = value.strip()
    try:
        parsed = datetime.fromisoformat(source.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(source)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def capture_response(
    *,
    provider: ProviderName,
    endpoint_category: EndpointCategory,
    payload: JsonPayload,
    response: Any,
    provider_timestamp: object = None,
    event_id: str | None = None,
    use_response_date_as_provider_timestamp: bool = True,
) -> RawPayloadCapture:
    source_timestamp = normalize_provider_timestamp(provider_timestamp)
    if source_timestamp is None and use_response_date_as_provider_timestamp:
        source_timestamp = normalize_provider_timestamp(response_header(response, "Date"))
    return RawPayloadCapture(
        provider=provider,
        endpoint_category=endpoint_category,
        payload=payload,
        retrieved_at=utc_now_iso(),
        provider_timestamp=source_timestamp,
        content_type=response_header(response, "Content-Type") or "application/json",
        event_id=event_id,
    )


def sanitized_json_bytes(
    capture: RawPayloadCapture,
    *,
    secret_values: Iterable[str] = (),
) -> bytes:
    preserved_fields = ("key",) if capture.provider == "the_odds_api" else ()
    sanitized = redact_value(
        capture.payload,
        secret_values,
        preserve_field_names=preserved_fields,
    )
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sanitized_checksum(
    capture: RawPayloadCapture,
    *,
    secret_values: Iterable[str] = (),
) -> str:
    return hashlib.sha256(
        sanitized_json_bytes(capture, secret_values=secret_values)
    ).hexdigest()
