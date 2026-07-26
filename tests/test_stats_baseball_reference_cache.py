from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import pytest
import requests

from app.stats.contracts import (
    FixtureResponse,
    StatsProviderPayloadError,
    StatsTransportError,
)
from app.stats.providers.baseball_reference import (
    BaseballReferenceProvider,
    BaseballReferenceTable,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport, HttpStatsTransport


VALID_HTML = b"""<!doctype html>
<html><body><table id="cache_table">
<thead><tr><th data-stat="name">Name</th></tr></thead>
<tbody><tr><td data-stat="name">Example Player</td></tr></tbody>
</table></body></html>
"""


@dataclass
class FakeResponse:
    body: bytes

    def __post_init__(self) -> None:
        self.status_code = 200
        self.headers = {"Content-Type": "text/html", "ETag": '"source-v1"'}
        self.url = "https://www.baseball-reference.com/cache-test"

    def iter_content(self, *, chunk_size: int) -> object:
        del chunk_size
        yield self.body


class RecordingSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("a cached Baseball-Reference request reached the network")
        return self.responses.pop(0)


class FakeClock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


def _provider(
    raw_root: Path,
    session: RecordingSession,
    clock: FakeClock,
) -> BaseballReferenceProvider:
    transport = HttpStatsTransport(
        RawArtifactStore(raw_root),
        user_agent="DailyMLBStats/1.0 (ops@example.test)",
        session=cast(requests.Session, session),
        clock=clock.now,
        sleeper=clock.sleep,
    )
    return BaseballReferenceProvider(transport)


def _collect(
    provider: BaseballReferenceProvider,
    *,
    marker: str = "same-query",
) -> BaseballReferenceTable:
    return provider.collect_table(
        "/cache-test",
        table_id="cache_table",
        params={"marker": marker, "api_key": "must-not-be-persisted"},
        fixture_key=f"bref:{marker}",
    )


def test_new_transport_reuses_verified_durable_bytes_and_original_identity(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    first_clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    first_session = RecordingSession([FakeResponse(VALID_HTML)])
    first = _collect(_provider(raw_root, first_session, first_clock))

    second_clock = FakeClock(datetime(2026, 7, 15, 20, tzinfo=timezone.utc))
    second_session = RecordingSession([])
    second = _collect(_provider(raw_root, second_session, second_clock))

    assert first.attempts == 1
    assert second.attempts == 0
    assert second.raw.capture_id == first.raw.capture_id
    assert second.raw.retrieved_at == first.raw.retrieved_at
    assert second.raw.path == first.raw.path
    assert len(first_session.calls) == 1
    assert second_session.calls == []
    assert len(list(raw_root.rglob("*.bin"))) == 1


def test_cache_index_never_persists_url_or_request_parameter_values(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    _collect(_provider(raw_root, RecordingSession([FakeResponse(VALID_HTML)]), clock))

    indexes = list((raw_root / "cache" / "baseball_reference").glob("*.json"))
    assert len(indexes) == 1
    index_text = indexes[0].read_text(encoding="utf-8")
    assert "baseball-reference.com" not in index_text
    assert "cache-test" not in index_text
    assert "same-query" not in index_text
    assert "must-not-be-persisted" not in index_text
    assert "api_key" not in index_text


def test_cached_byte_corruption_fails_closed_before_network(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    first = _collect(_provider(raw_root, RecordingSession([FakeResponse(VALID_HTML)]), clock))
    first.raw.path.write_bytes(b"corrupted source evidence")

    replacement_session = RecordingSession([FakeResponse(VALID_HTML)])
    replacement = _provider(
        raw_root,
        replacement_session,
        FakeClock(datetime(2026, 7, 15, 20, tzinfo=timezone.utc)),
    )
    with pytest.raises(StatsTransportError, match="cache verification failed"):
        _collect(replacement)

    assert replacement_session.calls == []


def test_payload_that_fails_provider_validation_is_not_cached(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    invalid_session = RecordingSession(
        [FakeResponse(b"<html><body>missing table</body></html>")]
    )
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    with pytest.raises(StatsProviderPayloadError, match="was not present"):
        _collect(_provider(raw_root, invalid_session, clock))

    assert list((raw_root / "cache").rglob("*.json")) == []
    valid_session = RecordingSession([FakeResponse(VALID_HTML)])
    result = _collect(_provider(raw_root, valid_session, clock))
    assert result.attempts == 1
    assert len(valid_session.calls) == 1


def test_distinct_live_requests_still_observe_six_second_interval(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    session = RecordingSession([FakeResponse(VALID_HTML), FakeResponse(VALID_HTML)])
    provider = _provider(raw_root, session, clock)

    _collect(provider, marker="first-query")
    _collect(provider, marker="second-query")

    assert len(session.calls) == 2
    assert clock.sleeps == [6.0]


def test_evolving_schedule_requests_bypass_persistent_cache(tmp_path: Path) -> None:
    raw_root = tmp_path / "raw"
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    scheduled = VALID_HTML.replace(b"Example Player", b"Scheduled")
    final = VALID_HTML.replace(b"Example Player", b"Final")
    session = RecordingSession([FakeResponse(scheduled), FakeResponse(final)])
    provider = _provider(raw_root, session, clock)

    first = provider.collect_table(
        "/cache-test",
        table_id="cache_table",
        fixture_key="bref:schedule",
        persistent_cache=False,
    )
    second = provider.collect_table(
        "/cache-test",
        table_id="cache_table",
        fixture_key="bref:schedule",
        persistent_cache=False,
    )

    assert first.rows[0]["name"] == "Scheduled"
    assert second.rows[0]["name"] == "Final"
    assert first.raw.capture_id != second.raw.capture_id
    assert len(session.calls) == 2
    assert clock.sleeps == [6.0]
    assert not (raw_root / "cache").exists()


def test_offline_fixtures_remain_raw_first_without_live_cache_reuse(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    clock = FakeClock(datetime(2026, 7, 14, 20, tzinfo=timezone.utc))
    transport = FixtureStatsTransport(
        RawArtifactStore(raw_root),
        {"bref:fixture": FixtureResponse(VALID_HTML, "text/html")},
        clock=clock.now,
    )
    provider = BaseballReferenceProvider(transport)

    first = provider.collect_table(
        "/cache-test", table_id="cache_table", fixture_key="bref:fixture"
    )
    clock.value += timedelta(seconds=1)
    second = provider.collect_table(
        "/cache-test", table_id="cache_table", fixture_key="bref:fixture"
    )

    assert first.raw.capture_id != second.raw.capture_id
    assert second.raw.retrieved_at > first.raw.retrieved_at
    assert len(list(raw_root.rglob("*.bin"))) == 2
    assert not (raw_root / "cache").exists()
