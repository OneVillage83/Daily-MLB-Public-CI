from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import requests

from app.redaction import redact_url

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_MAX_SECONDS = 30.0
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_RETRY_AFTER_STATUS_CODES = frozenset({429, 503})


@dataclass(frozen=True, slots=True)
class HttpRequestDiagnostics:
    request_status: Literal["success", "failed"]
    status_code: int | None
    attempts: int
    retries_performed: int
    duration_seconds: float
    response_date_utc: str | None
    quota_headers: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class JsonHttpResult:
    payload: dict[str, Any] | list[Any]
    response: requests.Response
    diagnostics: HttpRequestDiagnostics


class HttpError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        diagnostics: HttpRequestDiagnostics | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class RetryableHttpError(HttpError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _response_header(response: requests.Response, name: str) -> str | None:
    value = response.headers.get(name)
    return str(value) if value is not None else None


def _quota_headers(response: requests.Response) -> dict[str, str | None]:
    return {
        "requests_remaining": _response_header(response, "x-requests-remaining"),
        "requests_used": _response_header(response, "x-requests-used"),
        "requests_last": _response_header(response, "x-requests-last"),
    }


def _http_date_utc(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _retry_after_seconds(value: str | None, *, maximum: float) -> float | None:
    if value is None:
        return None
    source = value.strip()
    if not source:
        return None
    try:
        delay = float(source)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(source)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        delay = (retry_at.astimezone(timezone.utc) - _utc_now()).total_seconds()
    if not math.isfinite(delay):
        return None
    if delay < 0:
        return 0.0
    return min(delay, maximum)


def _backoff_seconds(attempt: int) -> float:
    return float(min(2 ** (attempt - 1), 8))


class HttpClient:
    def __init__(
        self,
        timeout: int = 30,
        *,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_max_seconds: float = _DEFAULT_RETRY_MAX_SECONDS,
    ):
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if not math.isfinite(retry_max_seconds) or retry_max_seconds < 0:
            raise ValueError("retry_max_seconds must be a finite non-negative number")
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_max_seconds = retry_max_seconds
        self.session = requests.Session()

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[dict[str, Any] | list[Any], requests.Response]:
        """Return the legacy payload/response tuple used by existing collectors."""

        result = self.get_json_with_diagnostics(url, params=params, headers=headers)
        return result.payload, result.response

    def get_json_with_diagnostics(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> JsonHttpResult:
        started = time.monotonic()
        try:
            prepared_url = requests.Request("GET", url, params=params).prepare().url or url
        except requests.RequestException:
            prepared_url = url
        safe_url = redact_url(prepared_url)

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self.max_attempts:
                    time.sleep(_backoff_seconds(attempt))
                    continue
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=None,
                    attempts=attempt,
                    response_date_utc=None,
                )
                raise RetryableHttpError(
                    f"Request failed for {safe_url}: {type(exc).__name__}",
                    diagnostics=diagnostics,
                ) from None
            except requests.RequestException as exc:
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=None,
                    attempts=attempt,
                    response_date_utc=None,
                )
                raise HttpError(
                    f"Request failed for {safe_url}: {type(exc).__name__}",
                    diagnostics=diagnostics,
                ) from None

            response_date_utc = _http_date_utc(_response_header(response, "Date"))
            response_url = redact_url(response.url or prepared_url)
            if response.status_code in _RETRYABLE_STATUS_CODES:
                if attempt < self.max_attempts:
                    delay = None
                    if response.status_code in _RETRY_AFTER_STATUS_CODES:
                        delay = _retry_after_seconds(
                            _response_header(response, "Retry-After"),
                            maximum=self.retry_max_seconds,
                        )
                    time.sleep(delay if delay is not None else _backoff_seconds(attempt))
                    continue
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=response.status_code,
                    attempts=attempt,
                    response_date_utc=response_date_utc,
                    quota_headers=_quota_headers(response),
                )
                raise RetryableHttpError(
                    f"Temporary HTTP {response.status_code} from {response_url}",
                    diagnostics=diagnostics,
                )
            if not response.ok:
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=response.status_code,
                    attempts=attempt,
                    response_date_utc=response_date_utc,
                    quota_headers=_quota_headers(response),
                )
                raise HttpError(
                    f"HTTP {response.status_code} from {response_url}",
                    diagnostics=diagnostics,
                )
            try:
                payload = response.json()
            except requests.exceptions.JSONDecodeError:
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=response.status_code,
                    attempts=attempt,
                    response_date_utc=response_date_utc,
                    quota_headers=_quota_headers(response),
                )
                raise HttpError(
                    f"Invalid JSON response from {response_url}",
                    diagnostics=diagnostics,
                ) from None
            if not isinstance(payload, (dict, list)):
                diagnostics = self._diagnostics(
                    started=started,
                    request_status="failed",
                    status_code=response.status_code,
                    attempts=attempt,
                    response_date_utc=response_date_utc,
                    quota_headers=_quota_headers(response),
                )
                raise HttpError(
                    f"Unexpected JSON payload type from {response_url}",
                    diagnostics=diagnostics,
                )
            diagnostics = self._diagnostics(
                started=started,
                request_status="success",
                status_code=response.status_code,
                attempts=attempt,
                response_date_utc=response_date_utc,
                quota_headers=_quota_headers(response),
            )
            return JsonHttpResult(payload, response, diagnostics)

        raise AssertionError("HTTP attempt loop ended unexpectedly")

    @staticmethod
    def _diagnostics(
        *,
        started: float,
        request_status: Literal["success", "failed"],
        status_code: int | None,
        attempts: int,
        response_date_utc: str | None,
        quota_headers: dict[str, str | None] | None = None,
    ) -> HttpRequestDiagnostics:
        return HttpRequestDiagnostics(
            request_status=request_status,
            status_code=status_code,
            attempts=attempts,
            retries_performed=max(0, attempts - 1),
            duration_seconds=max(0.0, time.monotonic() - started),
            response_date_utc=response_date_utc,
            quota_headers=quota_headers or {},
        )
