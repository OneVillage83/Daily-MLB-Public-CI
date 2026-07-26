from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Mapping


class DataQualityState(str, Enum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class UncertaintyGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class CandidateDecision(str, Enum):
    PASS = "PASS"
    CANDIDATE_REQUIRES_REVIEW = "CANDIDATE_REQUIRES_REVIEW"


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    message: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class GateResult:
    code: str
    passed: bool
    threshold: bool | int | float | str | None
    observed_value: bool | int | float | str | None
    reason: str


def utc_datetime(value: datetime | str, *, field: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be a valid ISO-8601 timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def optional_utc_datetime(value: object) -> datetime | None:
    if not isinstance(value, (datetime, str)) or not value:
        return None
    try:
        return utc_datetime(value, field="timestamp")
    except (TypeError, ValueError):
        return None


def finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return utc_datetime(value, field="timestamp").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    return json.dumps(
        json_value(value),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def checksum_payload(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
