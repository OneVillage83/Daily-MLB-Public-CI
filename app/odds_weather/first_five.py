from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.processors.odds_processor import (
    CALCULATION_VERSION as FEATURED_ODDS_CALCULATION_VERSION,
    ODDS_CONSENSUS_CONTRACT_VERSION,
    ConsensusThresholds,
    FreshnessThresholds,
    process_game,
)

FIRST_FIVE_ODDS_MARKETS = (
    "h2h_1st_5_innings",
    "spreads_1st_5_innings",
    "totals_1st_5_innings",
)
FIRST_FIVE_ODDS_MARKET_SET = frozenset(FIRST_FIVE_ODDS_MARKETS)
FIRST_FIVE_TO_FEATURED_MARKET = {
    "h2h_1st_5_innings": "h2h",
    "spreads_1st_5_innings": "spreads",
    "totals_1st_5_innings": "totals",
}
FEATURED_TO_FIRST_FIVE_MARKET = {
    value: key for key, value in FIRST_FIVE_TO_FEATURED_MARKET.items()
}
FIRST_FIVE_ODDS_EVIDENCE_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_ODDS_EVIDENCE_V1"
FIRST_FIVE_ODDS_PLAN_CONTRACT_VERSION = "DSE_MLB_FIRST_FIVE_ODDS_REQUEST_PLAN_V1"
FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION = (
    "DSE_MLB_FIRST_FIVE_ODDS_NORMALIZATION_V1"
)
FIRST_FIVE_ODDS_NORMALIZATION_CALCULATION_VERSION = (
    "DSE_MLB_FIRST_FIVE_ODDS_VIA_FEATURED_NORMALIZER_V1"
)
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REGION_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")


class FirstFiveOddsError(ValueError):
    """Raised when First Five event-odds evidence violates its V4 contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstFiveOddsError(f"{name} must be non-empty trimmed text")
    return value


def _event_id(value: object) -> str:
    text = _text(value, "provider_event_id")
    if _EVENT_ID_RE.fullmatch(text) is None:
        raise FirstFiveOddsError("provider_event_id contains unsupported characters")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FirstFiveOddsError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstFiveOddsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FirstFiveOddsError(f"{name} must be finite numeric")
    return result


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstFiveOddsError(f"{name} must be lowercase SHA-256")
    return text


def _regions(value: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(","))
    if (
        not items
        or len(items) != len(set(items))
        or any(not item or _REGION_TOKEN_RE.fullmatch(item) is None for item in items)
    ):
        raise FirstFiveOddsError("regions must contain unique provider region tokens")
    return items


@dataclass(frozen=True, slots=True)
class FirstFiveOddsRequestPlanV1:
    provider_event_ids: tuple[str, ...]
    regions: tuple[str, ...]
    market_keys: tuple[str, ...] = FIRST_FIVE_ODDS_MARKETS
    contract_version: str = FIRST_FIVE_ODDS_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        events = tuple(_event_id(item) for item in self.provider_event_ids)
        if len(events) != len(set(events)):
            raise FirstFiveOddsError("First Five request plan contains duplicate events")
        object.__setattr__(self, "provider_event_ids", events)
        regions = tuple(_text(item, "region") for item in self.regions)
        if len(regions) != len(set(regions)) or any(
            _REGION_TOKEN_RE.fullmatch(item) is None for item in regions
        ):
            raise FirstFiveOddsError("First Five request plan regions are invalid")
        object.__setattr__(self, "regions", regions)
        if self.market_keys != FIRST_FIVE_ODDS_MARKETS:
            raise FirstFiveOddsError("First Five request-plan market set is frozen")
        if self.contract_version != FIRST_FIVE_ODDS_PLAN_CONTRACT_VERSION:
            raise FirstFiveOddsError("unsupported First Five odds request-plan contract")

    @property
    def planned_event_requests(self) -> int:
        return len(self.provider_event_ids)

    @property
    def maximum_quota_credits(self) -> int:
        return self.planned_event_requests * len(self.regions) * len(self.market_keys)

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "market_keys": list(self.market_keys),
            "maximum_quota_credits": self.maximum_quota_credits,
            "planned_event_requests": self.planned_event_requests,
            "provider_event_ids": list(self.provider_event_ids),
            "regions": list(self.regions),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def plan_first_five_odds_requests(
    provider_event_ids: tuple[str, ...] | list[str],
    *,
    regions: str,
) -> FirstFiveOddsRequestPlanV1:
    """Plan provider event requests without executing any network call.

    The maximum quota estimate is event count x three F5 markets x region count.
    The provider may charge less when one or more requested markets are unavailable.
    """

    return FirstFiveOddsRequestPlanV1(
        provider_event_ids=tuple(provider_event_ids),
        regions=_regions(regions),
    )


def _validate_event_shape(event: Mapping[str, Any], expected_event_id: str) -> None:
    if str(event.get("id") or "") != expected_event_id:
        raise FirstFiveOddsError("First Five provider event identity mismatch")
    if event.get("sport_key") != "baseball_mlb":
        raise FirstFiveOddsError("First Five provider event sport_key must be baseball_mlb")
    home = _text(event.get("home_team"), "home_team")
    away = _text(event.get("away_team"), "away_team")
    if home == away:
        raise FirstFiveOddsError("First Five provider event teams must differ")
    if not isinstance(event.get("bookmakers"), list):
        raise FirstFiveOddsError("First Five provider event bookmakers must be a list")


def _validate_market_payload(event: Mapping[str, Any]) -> None:
    for book_index, bookmaker in enumerate(event.get("bookmakers", [])):
        if not isinstance(bookmaker, Mapping):
            raise FirstFiveOddsError(f"bookmaker[{book_index}] must be an object")
        _text(bookmaker.get("key"), f"bookmaker[{book_index}].key")
        _text(bookmaker.get("title"), f"bookmaker[{book_index}].title")
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            raise FirstFiveOddsError(f"bookmaker[{book_index}].markets must be a list")
        for market_index, market in enumerate(markets):
            if not isinstance(market, Mapping):
                raise FirstFiveOddsError("First Five market must be an object")
            key = str(market.get("key") or "")
            if key not in FIRST_FIVE_ODDS_MARKET_SET:
                raise FirstFiveOddsError(
                    f"bookmaker[{book_index}].market[{market_index}] is not a supported F5 market"
                )
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list):
                raise FirstFiveOddsError("First Five market outcomes must be a list")
            for outcome_index, outcome in enumerate(outcomes):
                if not isinstance(outcome, Mapping):
                    raise FirstFiveOddsError("First Five outcome must be an object")
                _text(outcome.get("name"), "outcome name")
                _finite(outcome.get("price"), "outcome price")
                if outcome.get("point") is not None:
                    _finite(outcome.get("point"), "outcome point")


@dataclass(frozen=True, slots=True)
class FirstFiveOddsEvidenceV1:
    provider_event_id: str
    retrieved_at: datetime
    raw_capture_checksum: str
    event: dict[str, Any]
    contract_version: str = FIRST_FIVE_ODDS_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        event_id = _event_id(self.provider_event_id)
        object.__setattr__(self, "provider_event_id", event_id)
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at"))
        object.__setattr__(
            self,
            "raw_capture_checksum",
            _sha(self.raw_capture_checksum, "raw_capture_checksum"),
        )
        _validate_event_shape(self.event, event_id)
        _validate_market_payload(self.event)
        if self.contract_version != FIRST_FIVE_ODDS_EVIDENCE_CONTRACT_VERSION:
            raise FirstFiveOddsError("unsupported First Five odds evidence contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "event": self.event,
            "provider_event_id": self.provider_event_id,
            "raw_capture_checksum": self.raw_capture_checksum,
            "retrieved_at": self.retrieved_at.isoformat(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def _alias_event(event: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(event))
    for bookmaker in result.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            source_key = str(market.get("key") or "")
            market["first_five_source_market_key"] = source_key
            market["key"] = FIRST_FIVE_TO_FEATURED_MARKET[source_key]
    return result


def _restore_summary(summary: dict[str, Any]) -> dict[str, Any]:
    restored = deepcopy(summary)
    if restored.get("contract_version") != ODDS_CONSENSUS_CONTRACT_VERSION:
        raise FirstFiveOddsError("featured odds consensus contract changed unexpectedly")
    if restored.get("calculation_version") != FEATURED_ODDS_CALCULATION_VERSION:
        raise FirstFiveOddsError("featured odds calculation version changed unexpectedly")

    markets = restored.get("markets")
    if not isinstance(markets, dict):
        raise FirstFiveOddsError("featured odds summary lacks markets")
    remapped: dict[str, Any] = {}
    for alias, market_summary in markets.items():
        source_key = FEATURED_TO_FIRST_FIVE_MARKET.get(str(alias))
        if source_key is None:
            continue
        if isinstance(market_summary, dict):
            lines = market_summary.get("lines", {})
            if isinstance(lines, dict):
                for line in lines.values():
                    if isinstance(line, dict):
                        line["market_key"] = source_key
        remapped[source_key] = market_summary
    restored["markets"] = remapped

    best_prices = restored.get("best_prices")
    if isinstance(best_prices, dict):
        restored["best_prices"] = {
            FEATURED_TO_FIRST_FIVE_MARKET[key]: value
            for key, value in best_prices.items()
            if key in FEATURED_TO_FIRST_FIVE_MARKET
        }

    warnings = restored.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, dict) and warning.get("market") in FEATURED_TO_FIRST_FIVE_MARKET:
                warning["market"] = FEATURED_TO_FIRST_FIVE_MARKET[str(warning["market"])]

    restored["base_consensus_contract_version"] = ODDS_CONSENSUS_CONTRACT_VERSION
    restored["base_calculation_version"] = FEATURED_ODDS_CALCULATION_VERSION
    restored["contract_version"] = FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION
    restored["calculation_version"] = FIRST_FIVE_ODDS_NORMALIZATION_CALCULATION_VERSION
    restored["market_period"] = "first_five"
    restored["source_market_keys"] = list(FIRST_FIVE_ODDS_MARKETS)
    return restored


@dataclass(frozen=True, slots=True)
class FirstFiveNormalizedOddsV1:
    provider_event_id: str
    source_evidence_checksum: str
    source_raw_capture_checksum: str
    summary: dict[str, Any]
    raw_snapshot_count: int
    normalized_market_count: int
    contract_version: str = FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_event_id", _event_id(self.provider_event_id))
        object.__setattr__(
            self,
            "source_evidence_checksum",
            _sha(self.source_evidence_checksum, "source_evidence_checksum"),
        )
        object.__setattr__(
            self,
            "source_raw_capture_checksum",
            _sha(self.source_raw_capture_checksum, "source_raw_capture_checksum"),
        )
        if self.summary.get("market_period") != "first_five":
            raise FirstFiveOddsError("normalized First Five summary period mismatch")
        if self.summary.get("contract_version") != self.contract_version:
            raise FirstFiveOddsError("normalized First Five summary contract mismatch")
        for name in ("raw_snapshot_count", "normalized_market_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FirstFiveOddsError(f"{name} must be nonnegative integer")
        if self.contract_version != FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION:
            raise FirstFiveOddsError("unsupported First Five normalized odds contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "normalized_market_count": self.normalized_market_count,
            "provider_event_id": self.provider_event_id,
            "raw_snapshot_count": self.raw_snapshot_count,
            "source_evidence_checksum": self.source_evidence_checksum,
            "source_raw_capture_checksum": self.source_raw_capture_checksum,
            "summary": self.summary,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def normalize_first_five_odds(
    evidence: FirstFiveOddsEvidenceV1,
    *,
    run_id: str = "",
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
) -> FirstFiveNormalizedOddsV1:
    """Normalize F5 event evidence through the hardened featured-market math.

    Aliasing is internal only. Returned evidence restores the provider's First Five
    market keys and carries explicit first-five period metadata.
    """

    processed = process_game(
        _alias_event(evidence.event),
        run_id=run_id,
        retrieved_at=evidence.retrieved_at.isoformat(),
        freshness_thresholds=freshness_thresholds,
        consensus_thresholds=consensus_thresholds,
        history_rows=(),
    )
    summary = _restore_summary(processed.summary)
    return FirstFiveNormalizedOddsV1(
        provider_event_id=evidence.provider_event_id,
        source_evidence_checksum=evidence.checksum,
        source_raw_capture_checksum=evidence.raw_capture_checksum,
        summary=summary,
        raw_snapshot_count=processed.raw_snapshot_count,
        normalized_market_count=processed.normalized_market_count,
    )
