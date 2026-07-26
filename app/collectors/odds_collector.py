from __future__ import annotations

import math
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from app.config import Settings
from app.http import HttpClient, HttpRequestDiagnostics, JsonHttpResult
from app.raw_payloads import (
    RawPayloadCapture,
    capture_response,
    normalize_provider_timestamp,
    response_header,
    utc_now_iso,
)
from app.redaction import redact_text

BASE_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
COLLECTOR_VERSION = "odds-collector-v3"
_SUPPORTED_MARKETS = frozenset({"h2h", "spreads", "totals"})
_REGION_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")

OddsCollectorWarningCode = Literal[
    "malformed_event",
    "malformed_bookmaker",
    "malformed_market",
    "malformed_outcome",
]


@dataclass(frozen=True, slots=True)
class OddsCollectorWarning:
    code: OddsCollectorWarningCode
    message: str
    created_at: str
    event_id: str | None = None
    bookmaker: str | None = None
    market: str | None = None


@dataclass(frozen=True, slots=True)
class OddsCollectionResult:
    games: list[dict[str, Any]]
    quota: dict[str, str | None]
    capture: RawPayloadCapture
    request: HttpRequestDiagnostics
    warnings: tuple[OddsCollectorWarning, ...]
    collector_version: str = COLLECTOR_VERSION

    def __iter__(self) -> Iterator[Any]:
        """Keep legacy three-value unpacking during the output-contract migration."""

        yield self.games
        yield self.quota
        yield self.capture


class OddsPayloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        capture: RawPayloadCapture,
        request: HttpRequestDiagnostics,
        warnings: tuple[OddsCollectorWarning, ...] = (),
        quota: dict[str, str | None] | None = None,
    ) -> None:
        super().__init__(message)
        self.capture = capture
        self.request = request
        self.warnings = warnings
        self.quota = quota or {}


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _valid_commence_time(value: object) -> bool:
    if not _nonempty_string(value):
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


class OddsCollector:
    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.http = http

    def collect(
        self,
    ) -> OddsCollectionResult:
        if self.settings.odds_format != "american":
            raise ValueError("ODDS_FORMAT must be american during Phase 1")
        regions = self._configured_regions()
        markets = self._configured_markets()
        params = {
            "apiKey": self.settings.odds_api_key,
            "regions": ",".join(regions),
            "markets": ",".join(markets),
            "oddsFormat": self.settings.odds_format,
            "dateFormat": "iso",
        }
        result = self._request(params)
        payload = result.payload
        response = result.response
        capture = capture_response(
            provider="the_odds_api",
            endpoint_category="mlb_odds",
            payload=payload,
            response=response,
            use_response_date_as_provider_timestamp=False,
        )
        quota = {
            "requests_remaining": response.headers.get("x-requests-remaining"),
            "requests_used": response.headers.get("x-requests-used"),
            "requests_last": response.headers.get("x-requests-last"),
        }
        if not isinstance(payload, list):
            raise OddsPayloadError(
                "The Odds API returned a non-list payload",
                capture=capture,
                request=result.diagnostics,
                quota=quota,
            )

        games, warnings = self._validated_games(payload)
        if payload and not games:
            raise OddsPayloadError(
                "The Odds API payload contained no structurally valid events",
                capture=capture,
                request=result.diagnostics,
                warnings=tuple(warnings),
                quota=quota,
            )
        return OddsCollectionResult(
            games=games,
            quota=quota,
            capture=capture,
            request=result.diagnostics,
            warnings=tuple(warnings),
        )

    def _configured_regions(self) -> tuple[str, ...]:
        regions = tuple(part.strip() for part in self.settings.odds_regions.split(","))
        if (
            not regions
            or any(not region or _REGION_TOKEN_RE.fullmatch(region) is None for region in regions)
            or len(set(regions)) != len(regions)
        ):
            raise ValueError("ODDS_REGIONS must contain unique, nonempty region tokens")
        return regions

    def _configured_markets(self) -> tuple[str, ...]:
        markets = tuple(part.strip() for part in self.settings.odds_markets.split(","))
        if (
            not markets
            or any(not market or market not in _SUPPORTED_MARKETS for market in markets)
            or len(set(markets)) != len(markets)
        ):
            raise ValueError(
                "ODDS_MARKETS must contain a unique, nonempty subset of h2h, spreads, and totals"
            )
        return markets

    def _request(self, params: dict[str, Any]) -> JsonHttpResult:
        diagnostic_get = getattr(self.http, "get_json_with_diagnostics", None)
        if callable(diagnostic_get):
            result: JsonHttpResult = diagnostic_get(BASE_URL, params=params)
            return result

        # Compatibility for small existing test doubles that implement only get_json.
        started = time.monotonic()
        payload, response = self.http.get_json(BASE_URL, params=params)
        response_date_utc = normalize_provider_timestamp(response_header(response, "Date"))
        status_code = getattr(response, "status_code", 200)
        diagnostics = HttpRequestDiagnostics(
            request_status="success",
            status_code=status_code if isinstance(status_code, int) else 200,
            attempts=1,
            retries_performed=0,
            duration_seconds=max(0.0, time.monotonic() - started),
            response_date_utc=response_date_utc,
        )
        return JsonHttpResult(payload, response, diagnostics)

    def _validated_games(
        self,
        payload: list[Any],
    ) -> tuple[list[dict[str, Any]], list[OddsCollectorWarning]]:
        games: list[dict[str, Any]] = []
        warnings: list[OddsCollectorWarning] = []
        for raw_event in payload:
            event_id = self._context(raw_event.get("id")) if isinstance(raw_event, dict) else None
            if not self._valid_event(raw_event):
                warnings.append(
                    self._warning(
                        "malformed_event",
                        "Provider event did not match the required Odds API event shape",
                        event_id=event_id,
                    )
                )
                continue

            event = dict(raw_event)
            valid_bookmakers: list[dict[str, Any]] = []
            for raw_bookmaker in raw_event["bookmakers"]:
                bookmaker = (
                    self._context(raw_bookmaker.get("key"))
                    if isinstance(raw_bookmaker, dict)
                    else None
                )
                if not self._valid_bookmaker(raw_bookmaker):
                    warnings.append(
                        self._warning(
                            "malformed_bookmaker",
                            "Provider bookmaker did not match the required Odds API bookmaker shape",
                            event_id=event_id,
                            bookmaker=bookmaker,
                        )
                    )
                    continue

                valid_markets: list[dict[str, Any]] = []
                for raw_market in raw_bookmaker["markets"]:
                    market_key = (
                        self._context(raw_market.get("key"))
                        if isinstance(raw_market, dict)
                        else None
                    )
                    if not self._valid_market(raw_market):
                        warnings.append(
                            self._warning(
                                "malformed_market",
                                "Provider market did not match the required Odds API market shape",
                                event_id=event_id,
                                bookmaker=bookmaker,
                                market=market_key,
                            )
                        )
                        continue

                    valid_outcomes: list[dict[str, Any]] = []
                    for raw_outcome in raw_market["outcomes"]:
                        if not self._valid_outcome(raw_outcome):
                            warnings.append(
                                self._warning(
                                    "malformed_outcome",
                                    "Provider outcome did not match the required Odds API outcome shape",
                                    event_id=event_id,
                                    bookmaker=bookmaker,
                                    market=market_key,
                                )
                            )
                            continue
                        valid_outcomes.append(dict(raw_outcome))

                    market = dict(raw_market)
                    market["outcomes"] = valid_outcomes
                    valid_markets.append(market)

                book = dict(raw_bookmaker)
                book["markets"] = valid_markets
                valid_bookmakers.append(book)

            event["bookmakers"] = valid_bookmakers
            games.append(event)
        return games, warnings

    @staticmethod
    def _valid_event(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        home_team = value.get("home_team")
        away_team = value.get("away_team")
        return (
            _nonempty_string(value.get("id"))
            and value.get("sport_key") == "baseball_mlb"
            and _valid_commence_time(value.get("commence_time"))
            and _nonempty_string(home_team)
            and _nonempty_string(away_team)
            and home_team != away_team
            and isinstance(value.get("bookmakers"), list)
        )

    @staticmethod
    def _valid_bookmaker(value: object) -> bool:
        return (
            isinstance(value, dict)
            and _nonempty_string(value.get("key"))
            and _nonempty_string(value.get("title"))
            and isinstance(value.get("markets"), list)
        )

    @staticmethod
    def _valid_market(value: object) -> bool:
        return (
            isinstance(value, dict)
            and _nonempty_string(value.get("key"))
            and isinstance(value.get("outcomes"), list)
        )

    @staticmethod
    def _valid_outcome(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        point = value.get("point")
        return (
            _nonempty_string(value.get("name"))
            and _finite_number(value.get("price"))
            and (point is None or _finite_number(point))
        )

    def _warning(
        self,
        code: OddsCollectorWarningCode,
        message: str,
        *,
        event_id: str | None = None,
        bookmaker: str | None = None,
        market: str | None = None,
    ) -> OddsCollectorWarning:
        return OddsCollectorWarning(
            code=code,
            message=redact_text(message, self.settings.credential_values()),
            created_at=utc_now_iso(),
            event_id=event_id,
            bookmaker=bookmaker,
            market=market,
        )

    def _context(self, value: object) -> str | None:
        if not _nonempty_string(value):
            return None
        return redact_text(str(value), self.settings.credential_values())
