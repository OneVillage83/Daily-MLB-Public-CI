from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from uuid import uuid4


_REQUESTED_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_RUN_ID_PATTERN = re.compile(r"run_([0-9]{4})([0-9]{2})([0-9]{2})_([0-9a-f]{32})\Z")


@dataclass(frozen=True, slots=True)
class ParsedRunId:
    value: str
    requested_date: date
    uuid_hex: str


def parse_requested_date(value: str) -> date:
    """Parse the API's exact ``YYYY-MM-DD`` date representation."""
    if not isinstance(value, str) or _REQUESTED_DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("requested_date must be an exact YYYY-MM-DD string")

    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("requested_date must be a valid calendar date") from exc

    if parsed.isoformat() != value:
        raise ValueError("requested_date must be an exact YYYY-MM-DD string")
    return parsed


def generate_run_id(requested_date: date) -> str:
    """Generate the canonical run identifier for a requested slate date."""
    if not isinstance(requested_date, date) or isinstance(requested_date, datetime):
        raise TypeError("requested_date must be a datetime.date")
    return f"run_{requested_date:%Y%m%d}_{uuid4().hex}"


def parse_run_id(value: str) -> ParsedRunId:
    if not isinstance(value, str):
        raise ValueError("run_id must be a canonical run identifier")

    match = _RUN_ID_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("run_id must match run_YYYYMMDD_<32 lowercase hex characters>")

    year, month, day, uuid_hex = match.groups()
    try:
        requested_date = date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ValueError("run_id contains an invalid calendar date") from exc

    return ParsedRunId(value=value, requested_date=requested_date, uuid_hex=uuid_hex)


def validate_run_id(value: str, *, expected_date: date | None = None) -> str:
    parsed = parse_run_id(value)
    if expected_date is not None:
        if not isinstance(expected_date, date) or isinstance(expected_date, datetime):
            raise TypeError("expected_date must be a datetime.date")
        if parsed.requested_date != expected_date:
            raise ValueError("run_id date does not match requested_date")
    return parsed.value
