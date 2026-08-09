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

FIRST_INNING_TOTALS_MARKET = "totals_1st_1_innings"
FIRST_INNING_ODDS_EVIDENCE_CONTRACT_VERSION = (
    "DSE_MLB_FIRST_INNING_ODDS_EVIDENCE_V1"
)
FIRST_INNING_ODDS_PLAN_CONTRACT_VERSION = "DSE_MLB_FIRST_INNING_ODDS_REQUEST_PLAN_V1"
FIRST_INNING_ODDS_NORMALIZATION_CONTRACT_VERSION = (
    "DSE_MLB_FIRST_INNING_ODDS_NORMALIZATION_V1"
)
FIRST_INNING_ODDS_NORMALIZATION_CALCULATION_VERSION = (
    "DSE_MLB_FIRST_INNING_ODDS_VIA_FEATURED_NORMALIZER_V1"
)
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REGION_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")


class FirstInningOddsError(ValueError):
    """Raised when first-inning event-odds evidence violates its V5 contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise FirstInningOddsError(f"{name} must be non-empty trimmed text")
    return value


def _event_id(value: object) -> str:
    text = _text(value, "provider_event_id")
    if _EVENT_ID_RE.fullmatch(text) is None:
        raise FirstInningOddsError("provider_event_id contains unsupported characters")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FirstInningOddsError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise FirstInningOddsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise FirstInningOddsError(f"{name} must be finite numeric")
    return result


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise FirstInningOddsError(f"{name} must be lowercase SHA-256")
    return text


def _regions(value: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(","))
    if (
        not items
        or len(items) != len(set(items))
        or any(not item or _REGION_TOKEN_RE.fullmatch(item) is None for item in items)
    ):
        raise FirstInningOddsError("regions must contain unique provider region tokens")
    return items


@dataclass(frozen=True, slots=True)
class FirstInningOddsRequestPlanV1:
    provider_event_ids: tuple[str, ...]
    regions: tuple[str, ...]
    market_key: str = FIRST_INNING_TOTALS_MARKET
    contract_version: str = FIRST_INNING_ODDS_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        events = tuple(_event_id(item) for item in self.provider_event_ids)
        if len(events) != len(set(events)):
            raise FirstInningOddsError("first-inning request plan contains duplicate events")
        object.__setattr__(self, "provider_event_ids", events)
        regions = tuple(_text(item, "region") for item in self.regions)
        if len(regions) != len(set(regions)) or any(
            _REGION_TOKEN_RE.fullmatch(item) is None for item in regions
        ):
            raise FirstInningOddsError("first-inning request-plan regions are invalid")
        object.__setattr__(self, "regions", regions)
        if self.market_key != FIRST_INNING_TOTALS_MARKET:
            raise FirstInningOddsError("first-inning request-plan market is frozen")
        if self.contract_version != FIRST_INNING_ODDS_PLAN_CONTRACT_VERSION:
            raise FirstInningOddsError("unsupported first-inning request-plan contract")

    @property
    def planned_event_requests(self) -> int:
        return len(self.provider_event_ids)

    @property
    def maximum_quota_credits(self) -> int:
        return self.planned_event_requests * len(self.regions)

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "market_key": self.market_key,
            "maximum_quota_credits": self.maximum_quota_credits,
            "planned_event_requests": self.planned_event_requests,
            "provider_event_ids": list(self.provider_event_ids),
            "regions": list(self.regions),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def plan_first_inning_odds_requests(
    provider_event_ids: tuple[str, ...] | list[str],
    *,
    regions: str,
) -> FirstInningOddsRequestPlanV1:
    return FirstInningOddsRequestPlanV1(
        provider_event_ids=tuple(provider_event_ids),
        regions=_regions(regions),
    )


def _validate_event(event: Mapping[str, Any], expected_event_id: str) -> None:
    if str(event.get("id") or "") != expected_event_id:
        raise FirstInningOddsError("first-inning provider event identity mismatch")
    if event.get("sport_key") != "baseball_mlb":
        raise FirstInningOddsError("first-inning provider event must be MLB")
    home = _text(event.get("home_team"), "home_team")
    away = _text(event.get("away_team"), "away_team")
    if home == away:
        raise FirstInningOddsError("first-inning provider event teams must differ")
    books = event.get("bookmakers")
    if not isinstance(books, list):
        raise FirstInningOddsError("first-inning provider bookmakers must be a list")
    for book_index, bookmaker in enumerate(books):
        if not isinstance(bookmaker, Mapping):
            raise FirstInningOddsError(f"bookmaker[{book_index}] must be an object")
        _text(bookmaker.get("key"), "bookmaker key")
        _text(bookmaker.get("title"), "bookmaker title")
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            raise FirstInningOddsError("bookmaker markets must be a list")
        for market in markets:
            if not isinstance(market, Mapping):
                raise FirstInningOddsError("first-inning market must be an object")
            if market.get("key") != FIRST_INNING_TOTALS_MARKET:
                raise FirstInningOddsError("unsupported first-inning market key")
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list):
                raise FirstInningOddsError("first-inning market outcomes must be a list")
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    raise FirstInningOddsError("first-inning outcome must be an object")
                if outcome.get("name") not in {"Over", "Under"}:
                    raise FirstInningOddsError("first-inning total outcome must be Over or Under")
                _finite(outcome.get("price"), "outcome price")
                _finite(outcome.get("point"), "outcome point")


@dataclass(frozen=True, slots=True)
class FirstInningOddsEvidenceV1:
    provider_event_id: str
    retrieved_at: datetime
    raw_capture_checksum: str
    event: dict[str, Any]
    contract_version: str = FIRST_INNING_ODDS_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        event_id = _event_id(self.provider_event_id)
        object.__setattr__(self, "provider_event_id", event_id)
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at"))
        object.__setattr__(
            self,
            "raw_capture_checksum",
            _sha(self.raw_capture_checksum, "raw_capture_checksum"),
        )
        _validate_event(self.event, event_id)
        if self.contract_version != FIRST_INNING_ODDS_EVIDENCE_CONTRACT_VERSION:
            raise FirstInningOddsError("unsupported first-inning odds evidence contract")

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
            market["first_inning_source_market_key"] = market["key"]
            market["key"] = "totals"
    return result


def _restore_summary(summary: dict[str, Any]) -> dict[str, Any]:
    restored = deepcopy(summary)
    if restored.get("contract_version") != ODDS_CONSENSUS_CONTRACT_VERSION:
        raise FirstInningOddsError("featured consensus contract changed unexpectedly")
    if restored.get("calculation_version") != FEATURED_ODDS_CALCULATION_VERSION:
        raise FirstInningOddsError("featured odds calculation changed unexpectedly")
    markets = restored.get("markets")
    if not isinstance(markets, dict):
        raise FirstInningOddsError("normalized summary lacks markets")
    totals = markets.get("totals")
    restored["markets"] = (
        {FIRST_INNING_TOTALS_MARKET: totals} if isinstance(totals, dict) else {}
    )
    if isinstance(totals, dict):
        lines = totals.get("lines")
        if isinstance(lines, dict):
            for line in lines.values():
                if isinstance(line, dict):
                    line["market_key"] = FIRST_INNING_TOTALS_MARKET
    best_prices = restored.get("best_prices")
    if isinstance(best_prices, dict) and "totals" in best_prices:
        restored["best_prices"] = {
            FIRST_INNING_TOTALS_MARKET: best_prices["totals"]
        }
    warnings = restored.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, dict) and warning.get("market") == "totals":
                warning["market"] = FIRST_INNING_TOTALS_MARKET
    restored["base_consensus_contract_version"] = ODDS_CONSENSUS_CONTRACT_VERSION
    restored["base_calculation_version"] = FEATURED_ODDS_CALCULATION_VERSION
    restored["contract_version"] = FIRST_INNING_ODDS_NORMALIZATION_CONTRACT_VERSION
    restored["calculation_version"] = FIRST_INNING_ODDS_NORMALIZATION_CALCULATION_VERSION
    restored["market_period"] = "first_inning"
    restored["source_market_key"] = FIRST_INNING_TOTALS_MARKET
    return restored


@dataclass(frozen=True, slots=True)
class FirstInningNormalizedOddsV1:
    provider_event_id: str
    source_evidence_checksum: str
    source_raw_capture_checksum: str
    summary: dict[str, Any]
    raw_snapshot_count: int
    normalized_market_count: int
    contract_version: str = FIRST_INNING_ODDS_NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_event_id", _event_id(self.provider_event_id))
        object.__setattr__(self, "source_evidence_checksum", _sha(self.source_evidence_checksum, "source_evidence_checksum"))
        object.__setattr__(self, "source_raw_capture_checksum", _sha(self.source_raw_capture_checksum, "source_raw_capture_checksum"))
        if self.summary.get("market_period") != "first_inning":
            raise FirstInningOddsError("normalized first-inning period mismatch")
        for name in ("raw_snapshot_count", "normalized_market_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FirstInningOddsError(f"{name} must be nonnegative integer")
        if self.contract_version != FIRST_INNING_ODDS_NORMALIZATION_CONTRACT_VERSION:
            raise FirstInningOddsError("unsupported normalized first-inning odds contract")

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


def normalize_first_inning_odds(
    evidence: FirstInningOddsEvidenceV1,
    *,
    run_id: str = "",
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
) -> FirstInningNormalizedOddsV1:
    processed = process_game(
        _alias_event(evidence.event),
        run_id=run_id,
        retrieved_at=evidence.retrieved_at.isoformat(),
        freshness_thresholds=freshness_thresholds,
        consensus_thresholds=consensus_thresholds,
        history_rows=(),
    )
    return FirstInningNormalizedOddsV1(
        provider_event_id=evidence.provider_event_id,
        source_evidence_checksum=evidence.checksum,
        source_raw_capture_checksum=evidence.raw_capture_checksum,
        summary=_restore_summary(processed.summary),
        raw_snapshot_count=processed.raw_snapshot_count,
        normalized_market_count=processed.normalized_market_count,
    )
