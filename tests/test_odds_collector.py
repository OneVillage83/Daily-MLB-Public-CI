from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

import pytest
import requests

import app.http as http_module
from app.collectors.odds_collector import BASE_URL, OddsCollector, OddsPayloadError
from app.config import Settings
from app.http import HttpClient, HttpError, RetryableHttpError

SECRET = "odds-collector-secret"


@dataclass
class RecordedCall:
    url: str
    params: dict[str, Any] | None
    headers: dict[str, str] | None
    timeout: int


class SequenceSession:
    def __init__(self, items: list[requests.Response | requests.RequestException]) -> None:
        self.items = items
        self.calls: list[RecordedCall] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int,
    ) -> requests.Response:
        self.calls.append(RecordedCall(url, params, headers, timeout))
        item = self.items.pop(0)
        if isinstance(item, requests.RequestException):
            raise item
        return item


def make_response(
    status: int,
    payload: object,
    *,
    headers: dict[str, str] | None = None,
    url: str = BASE_URL,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = url
    response.headers.update(headers or {})
    response._content = json.dumps(payload).encode("utf-8")
    response.encoding = "utf-8"
    return response


def valid_event() -> dict[str, Any]:
    return {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-07-12T20:10:00Z",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": "2026-07-12T20:00:00Z",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-07-12T20:01:00Z",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 8.5},
                            {"name": "Under", "price": -105, "point": 8.5},
                        ],
                    }
                ],
            }
        ],
    }


def collector_for(
    items: list[requests.Response | requests.RequestException],
    *,
    timeout: int = 17,
    max_attempts: int = 3,
    retry_max_seconds: float = 30,
) -> tuple[OddsCollector, SequenceSession]:
    http = HttpClient(
        timeout=timeout,
        max_attempts=max_attempts,
        retry_max_seconds=retry_max_seconds,
    )
    session = SequenceSession(items)
    http.session = cast(requests.Session, session)
    settings = Settings(
        odds_api_key=SECRET,
        odds_regions="us,us2",
        odds_markets="h2h,spreads,totals",
    )
    return OddsCollector(settings, http), session


def test_valid_list_payload_preserves_provider_fields_and_request_metadata() -> None:
    event = valid_event()
    collector, session = collector_for(
        [
            make_response(
                200,
                [event],
                headers={
                    "Date": "Sun, 12 Jul 2026 20:02:00 GMT",
                    "Content-Type": "application/json",
                    "x-requests-remaining": "498",
                    "x-requests-used": "2",
                    "x-requests-last": "1",
                },
            )
        ]
    )

    result = collector.collect()

    assert result.games == [event]
    assert result.games[0]["commence_time"] == "2026-07-12T20:10:00Z"
    book = result.games[0]["bookmakers"][0]
    assert (book["key"], book["title"], book["last_update"]) == (
        "book-a",
        "Book A",
        "2026-07-12T20:00:00Z",
    )
    assert book["markets"][0]["last_update"] == "2026-07-12T20:01:00Z"
    assert result.capture.payload == [event]
    assert result.capture.provider_timestamp is None
    assert result.request.response_date_utc == "2026-07-12T20:02:00+00:00"
    assert result.request.status_code == 200
    assert result.request.attempts == 1
    assert result.request.retries_performed == 0
    assert result.request.duration_seconds >= 0
    assert result.quota == {
        "requests_remaining": "498",
        "requests_used": "2",
        "requests_last": "1",
    }
    assert result.warnings == ()
    call = session.calls[0]
    assert call.url == BASE_URL
    assert call.timeout == 17
    assert call.params == {
        "apiKey": SECRET,
        "regions": "us,us2",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }


def test_empty_list_is_a_valid_empty_slate() -> None:
    collector, _ = collector_for([make_response(200, [])])

    result = collector.collect()

    assert result.games == []
    assert result.capture.payload == []
    assert result.warnings == ()


def test_non_list_payload_is_fatal_and_retained_on_exception() -> None:
    payload = {"message": "unexpected object"}
    collector, _ = collector_for([make_response(200, payload)])

    with pytest.raises(OddsPayloadError, match="non-list payload") as captured:
        collector.collect()

    assert captured.value.capture.payload == payload
    assert captured.value.capture.provider_timestamp is None
    assert captured.value.request.request_status == "success"


def test_nonempty_all_invalid_payload_is_fatal() -> None:
    payload: list[Any] = [{"id": "broken"}, "not-an-event"]
    collector, _ = collector_for([make_response(200, payload)])

    with pytest.raises(OddsPayloadError, match="no structurally valid events") as captured:
        collector.collect()

    assert captured.value.capture.payload == payload
    assert [warning.code for warning in captured.value.warnings] == [
        "malformed_event",
        "malformed_event",
    ]


def test_malformed_event_is_excluded_with_warning_when_another_event_is_valid() -> None:
    valid = valid_event()
    collector, _ = collector_for([make_response(200, [{"id": "bad-event"}, valid])])

    result = collector.collect()

    assert result.games == [valid]
    assert len(result.warnings) == 1
    assert result.warnings[0].code == "malformed_event"
    assert result.warnings[0].event_id == "bad-event"


def test_malformed_bookmaker_is_excluded_but_raw_payload_is_unchanged() -> None:
    event = valid_event()
    malformed = {"key": "broken-book", "title": "Broken", "markets": "not-a-list"}
    event["bookmakers"].append(malformed)
    collector, _ = collector_for([make_response(200, [event])])

    result = collector.collect()

    assert [book["key"] for book in result.games[0]["bookmakers"]] == ["book-a"]
    assert result.capture.payload == [event]
    assert result.warnings[0].code == "malformed_bookmaker"
    assert result.warnings[0].bookmaker == "broken-book"


def test_malformed_market_and_outcome_are_excluded_with_specific_warnings() -> None:
    event = valid_event()
    book = event["bookmakers"][0]
    book["markets"].append({"key": "broken-market", "outcomes": "not-a-list"})
    book["markets"][0]["outcomes"].append({"name": "Over", "price": "bad", "point": 9.0})
    collector, _ = collector_for([make_response(200, [event])])

    result = collector.collect()

    markets = result.games[0]["bookmakers"][0]["markets"]
    assert [market["key"] for market in markets] == ["totals"]
    assert len(markets[0]["outcomes"]) == 2
    assert [warning.code for warning in result.warnings] == [
        "malformed_outcome",
        "malformed_market",
    ]


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_statuses_are_retried(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    headers = {"Retry-After": "4"} if status in {429, 503} else {}
    collector, session = collector_for(
        [make_response(status, {}, headers=headers), make_response(200, [])]
    )

    result = collector.collect()

    assert len(session.calls) == 2
    assert result.request.attempts == 2
    assert result.request.retries_performed == 1
    assert sleeps == [4.0 if status in {429, 503} else 1.0]


def test_connection_and_timeout_failures_are_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    collector, session = collector_for(
        [requests.ConnectionError("secret body omitted"), requests.Timeout("timeout"), make_response(200, [])]
    )

    result = collector.collect()

    assert len(session.calls) == 3
    assert result.request.attempts == 3
    assert result.request.retries_performed == 2
    assert sleeps == [1.0, 2.0]


def test_retry_after_is_capped_at_thirty_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    collector, _ = collector_for(
        [make_response(429, {}, headers={"Retry-After": "99"}), make_response(200, [])]
    )

    collector.collect()

    assert sleeps == [30.0]


def test_attempt_count_and_retry_cap_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    collector, session = collector_for(
        [make_response(429, {}, headers={"Retry-After": "99"}), make_response(429, {})],
        max_attempts=2,
        retry_max_seconds=7,
    )

    with pytest.raises(RetryableHttpError) as captured:
        collector.collect()

    assert len(session.calls) == 2
    assert sleeps == [7.0]
    assert captured.value.diagnostics is not None
    assert captured.value.diagnostics.attempts == 2
    assert captured.value.diagnostics.retries_performed == 1


def test_retry_after_http_date_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        http_module,
        "_utc_now",
        lambda: datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc),
    )
    collector, _ = collector_for(
        [
            make_response(
                503,
                {},
                headers={"Retry-After": "Sun, 12 Jul 2026 20:00:12 GMT"},
            ),
            make_response(200, []),
        ]
    )

    collector.collect()

    assert sleeps == [12.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_statuses_are_not_retried(
    status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    collector, session = collector_for(
        [
            make_response(
                status,
                {"message": SECRET},
                headers={"x-requests-remaining": "88"},
            )
        ]
    )

    with pytest.raises(HttpError) as captured:
        collector.collect()

    assert len(session.calls) == 1
    assert sleeps == []
    assert captured.value.diagnostics is not None
    assert captured.value.diagnostics.attempts == 1
    assert captured.value.diagnostics.quota_headers["requests_remaining"] == "88"


def test_exhausted_transient_error_reports_retries_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)
    url = f"{BASE_URL}?apiKey={SECRET}&regions=us"
    collector, session = collector_for(
        [make_response(503, {}, url=url), make_response(503, {}, url=url), make_response(503, {}, url=url)]
    )

    with pytest.raises(RetryableHttpError) as captured:
        collector.collect()

    assert len(session.calls) == 3
    assert SECRET not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)
    assert captured.value.diagnostics is not None
    assert captured.value.diagnostics.attempts == 3
    assert captured.value.diagnostics.retries_performed == 2


def test_invalid_json_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(http_module.time, "sleep", sleeps.append)
    response = make_response(200, {})
    response._content = b"not-json"
    collector, session = collector_for([response])

    with pytest.raises(HttpError, match="Invalid JSON"):
        collector.collect()

    assert len(session.calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("regions", "markets", "message"),
    [
        ("", "h2h", "ODDS_REGIONS"),
        ("us,,eu", "h2h", "ODDS_REGIONS"),
        ("us,us", "h2h", "ODDS_REGIONS"),
        ("us", "", "ODDS_MARKETS"),
        ("us", "h2h,,totals", "ODDS_MARKETS"),
        ("us", "h2h,player_props", "ODDS_MARKETS"),
        ("us", "h2h,h2h", "ODDS_MARKETS"),
    ],
)
def test_invalid_request_configuration_fails_before_http(
    regions: str,
    markets: str,
    message: str,
) -> None:
    http = HttpClient(timeout=17)
    session = SequenceSession([])
    http.session = cast(requests.Session, session)
    collector = OddsCollector(
        Settings(odds_api_key=SECRET, odds_regions=regions, odds_markets=markets),
        http,
    )

    with pytest.raises(ValueError, match=message):
        collector.collect()

    assert session.calls == []


def test_non_american_odds_format_fails_before_http() -> None:
    http = HttpClient(timeout=17)
    session = SequenceSession([])
    http.session = cast(requests.Session, session)
    collector = OddsCollector(
        Settings(odds_api_key=SECRET, odds_format="decimal"),
        http,
    )

    with pytest.raises(ValueError, match="ODDS_FORMAT must be american"):
        collector.collect()

    assert session.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"max_attempts": 0},
        {"retry_max_seconds": -1},
        {"retry_max_seconds": float("nan")},
    ],
)
def test_http_retry_configuration_is_validated(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HttpClient(**kwargs)
