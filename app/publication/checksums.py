from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json
import math
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class SupportsAsDict(Protocol):
    def as_dict(self) -> Mapping[str, Any]: ...


def normalize_payload(value: Any) -> Any:
    """Return a deterministic, detached JSON value for a domain payload."""
    if isinstance(value, SupportsAsDict):
        return normalize_payload(value.as_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return normalize_payload(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): normalize_payload(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [normalize_payload(item) for item in value]
    if isinstance(value, Enum):
        return normalize_payload(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("publication payloads may not contain non-finite numbers")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported publication payload value: {type(value).__name__}")


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        normalize_payload(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def payload_checksum(payload: Any) -> str:
    return sha256(canonical_json_bytes(payload)).hexdigest()
