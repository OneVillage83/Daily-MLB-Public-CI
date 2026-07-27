from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, TypeAlias
from urllib.parse import urlsplit

_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")

RequestParameter: TypeAlias = str | int | float | bool
CsvRow: TypeAlias = Mapping[str, str]


class StatsProvider(StrEnum):
    MLB = "mlb"
    RETROSHEET = "retrosheet"
    BASEBALL_REFERENCE = "baseball_reference"
    STATCAST = "statcast"
    PYBASEBALL = "pybaseball"


class StatsError(RuntimeError):
    """Base error for the controlled statistics acquisition boundary."""


class StatsRequestError(StatsError):
    pass


class StatsTransportError(StatsError):
    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        status_code: int | None = None,
        captures: Iterable[RawArtifact] = (),
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code
        self.captures = tuple(captures)


class StatsProviderPayloadError(StatsError):
    def __init__(self, message: str, *, capture: RawArtifact) -> None:
        super().__init__(message)
        self.capture = capture


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _frozen_strings(values: Mapping[str, str]) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for key, value in values.items():
        name = str(key).strip()
        if not name:
            raise ValueError("mapping keys must not be blank")
        normalized_value = str(value)
        if any(character in name or character in normalized_value for character in "\r\n"):
            raise ValueError("mapping keys and values must be single-line strings")
        normalized[name] = normalized_value
    return MappingProxyType(normalized)


def _frozen_params(
    values: Mapping[str, RequestParameter],
) -> Mapping[str, RequestParameter]:
    normalized: dict[str, RequestParameter] = {}
    for key, value in values.items():
        name = str(key).strip()
        if not name:
            raise ValueError("request parameter names must not be blank")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"request parameter {name!r} must be finite")
        normalized[name] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class StatsRequest:
    provider: StatsProvider
    endpoint_category: str
    url: str
    fixture_key: str
    params: Mapping[str, RequestParameter] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    minimum_interval_seconds: float = 0.0
    persistent_cache: bool = False

    def __post_init__(self) -> None:
        endpoint = self.endpoint_category.strip()
        if _IDENTIFIER_RE.fullmatch(endpoint) is None:
            raise ValueError("endpoint_category must be a lowercase safe identifier")
        fixture_key = self.fixture_key.strip()
        if not fixture_key or len(fixture_key) > 200:
            raise ValueError("fixture_key must be between 1 and 200 characters")

        parsed = urlsplit(self.url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise StatsRequestError("statistics provider requests require an HTTPS URL")
        if parsed.username is not None or parsed.password is not None:
            raise StatsRequestError("statistics provider URLs must not contain credentials")
        if parsed.query or parsed.fragment:
            raise StatsRequestError(
                "statistics provider URL query values must be supplied as request parameters"
            )
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if not isinstance(self.persistent_cache, bool):
            raise TypeError("persistent_cache must be a boolean")
        if (
            not math.isfinite(self.minimum_interval_seconds)
            or self.minimum_interval_seconds < 0
        ):
            raise ValueError("minimum_interval_seconds must be finite and non-negative")
        if (
            self.provider is StatsProvider.BASEBALL_REFERENCE
            and self.minimum_interval_seconds < 6.0
        ):
            raise ValueError(
                "Baseball-Reference requests require at least six seconds between starts"
            )

        object.__setattr__(self, "endpoint_category", endpoint)
        object.__setattr__(self, "fixture_key", fixture_key)
        object.__setattr__(self, "params", _frozen_params(self.params))
        object.__setattr__(self, "headers", _frozen_strings(self.headers))


@dataclass(frozen=True, slots=True)
class RawArtifact:
    capture_id: str
    provider: StatsProvider
    endpoint_category: str
    retrieved_at: datetime
    content_type: str
    checksum_sha256: str
    size_bytes: int
    path: Path
    metadata_path: Path
    parent_checksum_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "retrieved_at", _utc_datetime(self.retrieved_at, "retrieved_at")
        )


@dataclass(frozen=True, slots=True)
class StatsResponse:
    request: StatsRequest
    status_code: int
    headers: Mapping[str, str]
    capture: RawArtifact
    attempt_captures: tuple[RawArtifact, ...]
    attempts: int
    cache_hit: bool = False

    def __post_init__(self) -> None:
        if self.attempts < 0:
            raise ValueError("statistics response attempts must not be negative")
        if self.cache_hit and self.attempts != 0:
            raise ValueError("a cached statistics response cannot report HTTP attempts")
        object.__setattr__(self, "headers", _frozen_strings(self.headers))


@dataclass(frozen=True, slots=True)
class FixtureResponse:
    body: bytes | Path
    content_type: str
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.body, (bytes, Path)):
            raise TypeError("fixture body must be bytes or a pathlib.Path")
        if not self.content_type.strip():
            raise ValueError("fixture content_type must not be blank")
        object.__setattr__(self, "headers", _frozen_strings(self.headers))


class StatsTransport(Protocol):
    def fetch(self, request: StatsRequest) -> StatsResponse: ...


_PYBASEBALL_227_STATCAST_PARAMS: Mapping[str, RequestParameter] = MappingProxyType(
    {
        "all": "true",
        "hfPT": "",
        "hfAB": "",
        "hfBBT": "",
        "hfPR": "",
        "hfZ": "",
        "stadium": "",
        "hfBBL": "",
        "hfNewZones": "",
        "hfGT": "R|PO|S|",
        "hfSea": "",
        "hfSit": "",
        "hfOuts": "",
        "opponent": "",
        "pitcher_throws": "",
        "batter_stands": "",
        "hfSA": "",
        "team": "",
        "position": "",
        "hfRO": "",
        "home_road": "",
        "hfFlag": "",
        "metric_1": "",
        "hfInn": "",
        "min_pitches": 0,
        "min_results": 0,
        "group_by": "name",
        "sort_col": "pitches",
        "player_event_sort": "h_launch_speed",
        "sort_order": "desc",
        "min_abs": 0,
    }
)


@dataclass(frozen=True, slots=True)
class StatcastQuery:
    start_date: date
    end_date: date
    player_type: str = "pitcher"
    extra_params: Mapping[str, RequestParameter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("Statcast end_date must not precede start_date")
        player_type = self.player_type.strip().casefold()
        if player_type not in {"batter", "pitcher"}:
            raise ValueError("Statcast player_type must be 'batter' or 'pitcher'")
        reserved = {
            *_PYBASEBALL_227_STATCAST_PARAMS,
            "game_date_gt",
            "game_date_lt",
            "player_type",
            "type",
        }
        collisions = reserved.intersection(self.extra_params)
        if collisions:
            raise ValueError(
                "Statcast extra_params may not replace controlled parameters: "
                + ", ".join(sorted(collisions))
            )
        object.__setattr__(self, "player_type", player_type)
        object.__setattr__(self, "extra_params", _frozen_params(self.extra_params))

    def request_params(self) -> Mapping[str, RequestParameter]:
        return MappingProxyType(
            {
                **_PYBASEBALL_227_STATCAST_PARAMS,
                "type": "details",
                "game_date_gt": self.start_date.isoformat(),
                "game_date_lt": self.end_date.isoformat(),
                "player_type": self.player_type,
                **self.extra_params,
            }
        )


class PybaseballParityHook(Protocol):
    """Optional comparison hook; its return value cannot replace Statcast rows."""

    def __call__(
        self,
        query: StatcastQuery,
        rows: tuple[CsvRow, ...],
        raw_checksum_sha256: str,
    ) -> Mapping[str, Any] | None: ...


Clock: TypeAlias = Callable[[], datetime]
Sleeper: TypeAlias = Callable[[float], None]
CaptureIdFactory: TypeAlias = Callable[[], str]
