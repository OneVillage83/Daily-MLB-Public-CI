from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from app.stats.contracts import CsvRow, FixtureResponse, StatcastQuery, StatsProviderPayloadError
from app.stats.providers.statcast import (
    STATCAST_REQUIRED_ANALYTICAL_COLUMNS,
    STATCAST_REQUIRED_IDENTITY_COLUMNS,
    StatcastProvider,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

FIXTURES = Path(__file__).with_name("fixtures") / "stats"


def make_provider(
    tmp_path: Path,
    body: bytes | Path,
    *,
    content_type: str = "text/csv",
    parity_hook: Any = None,
) -> tuple[StatcastProvider, FixtureStatsTransport]:
    ids = itertools.count(1)
    store = RawArtifactStore(
        tmp_path / "raw",
        capture_id_factory=lambda: f"capture_{next(ids):04d}",
    )
    transport = FixtureStatsTransport(
        store,
        {"statcast": FixtureResponse(body, content_type)},
        clock=lambda: datetime(2026, 7, 14, 20, tzinfo=timezone.utc),
    )
    return StatcastProvider(transport, parity_hook=parity_hook), transport


def query() -> StatcastQuery:
    return StatcastQuery(date(2026, 7, 10), date(2026, 7, 11))


def test_csv_is_parsed_after_raw_retention_and_zero_remains_zero(tmp_path: Path) -> None:
    hook_calls: list[tuple[StatcastQuery, tuple[CsvRow, ...], str]] = []

    def parity_hook(
        value: StatcastQuery, rows: tuple[CsvRow, ...], checksum: str
    ) -> dict[str, object]:
        hook_calls.append((value, rows, checksum))
        return {"pybaseball_row_count": len(rows), "matches": True}

    provider, transport = make_provider(
        tmp_path,
        FIXTURES / "statcast.csv",
        parity_hook=parity_hook,
    )

    dataset = provider.collect(query(), fixture_key="statcast")

    assert set(dataset.columns) >= (
        STATCAST_REQUIRED_IDENTITY_COLUMNS | STATCAST_REQUIRED_ANALYTICAL_COLUMNS
    )
    assert dataset.rows[1]["launch_speed"] == "0"
    assert dataset.raw.path.read_bytes() == (FIXTURES / "statcast.csv").read_bytes()
    assert dataset.parity_diagnostics == {"pybaseball_row_count": 2, "matches": True}
    assert hook_calls[0][1] is dataset.rows
    assert hook_calls[0][2] == dataset.raw.checksum_sha256
    assert transport.requests[0].params["game_date_gt"] == "2026-07-10"
    assert transport.requests[0].params["game_date_lt"] == "2026-07-11"
    assert transport.requests[0].params["all"] == "true"
    assert transport.requests[0].params["hfGT"] == "R|PO|S|"
    assert transport.requests[0].params["player_type"] == "pitcher"
    assert transport.requests[0].params["min_pitches"] == 0


def test_application_download_csv_is_parsed_after_raw_retention(
    tmp_path: Path,
) -> None:
    fixture = FIXTURES / "statcast_application_download.csv"
    provider, _ = make_provider(
        tmp_path,
        fixture,
        content_type="application/download; charset=utf-8",
    )

    dataset = provider.collect(query(), fixture_key="statcast")

    assert dataset.raw.content_type == "application/download; charset=utf-8"
    assert dataset.raw.path.read_bytes() == fixture.read_bytes()
    assert dataset.rows[0]["game_pk"] == "900001"
    assert set(dataset.columns) >= (
        STATCAST_REQUIRED_IDENTITY_COLUMNS | STATCAST_REQUIRED_ANALYTICAL_COLUMNS
    )


def test_parity_hook_cannot_replace_authoritative_csv_rows(tmp_path: Path) -> None:
    def parity_hook(
        value: StatcastQuery, rows: tuple[CsvRow, ...], checksum: str
    ) -> dict[str, object]:
        del value, rows, checksum
        return {"rows": [{"game_pk": "replacement"}]}

    provider, _ = make_provider(
        tmp_path, FIXTURES / "statcast.csv", parity_hook=parity_hook
    )

    dataset = provider.collect(query(), fixture_key="statcast")

    assert dataset.rows[0]["game_pk"] == "777001"


def test_parity_hook_cannot_mutate_authoritative_csv_rows(tmp_path: Path) -> None:
    def parity_hook(
        value: StatcastQuery, rows: tuple[CsvRow, ...], checksum: str
    ) -> None:
        del value, checksum
        with pytest.raises(TypeError):
            cast(dict[str, str], rows[0])["game_pk"] = "replacement"

    provider, _ = make_provider(
        tmp_path, FIXTURES / "statcast.csv", parity_hook=parity_hook
    )

    dataset = provider.collect(query(), fixture_key="statcast")

    assert dataset.rows[0]["game_pk"] == "777001"


@pytest.mark.parametrize(
    ("body", "content_type", "message"),
    [
        (b"player_name,events\nExample,home_run\n", "text/csv", "missing required"),
        (
            b"<!doctype html><html><body>Access Denied</body></html>",
            "text/html",
            "block page",
        ),
        (
            b"game_pk,game_date,game_type,at_bat_number,pitch_number,batter,"
            b"pitcher,home_team,away_team\n1\n",
            "text/csv",
            "fewer values",
        ),
    ],
)
def test_invalid_or_blocked_statcast_content_fails_after_retention(
    tmp_path: Path,
    body: bytes,
    content_type: str,
    message: str,
) -> None:
    provider, _ = make_provider(tmp_path, body, content_type=content_type)

    with pytest.raises(StatsProviderPayloadError, match=message) as caught:
        provider.collect(query(), fixture_key="statcast")

    assert caught.value.capture.path.read_bytes() == body


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            b"<!doctype html><html><body>Access Denied</body></html>",
            "block page",
        ),
        (b"game_pk,game_date\n1,\xff\n", "valid UTF-8 CSV"),
    ],
)
def test_application_download_does_not_bypass_payload_validation(
    tmp_path: Path,
    body: bytes,
    message: str,
) -> None:
    provider, _ = make_provider(
        tmp_path,
        body,
        content_type="application/download; charset=utf-8",
    )

    with pytest.raises(StatsProviderPayloadError, match=message) as caught:
        provider.collect(query(), fixture_key="statcast")

    assert caught.value.capture.content_type == "application/download; charset=utf-8"
    assert caught.value.capture.path.read_bytes() == body


def test_statcast_query_rejects_reversed_dates_and_controlled_parameter_override() -> None:
    with pytest.raises(ValueError, match="must not precede"):
        StatcastQuery(date(2026, 7, 12), date(2026, 7, 11))
    with pytest.raises(ValueError, match="controlled parameters"):
        StatcastQuery(
            date(2026, 7, 10),
            date(2026, 7, 11),
            extra_params={"player_type": "pitcher"},
        )
    with pytest.raises(ValueError, match="controlled parameters"):
        StatcastQuery(
            date(2026, 7, 10),
            date(2026, 7, 11),
            extra_params={"all": "false"},
        )


def test_statcast_rejects_identity_only_schema_without_analytical_columns(
    tmp_path: Path,
) -> None:
    body = (
        b"game_pk,game_date,game_type,at_bat_number,pitch_number,batter,pitcher,"
        b"home_team,away_team\n777001,2026-07-10,R,1,1,1001,2001,LAD,SF\n"
    )
    provider, _ = make_provider(tmp_path, body)

    with pytest.raises(StatsProviderPayloadError, match="analytical columns"):
        provider.collect(query(), fixture_key="statcast")
