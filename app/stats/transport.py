from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import threading
import time
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator
from urllib.parse import urlsplit

import requests

from app.stats.contracts import (
    Clock,
    FixtureResponse,
    RawArtifact,
    Sleeper,
    StatsProvider,
    StatsRequest,
    StatsResponse,
    StatsTransportError,
)
from app.stats.raw_store import RawArtifactStore, RawStoreError

_RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SAFE_RESPONSE_HEADERS = frozenset(
    {
        "content-type",
        "content-encoding",
        "date",
        "etag",
        "last-modified",
        "retry-after",
    }
)
_REQUEST_FINGERPRINT_CONTRACT = "DSE_STATS_REQUEST_FINGERPRINT_V1"
_TRANSPORT_RATE_LIMIT_CONTRACT = "DSE_STATS_TRANSPORT_RATE_LIMIT_V1"


class StatsLockUnavailableError(StatsTransportError):
    """Raised when a nonblocking statistics lock is already held."""


class _ExclusiveFileLock:
    """Provider-scoped advisory lock shared by processes using one raw root."""

    def __init__(self, path: Path, *, blocking: bool = True) -> None:
        self.path = path
        self.blocking = blocking
        self._descriptor: int | None = None

    def __enter__(self) -> _ExclusiveFileLock:
        if self.path.is_symlink():
            raise StatsTransportError("statistics transport lock must not be a symlink")
        flags = os.O_CREAT | os.O_RDWR
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        binary = getattr(os, "O_BINARY", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags | no_follow | binary, 0o600)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            self._lock_descriptor(descriptor, blocking=self.blocking)
        except StatsLockUnavailableError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise StatsTransportError(
                "statistics transport could not acquire its provider lock"
            ) from exc
        if descriptor is None:
            raise AssertionError("statistics transport lock descriptor is missing")
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            self._unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int, *, blocking: bool) -> None:
        if os.name == "nt":
            import msvcrt

            while True:
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not blocking:
                        raise StatsLockUnavailableError(
                            "statistics lock is already held by another process"
                        ) from exc
                    time.sleep(0.05)
            return

        fcntl = importlib.import_module("fcntl")
        flock = getattr(fcntl, "flock")
        operation = getattr(fcntl, "LOCK_EX")
        if not blocking:
            operation |= getattr(fcntl, "LOCK_NB")
        try:
            flock(descriptor, operation)
        except OSError as exc:
            if not blocking:
                raise StatsLockUnavailableError(
                    "statistics lock is already held by another process"
                ) from exc
            raise

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return

        fcntl = importlib.import_module("fcntl")
        flock = getattr(fcntl, "flock")
        flock(descriptor, getattr(fcntl, "LOCK_UN"))


def _retryable_status(request: StatsRequest, status_code: int) -> bool:
    if (
        request.provider is StatsProvider.BASEBALL_REFERENCE
        and status_code == 429
    ):
        return False
    return status_code in _RETRYABLE_STATUS_CODES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(clock: Clock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("statistics transport clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _headers_casefold(values: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    return {str(key).casefold(): (str(key), str(value)) for key, value in values.items()}


def _merge_headers(*sources: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, tuple[str, str]] = {}
    for source in sources:
        merged.update(_headers_casefold(source))
    return {original: value for original, value in merged.values()}


def _safe_response_headers(response: Any) -> Mapping[str, str]:
    source = getattr(response, "headers", {})
    if not isinstance(source, Mapping):
        return MappingProxyType({})
    retained = {
        str(key).casefold(): str(value)
        for key, value in source.items()
        if str(key).casefold() in _SAFE_RESPONSE_HEADERS and value is not None
    }
    return MappingProxyType(retained)


def _content_type(headers: Mapping[str, str]) -> str:
    return headers.get("content-type", "application/octet-stream")


def _request_fingerprint(request: StatsRequest) -> str:
    """Hash controlled request semantics without persisting a request URL or values."""
    document = {
        "contract": _REQUEST_FINGERPRINT_CONTRACT,
        "endpoint_category": request.endpoint_category,
        "headers": {key.casefold(): value for key, value in request.headers.items()},
        "method": "GET",
        "params": dict(request.params),
        "provider": request.provider.value,
        "url": request.url,
    }
    serialized = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _retry_after(value: str | None, *, now: datetime) -> float | None:
    if value is None or not value.strip():
        return None
    source = value.strip()
    try:
        delay = float(source)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(source)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at.astimezone(timezone.utc) - now).total_seconds()
    if not math.isfinite(delay):
        return None
    return max(0.0, delay)


def _response_chunks(response: Any) -> Iterable[bytes]:
    raw = getattr(response, "raw", None)
    stream = getattr(raw, "stream", None)
    if callable(stream):
        # requests.iter_content() asks urllib3 to decode gzip/deflate. Reading the
        # raw stream with decoding disabled preserves the exact response entity.
        yield from stream(64 * 1024, decode_content=False)
        return
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        yield from iterator(chunk_size=64 * 1024)
        return
    payload = getattr(response, "content", None)
    if not isinstance(payload, bytes):
        raise StatsTransportError("Provider response did not expose byte content")
    yield payload


class HttpStatsTransport:
    """Serial, retry-aware HTTPS transport that retains every HTTP response first."""

    def __init__(
        self,
        raw_store: RawArtifactStore,
        *,
        user_agent: str,
        session: requests.Session | None = None,
        clock: Clock = utc_now,
        sleeper: Sleeper = time.sleep,
        retry_backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0),
        retry_after_cap_seconds: float = 120.0,
    ) -> None:
        normalized_user_agent = user_agent.strip()
        if not normalized_user_agent or "\n" in normalized_user_agent or "\r" in normalized_user_agent:
            raise ValueError("statistics transport user_agent must be a single-line value")
        if not retry_backoff_seconds or any(
            not math.isfinite(value) or value < 0 for value in retry_backoff_seconds
        ):
            raise ValueError("retry backoff values must be finite and non-negative")
        if not math.isfinite(retry_after_cap_seconds) or retry_after_cap_seconds < 0:
            raise ValueError("retry_after_cap_seconds must be finite and non-negative")

        self.raw_store = raw_store
        self.session = session or requests.Session()
        self.clock = clock
        self.sleeper = sleeper
        self.retry_backoff_seconds = retry_backoff_seconds
        self.retry_after_cap_seconds = retry_after_cap_seconds
        self.default_headers = MappingProxyType(
            {
                "User-Agent": normalized_user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
        )
        self._serial_lock = threading.RLock()
        self._last_request_started: dict[str, datetime] = {}
        raw_root = self.raw_store.root.resolve(strict=True)
        state_directory = raw_root / "transport_state"
        state_directory.mkdir(parents=True, exist_ok=True)
        if state_directory.is_symlink():
            raise StatsTransportError(
                "statistics transport state directory must not be a symlink"
            )
        resolved_state_directory = state_directory.resolve(strict=True)
        try:
            resolved_state_directory.relative_to(raw_root)
        except ValueError as exc:
            raise StatsTransportError(
                "statistics transport state directory escapes the raw root"
            ) from exc
        self._baseball_reference_lock_path = (
            resolved_state_directory / "baseball_reference.lock"
        )
        self._statcast_lock_path = resolved_state_directory / "statcast.lock"
        self._baseball_reference_state_path = (
            resolved_state_directory / "baseball_reference.json"
        )

    def fetch(self, request: StatsRequest) -> StatsResponse:
        headers = _merge_headers(self.default_headers, request.headers)
        folded_headers = _headers_casefold(headers)
        if not folded_headers.get("user-agent", ("", ""))[1].strip():
            raise ValueError("statistics requests require a User-Agent header")
        if not folded_headers.get("accept", ("", ""))[1].strip():
            raise ValueError("statistics requests require an Accept header")

        captures: list[RawArtifact] = []
        with self._serial_lock:
            if (
                request.provider is StatsProvider.BASEBALL_REFERENCE
                and request.persistent_cache
            ):
                fingerprint = _request_fingerprint(request)
                try:
                    cached = self.raw_store.load_cached_response(
                        provider=request.provider,
                        request_fingerprint=fingerprint,
                    )
                except RawStoreError as exc:
                    raise StatsTransportError(
                        "baseball_reference persistent cache verification failed",
                        attempts=0,
                    ) from exc
                if cached is not None:
                    return StatsResponse(
                        request=request,
                        status_code=cached.status_code,
                        headers=cached.response_headers,
                        capture=cached.artifact,
                        attempt_captures=(),
                        attempts=0,
                        cache_hit=True,
                    )
            for attempt in range(1, request.max_attempts + 1):
                with self._provider_request_slot(request):
                    try:
                        response = self.session.get(
                            request.url,
                            params=dict(request.params),
                            headers=headers,
                            timeout=request.timeout_seconds,
                            allow_redirects=False,
                            stream=True,
                        )
                    except (requests.Timeout, requests.ConnectionError) as exc:
                        if attempt < request.max_attempts:
                            self._sleep_retry(attempt, None)
                            continue
                        raise StatsTransportError(
                            f"{request.provider.value} request failed after "
                            f"{attempt} attempts: {type(exc).__name__}",
                            attempts=attempt,
                            captures=captures,
                        ) from None
                    except requests.RequestException as exc:
                        raise StatsTransportError(
                            f"{request.provider.value} request failed: "
                            f"{type(exc).__name__}",
                            attempts=attempt,
                            captures=captures,
                        ) from None

                    try:
                        response_headers = _safe_response_headers(response)
                        capture = self.raw_store.retain_stream(
                            provider=request.provider,
                            endpoint_category=request.endpoint_category,
                            chunks=_response_chunks(response),
                            retrieved_at=_aware_utc(self.clock),
                            content_type=_content_type(response_headers),
                        )
                    except (requests.Timeout, requests.ConnectionError) as exc:
                        if attempt < request.max_attempts:
                            self._sleep_retry(attempt, None)
                            continue
                        raise StatsTransportError(
                            f"{request.provider.value} response stream failed after "
                            f"{attempt} attempts: {type(exc).__name__}",
                            attempts=attempt,
                            captures=captures,
                        ) from None
                    finally:
                        close = getattr(response, "close", None)
                        if callable(close):
                            close()
                captures.append(capture)
                try:
                    self._validate_final_url(request, response)
                except StatsTransportError as exc:
                    raise StatsTransportError(
                        str(exc),
                        attempts=attempt,
                        status_code=int(getattr(response, "status_code", 0)),
                        captures=captures,
                    ) from exc
                content_encoding = response_headers.get("content-encoding", "")
                if content_encoding.strip().casefold() not in {"", "identity"}:
                    raise StatsTransportError(
                        f"{request.provider.value} returned an unsupported encoded "
                        "response after exact-byte retention",
                        attempts=attempt,
                        status_code=int(getattr(response, "status_code", 0)),
                        captures=captures,
                    )
                status_code = int(getattr(response, "status_code", 0))
                if 200 <= status_code < 300:
                    return StatsResponse(
                        request=request,
                        status_code=status_code,
                        headers=response_headers,
                        capture=capture,
                        attempt_captures=tuple(captures),
                        attempts=attempt,
                    )
                retryable = _retryable_status(request, status_code)
                if retryable and attempt < request.max_attempts:
                    self._sleep_retry(attempt, response_headers.get("retry-after"))
                    continue
                disposition = (
                    "transient HTTP failure"
                    if retryable
                    else "provider block response"
                    if request.provider is StatsProvider.BASEBALL_REFERENCE
                    and status_code == 429
                    else "permanent HTTP failure"
                )
                raise StatsTransportError(
                    f"{request.provider.value} {disposition}: {status_code}",
                    attempts=attempt,
                    status_code=status_code,
                    captures=captures,
                )

        raise AssertionError("statistics request loop ended unexpectedly")

    def commit_cache(self, response: StatsResponse) -> None:
        """Publish provider-validated Baseball-Reference evidence for later runs."""
        if (
            response.request.provider is not StatsProvider.BASEBALL_REFERENCE
            or not response.request.persistent_cache
            or response.cache_hit
        ):
            return
        try:
            self.raw_store.publish_cached_response(
                provider=response.request.provider,
                request_fingerprint=_request_fingerprint(response.request),
                artifact=response.capture,
                status_code=response.status_code,
                response_headers=response.headers,
            )
        except RawStoreError as exc:
            raise StatsTransportError(
                "baseball_reference persistent cache publication failed",
                attempts=response.attempts,
                status_code=response.status_code,
                captures=response.attempt_captures,
            ) from exc

    @contextmanager
    def _provider_request_slot(self, request: StatsRequest) -> Iterator[None]:
        if request.provider is StatsProvider.BASEBALL_REFERENCE:
            with _ExclusiveFileLock(self._baseball_reference_lock_path):
                self._wait_for_provider_interval(request, persistent=True)
                yield
            return
        if request.provider is StatsProvider.STATCAST:
            with _ExclusiveFileLock(self._statcast_lock_path):
                self._wait_for_provider_interval(request, persistent=False)
                yield
            return
        self._wait_for_provider_interval(request, persistent=False)
        yield

    def _wait_for_provider_interval(
        self,
        request: StatsRequest,
        *,
        persistent: bool,
    ) -> None:
        now = _aware_utc(self.clock)
        previous = (
            self._load_baseball_reference_start()
            if persistent
            else self._last_request_started.get(request.provider.value)
        )
        started_at = now
        if previous is not None:
            elapsed = max(0.0, (now - previous).total_seconds())
            remaining = request.minimum_interval_seconds - elapsed
            if remaining > 0:
                self.sleeper(remaining)
                after_sleep = _aware_utc(self.clock)
                minimum_start = previous + timedelta(
                    seconds=request.minimum_interval_seconds
                )
                started_at = max(after_sleep, minimum_start)
        if persistent:
            self._persist_baseball_reference_start(started_at)
        else:
            self._last_request_started[request.provider.value] = started_at

    def _load_baseball_reference_start(self) -> datetime | None:
        state_path = self._baseball_reference_state_path
        if state_path.is_symlink():
            raise StatsTransportError(
                "baseball_reference rate-limit state must not be a symlink"
            )
        if not state_path.exists():
            return None
        try:
            document = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("state document must be an object")
            if document.get("contract") != _TRANSPORT_RATE_LIMIT_CONTRACT:
                raise ValueError("state contract is unsupported")
            if document.get("provider") != StatsProvider.BASEBALL_REFERENCE.value:
                raise ValueError("state provider is invalid")
            raw_timestamp = document.get("last_request_started_utc")
            if not isinstance(raw_timestamp, str):
                raise ValueError("state timestamp is missing")
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError("state timestamp is not timezone-aware")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise StatsTransportError(
                "baseball_reference rate-limit state is malformed"
            ) from exc
        return timestamp.astimezone(timezone.utc)

    def _persist_baseball_reference_start(self, started_at: datetime) -> None:
        state_path = self._baseball_reference_state_path
        if state_path.is_symlink():
            raise StatsTransportError(
                "baseball_reference rate-limit state must not be a symlink"
            )
        document = {
            "contract": _TRANSPORT_RATE_LIMIT_CONTRACT,
            "last_request_started_utc": started_at.astimezone(timezone.utc).isoformat(),
            "provider": StatsProvider.BASEBALL_REFERENCE.value,
        }
        payload = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".baseball-reference-",
            suffix=".part",
            dir=state_path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, state_path)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise StatsTransportError(
                "baseball_reference rate-limit state could not be persisted"
            ) from exc

    def _sleep_retry(self, attempt: int, retry_after_header: str | None) -> None:
        now = _aware_utc(self.clock)
        delay = _retry_after(retry_after_header, now=now)
        if delay is None:
            index = min(attempt - 1, len(self.retry_backoff_seconds) - 1)
            delay = self.retry_backoff_seconds[index]
        self.sleeper(min(delay, self.retry_after_cap_seconds))

    @staticmethod
    def _validate_final_url(request: StatsRequest, response: Any) -> None:
        final_url = getattr(response, "url", None)
        if not isinstance(final_url, str) or not final_url:
            return
        parsed = urlsplit(final_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise StatsTransportError(
                f"{request.provider.value} redirected outside HTTPS",
                status_code=int(getattr(response, "status_code", 0)),
            )
        requested = urlsplit(request.url)
        if parsed.hostname.casefold() != (requested.hostname or "").casefold():
            raise StatsTransportError(
                f"{request.provider.value} redirected to an unexpected host",
                status_code=int(getattr(response, "status_code", 0)),
            )


class FixtureStatsTransport:
    """Offline transport that retains fixture bytes through the production raw store."""

    def __init__(
        self,
        raw_store: RawArtifactStore,
        fixtures: Mapping[str, FixtureResponse],
        *,
        clock: Clock = utc_now,
    ) -> None:
        self.raw_store = raw_store
        self.fixtures = MappingProxyType(dict(fixtures))
        self.clock = clock
        self.requests: list[StatsRequest] = []

    def fetch(self, request: StatsRequest) -> StatsResponse:
        self.requests.append(request)
        try:
            fixture = self.fixtures[request.fixture_key]
        except KeyError as exc:
            raise StatsTransportError(
                f"No offline fixture is registered for {request.fixture_key!r}"
            ) from exc
        payload = (
            fixture.body.read_bytes()
            if isinstance(fixture.body, Path)
            else fixture.body
        )
        headers = {"content-type": fixture.content_type, **fixture.headers}
        capture = self.raw_store.retain_bytes(
            provider=request.provider,
            endpoint_category=request.endpoint_category,
            payload=payload,
            retrieved_at=_aware_utc(self.clock),
            content_type=fixture.content_type,
        )
        if not 200 <= fixture.status_code < 300:
            raise StatsTransportError(
                f"Offline fixture returned HTTP {fixture.status_code}",
                attempts=1,
                status_code=fixture.status_code,
                captures=(capture,),
            )
        return StatsResponse(
            request=request,
            status_code=fixture.status_code,
            headers=headers,
            capture=capture,
            attempt_captures=(capture,),
            attempts=1,
        )
