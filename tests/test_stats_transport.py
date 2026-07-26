from __future__ import annotations

import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
import requests

from app.stats.contracts import StatsProvider, StatsRequest, StatsRequestError, StatsTransportError
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import HttpStatsTransport


def _hold_transport_file_lock(
    lock_path: str,
    acquired: Any,
    release: Any,
) -> None:
    from app.stats.transport import _ExclusiveFileLock

    with _ExclusiveFileLock(Path(lock_path)):
        acquired.set()
        if not release.wait(10.0):
            raise RuntimeError("test process did not receive the lock release signal")


@dataclass
class Call:
    url: str
    params: dict[str, object]
    headers: dict[str, str]
    timeout: float
    stream: bool


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.current += timedelta(seconds=seconds)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: bytes,
        *,
        content_type: str = "text/csv",
        headers: dict[str, str] | None = None,
        url: str = "https://provider.example/data",
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.url = url

    def iter_content(self, *, chunk_size: int) -> object:
        del chunk_size
        yield self.body


class SequenceSession:
    def __init__(self, responses: list[FakeResponse | requests.RequestException]) -> None:
        self.responses = responses
        self.calls: list[Call] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        headers: dict[str, str],
        timeout: float,
        allow_redirects: bool,
        stream: bool,
    ) -> FakeResponse:
        assert allow_redirects is False
        self.calls.append(Call(url, params, headers, timeout, stream))
        item = self.responses.pop(0)
        if isinstance(item, requests.RequestException):
            raise item
        return item


def request(
    provider: StatsProvider = StatsProvider.STATCAST,
    *,
    minimum_interval_seconds: float = 0.0,
) -> StatsRequest:
    return StatsRequest(
        provider=provider,
        endpoint_category="search_csv" if provider is StatsProvider.STATCAST else "html_table",
        fixture_key="fixture",
        url="https://provider.example/data",
        params={"game": "1"},
        headers={"Accept": "text/csv"},
        timeout_seconds=11.0,
        max_attempts=3,
        minimum_interval_seconds=minimum_interval_seconds,
    )


def transport(
    tmp_path: Path,
    session: SequenceSession,
    clock: FakeClock,
) -> HttpStatsTransport:
    return HttpStatsTransport(
        RawArtifactStore(tmp_path / "raw"),
        user_agent="DailyMLBStats/1.0 (ops@example.test)",
        session=cast(requests.Session, session),
        clock=clock.now,
        sleeper=clock.sleep,
    )


def test_request_requires_https_and_baseball_reference_six_second_floor() -> None:
    with pytest.raises(StatsRequestError, match="HTTPS"):
        StatsRequest(
            provider=StatsProvider.STATCAST,
            endpoint_category="search_csv",
            fixture_key="fixture",
            url="http://provider.example/data",
        )
    with pytest.raises(ValueError, match="six seconds"):
        request(StatsProvider.BASEBALL_REFERENCE, minimum_interval_seconds=5.999)
    with pytest.raises(ValueError, match="single-line"):
        StatsRequest(
            provider=StatsProvider.STATCAST,
            endpoint_category="search_csv",
            fixture_key="fixture",
            url="https://provider.example/data",
            headers={"User-Agent": "safe\r\nInjected: true"},
        )


def test_transport_injects_headers_timeout_and_retains_before_return(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession([FakeResponse(200, b"raw-provider-bytes")])

    result = transport(tmp_path, session, clock).fetch(request())

    assert result.capture.path.read_bytes() == b"raw-provider-bytes"
    assert result.attempts == 1
    assert session.calls[0].timeout == 11.0
    assert session.calls[0].stream is True
    assert session.calls[0].headers["User-Agent"].startswith("DailyMLBStats/")
    assert session.calls[0].headers["Accept"] == "text/csv"
    assert session.calls[0].headers["Accept-Encoding"] == "identity"


def test_transport_retains_undecoded_response_entity_bytes(tmp_path: Path) -> None:
    class RawStream:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.decode_flags: list[bool] = []

        def stream(self, chunk_size: int, *, decode_content: bool) -> object:
            del chunk_size
            self.decode_flags.append(decode_content)
            yield self.payload

    response = FakeResponse(200, b"decoded-body")
    response.raw = RawStream(b"wire-entity-bytes")  # type: ignore[attr-defined]
    clock = FakeClock()
    session = SequenceSession([response])

    result = transport(tmp_path, session, clock).fetch(request())

    assert result.capture.path.read_bytes() == b"wire-entity-bytes"
    assert response.raw.decode_flags == [False]  # type: ignore[attr-defined]


def test_encoded_response_fails_closed_after_exact_raw_retention(tmp_path: Path) -> None:
    clock = FakeClock()
    response = FakeResponse(
        200,
        b"compressed-entity",
        headers={"Content-Encoding": "gzip"},
    )

    with pytest.raises(StatsTransportError, match="encoded response") as caught:
        transport(tmp_path, SequenceSession([response]), clock).fetch(request())

    assert caught.value.captures[0].path.read_bytes() == b"compressed-entity"


def test_transient_response_is_retained_and_retry_after_is_respected(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession(
        [
            FakeResponse(503, b"temporary", headers={"Retry-After": "2"}),
            FakeResponse(200, b"success"),
        ]
    )

    result = transport(tmp_path, session, clock).fetch(request())

    assert clock.sleeps == [2.0]
    assert result.attempts == 2
    assert [item.path.read_bytes() for item in result.attempt_captures] == [
        b"temporary",
        b"success",
    ]


def test_permanent_error_is_not_retried_and_retains_body(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession([FakeResponse(403, b"forbidden")])

    with pytest.raises(StatsTransportError) as caught:
        transport(tmp_path, session, clock).fetch(request())

    assert caught.value.status_code == 403
    assert caught.value.attempts == 1
    assert caught.value.captures[0].path.read_bytes() == b"forbidden"
    assert len(session.calls) == 1


def test_baseball_reference_rate_limit_stops_without_retry(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession(
        [
            FakeResponse(429, b"rate-limited", headers={"Retry-After": "60"}),
            FakeResponse(200, b"must-not-be-requested"),
        ]
    )
    bref = request(
        StatsProvider.BASEBALL_REFERENCE,
        minimum_interval_seconds=6.0,
    )

    with pytest.raises(StatsTransportError, match="provider block") as caught:
        transport(tmp_path, session, clock).fetch(bref)

    assert caught.value.attempts == 1
    assert caught.value.captures[0].path.read_bytes() == b"rate-limited"
    assert clock.sleeps == []
    assert len(session.calls) == 1


def test_rejected_response_url_is_checked_after_raw_retention(tmp_path: Path) -> None:
    clock = FakeClock()
    response = FakeResponse(
        302,
        b"redirect-response",
        url="https://unexpected.example/redirected",
    )

    with pytest.raises(StatsTransportError, match="unexpected host") as caught:
        transport(tmp_path, SequenceSession([response]), clock).fetch(request())

    assert caught.value.attempts == 1
    assert caught.value.captures[0].path.read_bytes() == b"redirect-response"


def test_timeout_retries_without_exposing_query_values(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession(
        [requests.Timeout("https://provider.example?token=secret"), requests.Timeout("secret")]
    )
    controlled = request()
    controlled = StatsRequest(
        provider=controlled.provider,
        endpoint_category=controlled.endpoint_category,
        fixture_key=controlled.fixture_key,
        url=controlled.url,
        params={"token": "do-not-print"},
        headers=controlled.headers,
        max_attempts=2,
    )

    with pytest.raises(StatsTransportError) as caught:
        transport(tmp_path, session, clock).fetch(controlled)

    assert caught.value.attempts == 2
    assert "do-not-print" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_response_stream_failure_is_retried_without_partial_artifact(tmp_path: Path) -> None:
    class BrokenStreamResponse(FakeResponse):
        def iter_content(self, *, chunk_size: int) -> object:
            del chunk_size
            yield b"partial"
            raise requests.ConnectionError("stream ended")

    clock = FakeClock()
    session = SequenceSession(
        [BrokenStreamResponse(200, b"unused"), FakeResponse(200, b"complete")]
    )

    result = transport(tmp_path, session, clock).fetch(request())

    assert result.attempts == 2
    assert len(result.attempt_captures) == 1
    assert result.capture.path.read_bytes() == b"complete"
    assert list((tmp_path / "raw").rglob("*.part")) == []


def test_baseball_reference_request_starts_are_at_least_six_seconds_apart(tmp_path: Path) -> None:
    clock = FakeClock()
    session = SequenceSession([FakeResponse(200, b"one"), FakeResponse(200, b"two")])
    client = transport(tmp_path, session, clock)
    bref = request(
        StatsProvider.BASEBALL_REFERENCE,
        minimum_interval_seconds=6.0,
    )

    client.fetch(bref)
    client.fetch(bref)

    assert clock.sleeps == [6.0]


def test_baseball_reference_interval_persists_across_transport_instances(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    first_session = SequenceSession([FakeResponse(200, b"one")])
    second_session = SequenceSession([FakeResponse(200, b"two")])
    bref = request(
        StatsProvider.BASEBALL_REFERENCE,
        minimum_interval_seconds=6.0,
    )

    transport(tmp_path, first_session, clock).fetch(bref)
    transport(tmp_path, second_session, clock).fetch(bref)

    assert clock.sleeps == [6.0]
    state = json.loads(
        (tmp_path / "raw" / "transport_state" / "baseball_reference.json").read_text(
            encoding="utf-8"
        )
    )
    assert state == {
        "contract": "DSE_STATS_TRANSPORT_RATE_LIMIT_V1",
        "last_request_started_utc": "2026-07-14T18:00:06+00:00",
        "provider": "baseball_reference",
    }


def test_baseball_reference_lock_serializes_independent_transport_instances(
    tmp_path: Path,
) -> None:
    class ConcurrentSession(SequenceSession):
        def __init__(self) -> None:
            super().__init__([FakeResponse(200, b"one"), FakeResponse(200, b"two")])
            self.active = 0
            self.maximum_active = 0
            self.guard = threading.Lock()

        def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
            with self.guard:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                time.sleep(0.02)
                return super().get(*args, **kwargs)
            finally:
                with self.guard:
                    self.active -= 1

    clock = FakeClock()
    session = ConcurrentSession()
    first = transport(tmp_path, session, clock)
    second = transport(tmp_path, session, clock)
    bref = request(
        StatsProvider.BASEBALL_REFERENCE,
        minimum_interval_seconds=6.0,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client.fetch, bref) for client in (first, second)]
        results = [future.result() for future in futures]

    assert len(results) == 2
    assert session.maximum_active == 1
    assert clock.sleeps == [6.0]


def test_statcast_lock_serializes_independent_transport_instances(
    tmp_path: Path,
) -> None:
    class ConcurrentSession(SequenceSession):
        def __init__(self) -> None:
            super().__init__([FakeResponse(200, b"one"), FakeResponse(200, b"two")])
            self.active = 0
            self.maximum_active = 0
            self.guard = threading.Lock()

        def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
            with self.guard:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                time.sleep(0.02)
                return super().get(*args, **kwargs)
            finally:
                with self.guard:
                    self.active -= 1

    clock = FakeClock()
    session = ConcurrentSession()
    clients = (transport(tmp_path, session, clock), transport(tmp_path, session, clock))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(client.fetch, request()) for client in clients]
        results = [future.result() for future in futures]

    assert len(results) == 2
    assert session.maximum_active == 1


def test_statcast_request_waits_for_cross_process_provider_lock(
    tmp_path: Path,
) -> None:
    raw_root = tmp_path / "raw"
    lock_path = raw_root / "transport_state" / "statcast.lock"
    lock_path.parent.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    acquired_by_process = context.Event()
    release_process = context.Event()
    process = context.Process(
        target=_hold_transport_file_lock,
        args=(str(lock_path), acquired_by_process, release_process),
    )
    process.start()
    assert acquired_by_process.wait(10.0)

    session = SequenceSession([FakeResponse(200, b"one")])
    client = transport(tmp_path, session, FakeClock())
    completed = threading.Event()

    def fetch_statcast() -> None:
        client.fetch(request())
        completed.set()

    contender = threading.Thread(target=fetch_statcast)
    contender.start()
    assert not completed.wait(0.1)
    assert session.calls == []

    release_process.set()
    assert completed.wait(10.0)
    contender.join(timeout=10.0)
    process.join(timeout=10.0)
    assert not contender.is_alive()
    assert process.exitcode == 0
    assert len(session.calls) == 1


def test_baseball_reference_file_lock_excludes_a_second_process(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "raw" / "transport_state" / "baseball_reference.lock"
    lock_path.parent.mkdir(parents=True)
    context = multiprocessing.get_context("spawn")
    acquired_by_process = context.Event()
    release_process = context.Event()
    process = context.Process(
        target=_hold_transport_file_lock,
        args=(str(lock_path), acquired_by_process, release_process),
    )
    process.start()
    assert acquired_by_process.wait(10.0)

    acquired_locally = threading.Event()

    def acquire_locally() -> None:
        from app.stats.transport import _ExclusiveFileLock

        with _ExclusiveFileLock(lock_path):
            acquired_locally.set()

    contender = threading.Thread(target=acquire_locally)
    contender.start()
    assert not acquired_locally.wait(0.1)

    release_process.set()
    assert acquired_locally.wait(10.0)
    contender.join(timeout=10.0)
    process.join(timeout=10.0)
    assert not contender.is_alive()
    assert process.exitcode == 0


def test_baseball_reference_malformed_persistent_rate_limit_state_fails_closed(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    session = SequenceSession([FakeResponse(200, b"must-not-be-requested")])
    client = transport(tmp_path, session, clock)
    state_path = tmp_path / "raw" / "transport_state" / "baseball_reference.json"
    state_path.write_text('{"contract":"unexpected"}', encoding="utf-8")

    with pytest.raises(StatsTransportError, match="rate-limit state is malformed"):
        client.fetch(
            request(
                StatsProvider.BASEBALL_REFERENCE,
                minimum_interval_seconds=6.0,
            )
        )

    assert session.calls == []


def test_transport_serializes_session_access(tmp_path: Path) -> None:
    class ConcurrentSession(SequenceSession):
        def __init__(self) -> None:
            super().__init__([FakeResponse(200, b"one"), FakeResponse(200, b"two")])
            self.active = 0
            self.maximum_active = 0
            self.guard = threading.Lock()

        def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
            with self.guard:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            try:
                time.sleep(0.02)
                return super().get(*args, **kwargs)
            finally:
                with self.guard:
                    self.active -= 1

    clock = FakeClock()
    session = ConcurrentSession()
    client = transport(tmp_path, session, clock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: client.fetch(request()), range(2)))

    assert len(results) == 2
    assert session.maximum_active == 1
