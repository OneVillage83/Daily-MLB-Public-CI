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
from app.team_aliases import team_key

TEAM_TOTAL_ODDS_MARKETS = ("team_totals", "alternate_team_totals")
TEAM_TOTAL_ODDS_MARKET_SET = frozenset(TEAM_TOTAL_ODDS_MARKETS)
TEAM_TOTAL_NORMALIZED_MARKET_KEY = "team_totals"
TEAM_TOTAL_ODDS_PLAN_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_ODDS_REQUEST_PLAN_V1"
TEAM_TOTAL_ODDS_EVIDENCE_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_ODDS_EVIDENCE_V1"
TEAM_TOTAL_ODDS_TEAM_SUMMARY_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_ODDS_TEAM_SUMMARY_V1"
TEAM_TOTAL_ODDS_NORMALIZATION_CONTRACT_VERSION = "DSE_MLB_TEAM_TOTAL_ODDS_NORMALIZATION_V1"
TEAM_TOTAL_ODDS_NORMALIZATION_CALCULATION_VERSION = (
    "DSE_MLB_TEAM_TOTAL_ODDS_VIA_FEATURED_TOTALS_NORMALIZER_V1"
)
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REGION_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")


class TeamTotalsOddsError(ValueError):
    """Raised when V6 team-total provider evidence violates its contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TeamTotalsOddsError(f"{name} must be non-empty trimmed text")
    return value


def _event_id(value: object) -> str:
    text = _text(value, "provider_event_id")
    if _EVENT_ID_RE.fullmatch(text) is None:
        raise TeamTotalsOddsError("provider_event_id contains unsupported characters")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TeamTotalsOddsError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TeamTotalsOddsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise TeamTotalsOddsError(f"{name} must be finite numeric")
    return result


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise TeamTotalsOddsError(f"{name} must be lowercase SHA-256")
    return text


def _regions(value: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(","))
    if (
        not items
        or len(items) != len(set(items))
        or any(not item or _REGION_TOKEN_RE.fullmatch(item) is None for item in items)
    ):
        raise TeamTotalsOddsError("regions must contain unique provider region tokens")
    return items


@dataclass(frozen=True, slots=True)
class TeamTotalsOddsRequestPlanV1:
    provider_event_ids: tuple[str, ...]
    regions: tuple[str, ...]
    market_keys: tuple[str, ...] = TEAM_TOTAL_ODDS_MARKETS
    contract_version: str = TEAM_TOTAL_ODDS_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        events = tuple(_event_id(item) for item in self.provider_event_ids)
        if len(events) != len(set(events)):
            raise TeamTotalsOddsError("team-total request plan contains duplicate events")
        object.__setattr__(self, "provider_event_ids", events)
        regions = tuple(_text(item, "region") for item in self.regions)
        if len(regions) != len(set(regions)) or any(
            _REGION_TOKEN_RE.fullmatch(item) is None for item in regions
        ):
            raise TeamTotalsOddsError("team-total request-plan regions are invalid")
        object.__setattr__(self, "regions", regions)
        if self.market_keys != TEAM_TOTAL_ODDS_MARKETS:
            raise TeamTotalsOddsError("team-total request-plan market set is frozen")
        if self.contract_version != TEAM_TOTAL_ODDS_PLAN_CONTRACT_VERSION:
            raise TeamTotalsOddsError("unsupported team-total odds request-plan contract")

    @property
    def planned_event_requests(self) -> int:
        return len(self.provider_event_ids)

    @property
    def maximum_quota_credits(self) -> int:
        # Current event-odds provider documentation states one usage credit per call.
        return self.planned_event_requests

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


def plan_team_totals_odds_requests(
    provider_event_ids: tuple[str, ...] | list[str],
    *,
    regions: str,
) -> TeamTotalsOddsRequestPlanV1:
    return TeamTotalsOddsRequestPlanV1(
        provider_event_ids=tuple(provider_event_ids),
        regions=_regions(regions),
    )


def _event_teams(event: Mapping[str, Any]) -> tuple[str, str, str, str]:
    away_raw = _text(event.get("away_team"), "away_team")
    home_raw = _text(event.get("home_team"), "home_team")
    if away_raw == home_raw:
        raise TeamTotalsOddsError("team-total provider event teams must differ")
    away_id = team_key(away_raw)
    home_id = team_key(home_raw)
    if away_id is None or home_id is None or away_id == home_id:
        raise TeamTotalsOddsError("team-total provider event teams lack canonical MLB mapping")
    return away_raw, home_raw, away_id, home_id


def _validate_event_shape(event: Mapping[str, Any], expected_event_id: str) -> None:
    if str(event.get("id") or "") != expected_event_id:
        raise TeamTotalsOddsError("team-total provider event identity mismatch")
    if event.get("sport_key") != "baseball_mlb":
        raise TeamTotalsOddsError("team-total provider event sport_key must be baseball_mlb")
    _event_teams(event)
    if not isinstance(event.get("bookmakers"), list):
        raise TeamTotalsOddsError("team-total provider event bookmakers must be a list")


def _validate_market_payload(event: Mapping[str, Any]) -> None:
    away_raw, home_raw, _, _ = _event_teams(event)
    valid_descriptions = {away_raw, home_raw}
    for book_index, bookmaker in enumerate(event.get("bookmakers", [])):
        if not isinstance(bookmaker, Mapping):
            raise TeamTotalsOddsError(f"bookmaker[{book_index}] must be an object")
        _text(bookmaker.get("key"), f"bookmaker[{book_index}].key")
        _text(bookmaker.get("title"), f"bookmaker[{book_index}].title")
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            raise TeamTotalsOddsError(f"bookmaker[{book_index}].markets must be a list")
        for market_index, market in enumerate(markets):
            if not isinstance(market, Mapping):
                raise TeamTotalsOddsError("team-total market must be an object")
            key = str(market.get("key") or "")
            if key not in TEAM_TOTAL_ODDS_MARKET_SET:
                raise TeamTotalsOddsError(
                    f"bookmaker[{book_index}].market[{market_index}] is not a supported team-total market"
                )
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list):
                raise TeamTotalsOddsError("team-total market outcomes must be a list")
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    raise TeamTotalsOddsError("team-total outcome must be an object")
                if _text(outcome.get("name"), "outcome name") not in {"Over", "Under"}:
                    raise TeamTotalsOddsError("team-total outcome name must be Over or Under")
                description = _text(outcome.get("description"), "outcome description")
                if description not in valid_descriptions:
                    raise TeamTotalsOddsError(
                        "team-total outcome description must identify one event team"
                    )
                _finite(outcome.get("price"), "outcome price")
                _finite(outcome.get("point"), "outcome point")


@dataclass(frozen=True, slots=True)
class TeamTotalsOddsEvidenceV1:
    provider_event_id: str
    retrieved_at: datetime
    raw_capture_checksum: str
    event: dict[str, Any]
    contract_version: str = TEAM_TOTAL_ODDS_EVIDENCE_CONTRACT_VERSION

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
        if self.contract_version != TEAM_TOTAL_ODDS_EVIDENCE_CONTRACT_VERSION:
            raise TeamTotalsOddsError("unsupported team-total odds evidence contract")

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


def _alias_team_event(event: Mapping[str, Any], raw_team_name: str) -> dict[str, Any]:
    result = deepcopy(dict(event))
    transformed_books: list[dict[str, Any]] = []
    for bookmaker in result.get("bookmakers", []):
        transformed_book = dict(bookmaker)
        transformed_markets: list[dict[str, Any]] = []
        for market in bookmaker.get("markets", []):
            source_key = str(market.get("key") or "")
            outcomes = [
                dict(outcome)
                for outcome in market.get("outcomes", [])
                if outcome.get("description") == raw_team_name
            ]
            if not outcomes:
                continue
            transformed_market = dict(market)
            transformed_market["team_total_source_market_key"] = source_key
            transformed_market["key"] = "totals"
            transformed_market["outcomes"] = outcomes
            transformed_markets.append(transformed_market)
        transformed_book["markets"] = transformed_markets
        transformed_books.append(transformed_book)
    result["bookmakers"] = transformed_books
    return result


def _restore_team_summary(summary: dict[str, Any], *, team_id: str) -> dict[str, Any]:
    restored = deepcopy(summary)
    if restored.get("contract_version") != ODDS_CONSENSUS_CONTRACT_VERSION:
        raise TeamTotalsOddsError("featured odds consensus contract changed unexpectedly")
    if restored.get("calculation_version") != FEATURED_ODDS_CALCULATION_VERSION:
        raise TeamTotalsOddsError("featured odds calculation version changed unexpectedly")

    markets = restored.get("markets")
    if not isinstance(markets, dict):
        raise TeamTotalsOddsError("featured odds summary lacks markets")
    totals = markets.get("totals")
    if isinstance(totals, dict):
        lines = totals.get("lines", {})
        if isinstance(lines, dict):
            for line in lines.values():
                if isinstance(line, dict):
                    line["market_key"] = TEAM_TOTAL_NORMALIZED_MARKET_KEY
    restored["markets"] = (
        {TEAM_TOTAL_NORMALIZED_MARKET_KEY: totals}
        if isinstance(totals, dict)
        else {}
    )

    best_prices = restored.get("best_prices")
    if isinstance(best_prices, dict):
        totals_prices = best_prices.get("totals")
        restored["best_prices"] = (
            {TEAM_TOTAL_NORMALIZED_MARKET_KEY: totals_prices}
            if totals_prices is not None
            else {}
        )

    warnings = restored.get("warnings")
    if isinstance(warnings, list):
        for warning in warnings:
            if isinstance(warning, dict) and warning.get("market") == "totals":
                warning["market"] = TEAM_TOTAL_NORMALIZED_MARKET_KEY

    restored["base_consensus_contract_version"] = ODDS_CONSENSUS_CONTRACT_VERSION
    restored["base_calculation_version"] = FEATURED_ODDS_CALCULATION_VERSION
    restored["contract_version"] = TEAM_TOTAL_ODDS_TEAM_SUMMARY_CONTRACT_VERSION
    restored["calculation_version"] = TEAM_TOTAL_ODDS_NORMALIZATION_CALCULATION_VERSION
    restored["market_family"] = "team_total"
    restored["subject_team_id"] = team_id
    restored["source_market_keys"] = list(TEAM_TOTAL_ODDS_MARKETS)
    return restored


@dataclass(frozen=True, slots=True)
class TeamTotalNormalizedTeamOddsV1:
    team_id: str
    raw_team_name: str
    summary: dict[str, Any]
    raw_snapshot_count: int
    normalized_market_count: int
    contract_version: str = TEAM_TOTAL_ODDS_TEAM_SUMMARY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "team_id", _text(self.team_id, "team_id"))
        object.__setattr__(self, "raw_team_name", _text(self.raw_team_name, "raw_team_name"))
        if team_key(self.raw_team_name) != self.team_id:
            raise TeamTotalsOddsError("normalized team-total raw/canonical team mismatch")
        if self.summary.get("contract_version") != self.contract_version:
            raise TeamTotalsOddsError("normalized team-total summary contract mismatch")
        if self.summary.get("subject_team_id") != self.team_id:
            raise TeamTotalsOddsError("normalized team-total summary subject mismatch")
        for name in ("raw_snapshot_count", "normalized_market_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TeamTotalsOddsError(f"{name} must be nonnegative integer")
        if self.contract_version != TEAM_TOTAL_ODDS_TEAM_SUMMARY_CONTRACT_VERSION:
            raise TeamTotalsOddsError("unsupported normalized team-total team contract")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "normalized_market_count": self.normalized_market_count,
            "raw_snapshot_count": self.raw_snapshot_count,
            "raw_team_name": self.raw_team_name,
            "summary": self.summary,
            "team_id": self.team_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class TeamTotalsNormalizedOddsV1:
    provider_event_id: str
    away_team_id: str
    home_team_id: str
    source_evidence_checksum: str
    source_raw_capture_checksum: str
    teams: tuple[TeamTotalNormalizedTeamOddsV1, TeamTotalNormalizedTeamOddsV1]
    contract_version: str = TEAM_TOTAL_ODDS_NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_event_id", _event_id(self.provider_event_id))
        for name in ("away_team_id", "home_team_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.away_team_id == self.home_team_id:
            raise TeamTotalsOddsError("normalized team-total event teams must differ")
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
        teams = tuple(self.teams)
        if len(teams) != 2 or tuple(item.team_id for item in teams) != (
            self.away_team_id,
            self.home_team_id,
        ):
            raise TeamTotalsOddsError(
                "normalized team-total teams must be deterministic away then home"
            )
        object.__setattr__(self, "teams", teams)
        if self.contract_version != TEAM_TOTAL_ODDS_NORMALIZATION_CONTRACT_VERSION:
            raise TeamTotalsOddsError("unsupported normalized team-total odds contract")

    @property
    def raw_snapshot_count(self) -> int:
        return sum(team.raw_snapshot_count for team in self.teams)

    @property
    def normalized_market_count(self) -> int:
        return sum(team.normalized_market_count for team in self.teams)

    def for_team(self, team_id: str) -> TeamTotalNormalizedTeamOddsV1:
        for team in self.teams:
            if team.team_id == team_id:
                return team
        raise TeamTotalsOddsError("requested team is not present in normalized team totals")

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "contract_version": self.contract_version,
            "home_team_id": self.home_team_id,
            "normalized_market_count": self.normalized_market_count,
            "provider_event_id": self.provider_event_id,
            "raw_snapshot_count": self.raw_snapshot_count,
            "source_evidence_checksum": self.source_evidence_checksum,
            "source_raw_capture_checksum": self.source_raw_capture_checksum,
            "teams": [team.as_dict() for team in self.teams],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


def normalize_team_totals_odds(
    evidence: TeamTotalsOddsEvidenceV1,
    *,
    run_id: str = "",
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
) -> TeamTotalsNormalizedOddsV1:
    away_raw, home_raw, away_id, home_id = _event_teams(evidence.event)
    normalized: list[TeamTotalNormalizedTeamOddsV1] = []
    for raw_name, team_id in ((away_raw, away_id), (home_raw, home_id)):
        processed = process_game(
            _alias_team_event(evidence.event, raw_name),
            run_id=run_id,
            retrieved_at=evidence.retrieved_at.isoformat(),
            freshness_thresholds=freshness_thresholds,
            consensus_thresholds=consensus_thresholds,
            history_rows=(),
        )
        normalized.append(
            TeamTotalNormalizedTeamOddsV1(
                team_id=team_id,
                raw_team_name=raw_name,
                summary=_restore_team_summary(processed.summary, team_id=team_id),
                raw_snapshot_count=processed.raw_snapshot_count,
                normalized_market_count=processed.normalized_market_count,
            )
        )
    return TeamTotalsNormalizedOddsV1(
        provider_event_id=evidence.provider_event_id,
        away_team_id=away_id,
        home_team_id=home_id,
        source_evidence_checksum=evidence.checksum,
        source_raw_capture_checksum=evidence.raw_capture_checksum,
        teams=(normalized[0], normalized[1]),
    )
