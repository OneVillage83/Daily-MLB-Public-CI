from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date
from app.processors.odds_processor import (
    CALCULATION_VERSION,
    ODDS_CONSENSUS_CONTRACT_VERSION,
)
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS, team_key

ODDS_WEATHER_CONTRACT_VERSION = "DSE_ODDS_WEATHER_V1"
ODDS_PROVIDER_EVENT_CONTRACT_VERSION = "DSE_ODDS_PROVIDER_EVENT_V1"
WEATHER_FORECAST_CONTRACT_VERSION = "DSE_WEATHER_FORECAST_V1"
VENUE_WEATHER_CONTEXT_CONTRACT_VERSION = "DSE_VENUE_WEATHER_CONTEXT_V1"
ODDS_WEATHER_SPORT = "MLB"
ODDS_WEATHER_LEAGUE = "MLB"
MAX_WEATHER_FORECAST_OFFSET_MINUTES = 60.0


class OddsWeatherContractError(ValueError):
    """Raised when a canonical Odds + Weather V1 contract is invalid."""


class WeatherProvider(StrEnum):
    NWS = "nws"
    OPENWEATHER = "openweather"


class WeatherStatus(StrEnum):
    AVAILABLE = "available"
    INDOOR_FIXED_ROOF = "indoor_fixed_roof"
    UNAVAILABLE = "unavailable"


class WeatherRelevance(StrEnum):
    DIRECT = "direct"
    CONTEXTUAL_ROOF_STATUS_UNKNOWN = "contextual_roof_status_unknown"
    CONTEXTUAL_ROOF_TYPE_UNVERIFIED = "contextual_roof_type_unverified"
    INDOOR_SUPPRESSED = "indoor_suppressed"
    UNAVAILABLE = "unavailable"


class OddsAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class OddsWeatherWarningDomain(StrEnum):
    ODDS_MATCHING = "odds_matching"
    ODDS = "odds"
    WEATHER = "weather"
    STADIUM = "stadium"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OddsWeatherContractError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _aware_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise OddsWeatherContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_aware(value: object, name: str) -> datetime:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsWeatherContractError(f"{name} must be ISO-8601") from exc
    return _aware_utc(parsed, name)


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise OddsWeatherContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


def _finite_optional(
    value: object,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    maximum_inclusive: bool = True,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise OddsWeatherContractError(f"{name} must be finite numeric when present")
    result = float(value)
    if not math.isfinite(result):
        raise OddsWeatherContractError(f"{name} must be finite numeric when present")
    if minimum is not None and result < minimum:
        raise OddsWeatherContractError(f"{name} is below its valid range")
    if maximum is not None:
        valid = result <= maximum if maximum_inclusive else result < maximum
        if not valid:
            raise OddsWeatherContractError(f"{name} is above its valid range")
    return result


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            raise OddsWeatherContractError("JSON payload contains non-finite numeric value")
        return value
    raise OddsWeatherContractError("payload must contain only JSON-compatible values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OddsWeatherContractError(f"{name} must be a mapping")
    return value


@dataclass(frozen=True, slots=True)
class OddsProviderEventV1:
    """Retained, structurally validated Odds API event evidence for fixture-first assembly."""

    provider_event_id: str
    retrieved_at: datetime
    raw_capture_checksum: str
    event: Mapping[str, Any]
    history_rows: tuple[Mapping[str, Any], ...] = ()
    contract_version: str = ODDS_PROVIDER_EVENT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_event_id",
            _required_text(self.provider_event_id, "provider_event_id"),
        )
        object.__setattr__(
            self,
            "retrieved_at",
            _aware_utc(self.retrieved_at, "odds retrieved_at"),
        )
        object.__setattr__(
            self,
            "raw_capture_checksum",
            _sha256(self.raw_capture_checksum, "raw_capture_checksum"),
        )
        if self.contract_version != ODDS_PROVIDER_EVENT_CONTRACT_VERSION:
            raise OddsWeatherContractError("unsupported odds provider event contract")
        raw = _mapping(self.event, "event")
        frozen = _freeze_json(dict(raw))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "event", frozen)
        if str(frozen.get("id") or "") != self.provider_event_id:
            raise OddsWeatherContractError("provider event payload id mismatch")
        if frozen.get("sport_key") != "baseball_mlb":
            raise OddsWeatherContractError("provider event sport_key must be baseball_mlb")
        _iso_aware(frozen.get("commence_time"), "provider event commence_time")
        home_raw = _required_text(frozen.get("home_team"), "provider event home_team")
        away_raw = _required_text(frozen.get("away_team"), "provider event away_team")
        if home_raw == away_raw:
            raise OddsWeatherContractError("provider event home and away teams must differ")
        if team_key(home_raw) is None or team_key(away_raw) is None:
            raise OddsWeatherContractError(
                "provider event teams must resolve exactly to canonical MLB teams"
            )
        if not isinstance(frozen.get("bookmakers"), tuple):
            raise OddsWeatherContractError("provider event bookmakers must be a list")
        frozen_history: list[Mapping[str, Any]] = []
        for index, row in enumerate(self.history_rows):
            mapping = _mapping(row, f"history_rows[{index}]")
            item = _freeze_json(dict(mapping))
            assert isinstance(item, Mapping)
            frozen_history.append(item)
        object.__setattr__(self, "history_rows", tuple(frozen_history))
        serialized = self.as_dict()
        if redact_value(serialized) != serialized:
            raise OddsWeatherContractError(
                "OddsProviderEventV1 contains credential-bearing material"
            )

    @property
    def commence_time(self) -> datetime:
        return _iso_aware(self.event.get("commence_time"), "provider event commence_time")

    @property
    def home_team_id(self) -> str:
        result = team_key(str(self.event.get("home_team") or ""))
        if result is None:
            raise OddsWeatherContractError("provider home team no longer resolves")
        return result

    @property
    def away_team_id(self) -> str:
        result = team_key(str(self.event.get("away_team") or ""))
        if result is None:
            raise OddsWeatherContractError("provider away team no longer resolves")
        return result

    def mutable_event(self) -> dict[str, Any]:
        thawed = _thaw_json(self.event)
        assert isinstance(thawed, dict)
        return thawed

    def mutable_history_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in self.history_rows:
            thawed = _thaw_json(row)
            assert isinstance(thawed, dict)
            rows.append(thawed)
        return rows

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event": _thaw_json(self.event),
            "history_rows": [_thaw_json(row) for row in self.history_rows],
            "provider_event_id": self.provider_event_id,
            "raw_capture_checksum": self.raw_capture_checksum,
            "retrieved_at": self.retrieved_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WeatherForecastEvidenceV1:
    source_game_id: str
    provider: WeatherProvider
    retrieved_at: datetime
    raw_capture_checksums: tuple[str, ...]
    forecast: Mapping[str, Any]
    contract_version: str = WEATHER_FORECAST_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_game_id",
            _required_text(self.source_game_id, "source_game_id"),
        )
        object.__setattr__(
            self,
            "retrieved_at",
            _aware_utc(self.retrieved_at, "weather retrieved_at"),
        )
        checksums = tuple(
            sorted({_sha256(value, "weather raw_capture_checksum") for value in self.raw_capture_checksums})
        )
        if not checksums:
            raise OddsWeatherContractError(
                "weather evidence requires at least one raw capture checksum"
            )
        object.__setattr__(self, "raw_capture_checksums", checksums)
        if self.contract_version != WEATHER_FORECAST_CONTRACT_VERSION:
            raise OddsWeatherContractError("unsupported weather forecast contract")
        raw = _mapping(self.forecast, "forecast")
        frozen = _freeze_json(dict(raw))
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "forecast", frozen)
        _iso_aware(frozen.get("forecast_time"), "forecast_time")
        _finite_optional(
            frozen.get("forecast_offset_minutes"),
            "forecast_offset_minutes",
            minimum=0.0,
            maximum=MAX_WEATHER_FORECAST_OFFSET_MINUTES,
        )
        _finite_optional(frozen.get("temperature_f"), "temperature_f")
        _finite_optional(frozen.get("humidity_pct"), "humidity_pct", minimum=0.0, maximum=100.0)
        _finite_optional(
            frozen.get("precipitation_probability_pct"),
            "precipitation_probability_pct",
            minimum=0.0,
            maximum=100.0,
        )
        _finite_optional(frozen.get("wind_speed_mph"), "wind_speed_mph", minimum=0.0)
        _finite_optional(
            frozen.get("wind_direction_deg"),
            "wind_direction_deg",
            minimum=0.0,
            maximum=360.0,
            maximum_inclusive=False,
        )
        _finite_optional(frozen.get("wind_gust_mph"), "wind_gust_mph", minimum=0.0)
        _finite_optional(frozen.get("clouds_pct"), "clouds_pct", minimum=0.0, maximum=100.0)
        _finite_optional(frozen.get("pressure_hpa"), "pressure_hpa", minimum=0.0)
        serialized = self.as_dict()
        if redact_value(serialized) != serialized:
            raise OddsWeatherContractError(
                "WeatherForecastEvidenceV1 contains credential-bearing material"
            )

    @property
    def forecast_time(self) -> datetime:
        return _iso_aware(self.forecast.get("forecast_time"), "forecast_time")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "forecast": _thaw_json(self.forecast),
            "provider": self.provider.value,
            "raw_capture_checksums": list(self.raw_capture_checksums),
            "retrieved_at": self.retrieved_at.isoformat(),
            "source_game_id": self.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class VenueWeatherContextV1:
    team_id: str
    physical_venue_key: str | None
    venue_name: str | None
    latitude: float | None
    longitude: float | None
    timezone_name: str | None
    roof_type: str
    operational_roof_status: str
    roof_verification_state: str
    outfield_bearing_degrees: float | None
    outfield_bearing_verification_state: str
    metadata_policy_version: str | None
    catalog_version: int | None
    association_errors: tuple[str, ...] = ()
    coordinate_errors: tuple[str, ...] = ()
    contract_version: str = VENUE_WEATHER_CONTEXT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.team_id not in CANONICAL_TEAM_KEYS:
            raise OddsWeatherContractError("venue team_id must be canonical MLB")
        object.__setattr__(
            self,
            "physical_venue_key",
            _optional_text(self.physical_venue_key, "physical_venue_key"),
        )
        object.__setattr__(self, "venue_name", _optional_text(self.venue_name, "venue_name"))
        object.__setattr__(
            self,
            "latitude",
            _finite_optional(self.latitude, "latitude", minimum=-90.0, maximum=90.0),
        )
        object.__setattr__(
            self,
            "longitude",
            _finite_optional(self.longitude, "longitude", minimum=-180.0, maximum=180.0),
        )
        object.__setattr__(
            self,
            "timezone_name",
            _optional_text(self.timezone_name, "timezone_name"),
        )
        object.__setattr__(self, "roof_type", _required_text(self.roof_type, "roof_type"))
        object.__setattr__(
            self,
            "operational_roof_status",
            _required_text(self.operational_roof_status, "operational_roof_status"),
        )
        object.__setattr__(
            self,
            "roof_verification_state",
            _required_text(self.roof_verification_state, "roof_verification_state"),
        )
        object.__setattr__(
            self,
            "outfield_bearing_degrees",
            _finite_optional(
                self.outfield_bearing_degrees,
                "outfield_bearing_degrees",
                minimum=0.0,
                maximum=360.0,
                maximum_inclusive=False,
            ),
        )
        object.__setattr__(
            self,
            "outfield_bearing_verification_state",
            _required_text(
                self.outfield_bearing_verification_state,
                "outfield_bearing_verification_state",
            ),
        )
        object.__setattr__(
            self,
            "metadata_policy_version",
            _optional_text(self.metadata_policy_version, "metadata_policy_version"),
        )
        if self.catalog_version is not None and (
            isinstance(self.catalog_version, bool)
            or not isinstance(self.catalog_version, int)
            or self.catalog_version <= 0
        ):
            raise OddsWeatherContractError("catalog_version must be a positive integer")
        object.__setattr__(
            self,
            "association_errors",
            tuple(sorted({_required_text(value, "association_error") for value in self.association_errors})),
        )
        object.__setattr__(
            self,
            "coordinate_errors",
            tuple(sorted({_required_text(value, "coordinate_error") for value in self.coordinate_errors})),
        )
        if self.contract_version != VENUE_WEATHER_CONTEXT_CONTRACT_VERSION:
            raise OddsWeatherContractError("unsupported venue weather context contract")

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "association_errors": list(self.association_errors),
            "catalog_version": self.catalog_version,
            "contract_version": self.contract_version,
            "coordinate_errors": list(self.coordinate_errors),
            "latitude": self.latitude,
            "longitude": self.longitude,
            "metadata_policy_version": self.metadata_policy_version,
            "operational_roof_status": self.operational_roof_status,
            "outfield_bearing_degrees": self.outfield_bearing_degrees,
            "outfield_bearing_verification_state": self.outfield_bearing_verification_state,
            "physical_venue_key": self.physical_venue_key,
            "roof_type": self.roof_type,
            "roof_verification_state": self.roof_verification_state,
            "team_id": self.team_id,
            "timezone_name": self.timezone_name,
            "venue_name": self.venue_name,
        }


@dataclass(frozen=True, slots=True)
class OddsSnapshotV1:
    availability: OddsAvailability
    provider_event_id: str | None
    retrieved_at: datetime | None
    raw_capture_checksum: str | None
    event_match_offset_minutes: float | None
    summary: Mapping[str, Any] | None
    normalized_market_count: int
    raw_snapshot_count: int
    freshness_counts: Mapping[str, int]
    calculation_version: str = CALCULATION_VERSION
    odds_consensus_contract_version: str = ODDS_CONSENSUS_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_event_id",
            _optional_text(self.provider_event_id, "provider_event_id"),
        )
        if self.retrieved_at is not None:
            object.__setattr__(
                self,
                "retrieved_at",
                _aware_utc(self.retrieved_at, "odds snapshot retrieved_at"),
            )
        if self.raw_capture_checksum is not None:
            object.__setattr__(
                self,
                "raw_capture_checksum",
                _sha256(self.raw_capture_checksum, "raw_capture_checksum"),
            )
        object.__setattr__(
            self,
            "event_match_offset_minutes",
            _finite_optional(
                self.event_match_offset_minutes,
                "event_match_offset_minutes",
                minimum=0.0,
            ),
        )
        for name in ("normalized_market_count", "raw_snapshot_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OddsWeatherContractError(f"{name} must be a non-negative integer")
        if self.calculation_version != CALCULATION_VERSION:
            raise OddsWeatherContractError("unsupported odds calculation_version")
        if self.odds_consensus_contract_version != ODDS_CONSENSUS_CONTRACT_VERSION:
            raise OddsWeatherContractError("unsupported odds consensus contract")
        frozen_freshness = _freeze_json(dict(self.freshness_counts))
        assert isinstance(frozen_freshness, Mapping)
        object.__setattr__(self, "freshness_counts", frozen_freshness)
        if self.availability is OddsAvailability.AVAILABLE:
            if (
                self.provider_event_id is None
                or self.retrieved_at is None
                or self.raw_capture_checksum is None
                or self.event_match_offset_minutes is None
                or self.summary is None
            ):
                raise OddsWeatherContractError(
                    "available odds snapshot requires provider identity, lineage, match offset, and summary"
                )
            summary = _mapping(self.summary, "summary")
            frozen_summary = _freeze_json(dict(summary))
            assert isinstance(frozen_summary, Mapping)
            object.__setattr__(self, "summary", frozen_summary)
            if frozen_summary.get("contract_version") != ODDS_CONSENSUS_CONTRACT_VERSION:
                raise OddsWeatherContractError("odds summary contract_version mismatch")
            if frozen_summary.get("calculation_version") != CALCULATION_VERSION:
                raise OddsWeatherContractError("odds summary calculation_version mismatch")
            if str(frozen_summary.get("event_id") or "") != self.provider_event_id:
                raise OddsWeatherContractError("odds summary provider event id mismatch")
        else:
            if any(
                value is not None
                for value in (
                    self.provider_event_id,
                    self.retrieved_at,
                    self.raw_capture_checksum,
                    self.event_match_offset_minutes,
                    self.summary,
                )
            ):
                raise OddsWeatherContractError(
                    "unavailable odds snapshot must not contain provider evidence"
                )
            if self.normalized_market_count or self.raw_snapshot_count:
                raise OddsWeatherContractError(
                    "unavailable odds snapshot must have zero market/snapshot counts"
                )

    @property
    def summary_checksum(self) -> str | None:
        return None if self.summary is None else canonical_sha256(_thaw_json(self.summary))

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "calculation_version": self.calculation_version,
            "event_match_offset_minutes": self.event_match_offset_minutes,
            "freshness_counts": _thaw_json(self.freshness_counts),
            "normalized_market_count": self.normalized_market_count,
            "odds_consensus_contract_version": self.odds_consensus_contract_version,
            "provider_event_id": self.provider_event_id,
            "raw_capture_checksum": self.raw_capture_checksum,
            "raw_snapshot_count": self.raw_snapshot_count,
            "retrieved_at": None if self.retrieved_at is None else self.retrieved_at.isoformat(),
            "summary": None if self.summary is None else _thaw_json(self.summary),
            "summary_checksum": self.summary_checksum,
        }


@dataclass(frozen=True, slots=True)
class WeatherSnapshotV1:
    status: WeatherStatus
    relevance: WeatherRelevance
    venue_context: VenueWeatherContextV1 | None
    primary_source: WeatherProvider | None
    nws: WeatherForecastEvidenceV1 | None
    openweather: WeatherForecastEvidenceV1 | None
    comparison: Mapping[str, Any]
    baseball_wind_impact: Mapping[str, Any]

    def __post_init__(self) -> None:
        comparison = _freeze_json(dict(_mapping(self.comparison, "comparison")))
        wind = _freeze_json(dict(_mapping(self.baseball_wind_impact, "baseball_wind_impact")))
        assert isinstance(comparison, Mapping)
        assert isinstance(wind, Mapping)
        object.__setattr__(self, "comparison", comparison)
        object.__setattr__(self, "baseball_wind_impact", wind)
        if self.nws is not None and self.nws.provider is not WeatherProvider.NWS:
            raise OddsWeatherContractError("nws field must contain NWS evidence")
        if self.openweather is not None and self.openweather.provider is not WeatherProvider.OPENWEATHER:
            raise OddsWeatherContractError(
                "openweather field must contain OpenWeather evidence"
            )
        if self.status is WeatherStatus.AVAILABLE:
            if self.venue_context is None or (self.nws is None and self.openweather is None):
                raise OddsWeatherContractError(
                    "available weather requires venue context and at least one forecast"
                )
            expected_primary = (
                WeatherProvider.NWS if self.nws is not None else WeatherProvider.OPENWEATHER
            )
            if self.primary_source is not expected_primary:
                raise OddsWeatherContractError("weather primary source priority mismatch")
        elif self.status is WeatherStatus.INDOOR_FIXED_ROOF:
            if self.venue_context is None:
                raise OddsWeatherContractError("indoor weather requires venue context")
            if self.primary_source is not None or self.nws is not None or self.openweather is not None:
                raise OddsWeatherContractError(
                    "fixed-roof indoor weather must not retain forecast evidence"
                )
        else:
            if self.primary_source is not None or self.nws is not None or self.openweather is not None:
                raise OddsWeatherContractError(
                    "unavailable weather must not retain selected forecast evidence"
                )

    def as_dict(self) -> dict[str, object]:
        return {
            "baseball_wind_impact": _thaw_json(self.baseball_wind_impact),
            "comparison": _thaw_json(self.comparison),
            "nws": None if self.nws is None else self.nws.as_dict(),
            "openweather": None if self.openweather is None else self.openweather.as_dict(),
            "primary_source": None if self.primary_source is None else self.primary_source.value,
            "relevance": self.relevance.value,
            "status": self.status.value,
            "venue_context": None if self.venue_context is None else self.venue_context.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class OddsWeatherWarningV1:
    code: str
    domain: OddsWeatherWarningDomain
    message: str
    source_game_id: str | None = None
    provider: str | None = None
    provider_event_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, "warning code"))
        object.__setattr__(self, "message", _required_text(self.message, "warning message"))
        object.__setattr__(
            self,
            "source_game_id",
            _optional_text(self.source_game_id, "warning source_game_id"),
        )
        object.__setattr__(self, "provider", _optional_text(self.provider, "warning provider"))
        object.__setattr__(
            self,
            "provider_event_id",
            _optional_text(self.provider_event_id, "warning provider_event_id"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "domain": self.domain.value,
            "message": self.message,
            "provider": self.provider,
            "provider_event_id": self.provider_event_id,
            "source_game_id": self.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class OddsWeatherGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    scheduled_start_time: datetime | None
    upstream_daily_slate_game_checksum: str
    upstream_baseball_intelligence_game_checksum: str
    odds: OddsSnapshotV1
    weather: WeatherSnapshotV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "edge_event_id", _required_text(self.edge_event_id, "edge_event_id"))
        object.__setattr__(
            self,
            "daily_mlb_game_id",
            _required_text(self.daily_mlb_game_id, "daily_mlb_game_id"),
        )
        object.__setattr__(
            self,
            "source_game_id",
            _required_text(self.source_game_id, "source_game_id"),
        )
        if self.away_team_id not in CANONICAL_TEAM_KEYS or self.home_team_id not in CANONICAL_TEAM_KEYS:
            raise OddsWeatherContractError("game teams must be canonical MLB")
        if self.away_team_id == self.home_team_id:
            raise OddsWeatherContractError("home and away teams must differ")
        if self.scheduled_start_time is not None:
            object.__setattr__(
                self,
                "scheduled_start_time",
                _aware_utc(self.scheduled_start_time, "scheduled_start_time"),
            )
        object.__setattr__(
            self,
            "upstream_daily_slate_game_checksum",
            _sha256(
                self.upstream_daily_slate_game_checksum,
                "upstream_daily_slate_game_checksum",
            ),
        )
        object.__setattr__(
            self,
            "upstream_baseball_intelligence_game_checksum",
            _sha256(
                self.upstream_baseball_intelligence_game_checksum,
                "upstream_baseball_intelligence_game_checksum",
            ),
        )
        if self.odds.availability is OddsAvailability.AVAILABLE and self.odds.summary is not None:
            if self.odds.summary.get("home_team_key") != self.home_team_id:
                raise OddsWeatherContractError("odds summary home team mismatch")
            if self.odds.summary.get("away_team_key") != self.away_team_id:
                raise OddsWeatherContractError("odds summary away team mismatch")

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "edge_event_id": self.edge_event_id,
            "home_team_id": self.home_team_id,
            "odds": self.odds.as_dict(),
            "scheduled_start_time": (
                None
                if self.scheduled_start_time is None
                else self.scheduled_start_time.isoformat()
            ),
            "source_game_id": self.source_game_id,
            "upstream_baseball_intelligence_game_checksum": self.upstream_baseball_intelligence_game_checksum,
            "upstream_daily_slate_game_checksum": self.upstream_daily_slate_game_checksum,
            "weather": self.weather.as_dict(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class OddsWeatherV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_daily_slate_checksum: str
    upstream_baseball_intelligence_checksum: str
    source_raw_capture_checksums: tuple[str, ...]
    games: tuple[OddsWeatherGameV1, ...]
    warnings: tuple[OddsWeatherWarningV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    contract_version: str = ODDS_WEATHER_CONTRACT_VERSION
    sport: str = ODDS_WEATHER_SPORT
    league: str = ODDS_WEATHER_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", _aware_utc(self.observed_at, "observed_at"))
        object.__setattr__(
            self,
            "upstream_daily_slate_checksum",
            _sha256(self.upstream_daily_slate_checksum, "upstream_daily_slate_checksum"),
        )
        object.__setattr__(
            self,
            "upstream_baseball_intelligence_checksum",
            _sha256(
                self.upstream_baseball_intelligence_checksum,
                "upstream_baseball_intelligence_checksum",
            ),
        )
        checksums = tuple(
            sorted(
                {
                    _sha256(value, "source_raw_capture_checksum")
                    for value in self.source_raw_capture_checksums
                }
            )
        )
        object.__setattr__(self, "source_raw_capture_checksums", checksums)
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise OddsWeatherContractError("OddsWeatherV1 contains duplicate games")
        object.__setattr__(self, "games", games)
        object.__setattr__(self, "warnings", tuple(self.warnings))
        if self.contract_version != ODDS_WEATHER_CONTRACT_VERSION:
            raise OddsWeatherContractError("unsupported OddsWeatherV1 contract_version")
        if self.sport != "MLB" or self.league != "MLB":
            raise OddsWeatherContractError("sport and league must both be MLB")
        payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured) != payload:
            raise OddsWeatherContractError(
                "OddsWeatherV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "requested_date": self.requested_date,
            "source_raw_capture_checksums": list(self.source_raw_capture_checksums),
            "sport": self.sport,
            "upstream_baseball_intelligence_checksum": self.upstream_baseball_intelligence_checksum,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "warnings": [warning.as_dict() for warning in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def selected_raw_capture_checksums(
    odds: OddsSnapshotV1,
    weather: WeatherSnapshotV1,
) -> tuple[str, ...]:
    values: set[str] = set()
    if odds.raw_capture_checksum is not None:
        values.add(odds.raw_capture_checksum)
    for evidence in (weather.nws, weather.openweather):
        if evidence is not None:
            values.update(evidence.raw_capture_checksums)
    return tuple(sorted(values))


def thaw_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _thaw_json(value)
    assert isinstance(result, dict)
    return result


def thaw_sequence(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [thaw_mapping(item) for item in value]
