from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

import pytest

from app.identifiers import generate_run_id, parse_requested_date, parse_run_id, validate_run_id


UUID_HEX = "0123456789abcdef" * 2
VALID_RUN_ID = f"run_20260711_{UUID_HEX}"


def test_parse_requested_date_accepts_exact_calendar_date() -> None:
    assert parse_requested_date("2026-07-11") == date(2026, 7, 11)
    assert parse_requested_date("2024-02-29") == date(2024, 2, 29)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " 2026-07-11",
        "2026-07-11 ",
        "2026-7-11",
        "20260711",
        "2026-07-11T00:00:00Z",
        "../2026-07-11",
        "2026-07-11/..",
        "2026-07-11%2f..",
        "2026%2d07%2d11",
        "2026-02-29",
        "2026-13-01",
        date(2026, 7, 11),
    ],
)
def test_parse_requested_date_rejects_noncanonical_or_invalid_values(value: Any) -> None:
    with pytest.raises(ValueError):
        parse_requested_date(value)


def test_generate_run_id_uses_canonical_format() -> None:
    run_id = generate_run_id(date(2026, 7, 11))

    assert re.fullmatch(r"run_20260711_[0-9a-f]{32}", run_id)
    assert validate_run_id(run_id, expected_date=date(2026, 7, 11)) == run_id


def test_generate_run_id_rejects_datetime_subclass() -> None:
    with pytest.raises(TypeError):
        generate_run_id(datetime(2026, 7, 11, tzinfo=timezone.utc))


def test_parse_run_id_returns_embedded_identity() -> None:
    parsed = parse_run_id(VALID_RUN_ID)

    assert parsed.value == VALID_RUN_ID
    assert parsed.requested_date == date(2026, 7, 11)
    assert parsed.uuid_hex == UUID_HEX


@pytest.mark.parametrize(
    "value",
    [
        "",
        f" run_20260711_{UUID_HEX}",
        f"run_20260711_{UUID_HEX} ",
        f"run-20260711-{UUID_HEX}",
        f"run_2026071_{UUID_HEX}",
        f"run_20260711_{UUID_HEX[:-1]}",
        f"run_20260711_{UUID_HEX}0",
        f"run_20260711_{UUID_HEX.upper()}",
        f"run_20260229_{UUID_HEX}",
        f"../run_20260711_{UUID_HEX}",
        f"run_20260711_{UUID_HEX}/..",
        f"run_20260711_{UUID_HEX}%2f..",
        f"C:\\run_20260711_{UUID_HEX}",
        f"\\\\server\\share\\run_20260711_{UUID_HEX}",
    ],
)
def test_validate_run_id_rejects_malformed_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(value)


def test_validate_run_id_rejects_requested_date_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        validate_run_id(VALID_RUN_ID, expected_date=date(2026, 7, 12))
