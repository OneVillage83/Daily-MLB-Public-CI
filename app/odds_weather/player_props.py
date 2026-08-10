from __future__ import annotations

import math
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from app.daily_slate.contracts import canonical_sha256
from app.predictions.player_props import PlayerPropStatistic
from app.processors.odds_processor import (
    ConsensusThresholds,
    FreshnessThresholds,
    process_game,
)

PLAYER_PROP_MARKET_TO_STATISTIC = {
    "batter_home_runs": PlayerPropStatistic.BATTER_HOME_RUNS,
    "batter_hits": PlayerPropStatistic.BATTER_HITS,
    "batter_total_bases": PlayerPropStatistic.BATTER_TOTAL_BASES,
    "batter_rbis": PlayerPropStatistic.BATTER_RBIS,
    "batter_runs_scored": PlayerPropStatistic.BATTER_RUNS_SCORED,
    "batter_hits_runs_rbis": PlayerPropStatistic.BATTER_HITS_RUNS_RBIS,
    "batter_singles": PlayerPropStatistic.BATTER_SINGLES,
    "batter_doubles": PlayerPropStatistic.BATTER_DOUBLES,
    "batter_triples": PlayerPropStatistic.BATTER_TRIPLES,
    "batter_walks": PlayerPropStatistic.BATTER_WALKS,
    "batter_strikeouts": PlayerPropStatistic.BATTER_STRIKEOUTS,
    "batter_stolen_bases": PlayerPropStatistic.BATTER_STOLEN_BASES,
    "pitcher_hits_allowed": PlayerPropStatistic.PITCHER_HITS_ALLOWED,
    "pitcher_walks": PlayerPropStatistic.PITCHER_WALKS,
    "pitcher_earned_runs": PlayerPropStatistic.PITCHER_EARNED_RUNS,
    "pitcher_outs": PlayerPropStatistic.PITCHER_OUTS,
    "batter_total_bases_alternate": PlayerPropStatistic.BATTER_TOTAL_BASES,
    "batter_home_runs_alternate": PlayerPropStatistic.BATTER_HOME_RUNS,
    "batter_hits_alternate": PlayerPropStatistic.BATTER_HITS,
    "batter_rbis_alternate": PlayerPropStatistic.BATTER_RBIS,
    "batter_walks_alternate": PlayerPropStatistic.BATTER_WALKS,
    "batter_strikeouts_alternate": PlayerPropStatistic.BATTER_STRIKEOUTS,
    "batter_runs_scored_alternate": PlayerPropStatistic.BATTER_RUNS_SCORED,
    "batter_hits_runs_rbis_alternate": PlayerPropStatistic.BATTER_HITS_RUNS_RBIS,
    "batter_singles_alternate": PlayerPropStatistic.BATTER_SINGLES,
    "batter_doubles_alternate": PlayerPropStatistic.BATTER_DOUBLES,
    "batter_triples_alternate": PlayerPropStatistic.BATTER_TRIPLES,
    "pitcher_hits_allowed_alternate": PlayerPropStatistic.PITCHER_HITS_ALLOWED,
    "pitcher_walks_alternate": PlayerPropStatistic.PITCHER_WALKS,
    "pitcher_earned_runs_alternate": PlayerPropStatistic.PITCHER_EARNED_RUNS,
    "pitcher_outs_alternate": PlayerPropStatistic.PITCHER_OUTS,
}
V8_PLAYER_PROP_MARKETS = tuple(PLAYER_PROP_MARKET_TO_STATISTIC)
V8_PLAYER_PROP_MARKET_SET = frozenset(V8_PLAYER_PROP_MARKETS)
PLAYER_PROP_ODDS_PLAN_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_ODDS_REQUEST_PLAN_V1"
PLAYER_PROP_ODDS_EVIDENCE_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_ODDS_EVIDENCE_V1"
PLAYER_PROP_ODDS_NORMALIZATION_CONTRACT_VERSION = "DSE_MLB_PLAYER_PROP_ODDS_NORMALIZATION_V1"
PLAYER_PROP_ODDS_NORMALIZATION_CALCULATION_VERSION = "DSE_MLB_PLAYER_PROP_ODDS_VIA_TOTALS_NORMALIZER_V1"
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REGION_TOKEN_RE = re.compile(r"^[a-z0-9_-]+$")


class PlayerPropsOddsError(ValueError):
    """Raised when V8 player-prop odds evidence violates its contract."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PlayerPropsOddsError(f"{name} must be non-empty trimmed text")
    return value


def _event_id(value: object) -> str:
    text = _text(value, "provider_event_id")
    if _EVENT_ID_RE.fullmatch(text) is None:
        raise PlayerPropsOddsError("provider_event_id contains unsupported characters")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PlayerPropsOddsError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PlayerPropsOddsError(f"{name} must be lowercase SHA-256")
    return text


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PlayerPropsOddsError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise PlayerPropsOddsError(f"{name} must be finite numeric")
    return result


def _regions(value: str) -> tuple[str, ...]:
    items = tuple(part.strip() for part in value.split(","))
    if (
        not items
        or len(items) != len(set(items))
        or any(not item or _REGION_TOKEN_RE.fullmatch(item) is None for item in items)
    ):
        raise PlayerPropsOddsError("regions must contain unique provider region tokens")
    return items


def provider_player_key(description: str) -> str:
    return " ".join(_text(description, "player description").casefold().split())


@dataclass(frozen=True, slots=True)
class PlayerPropsOddsRequestPlanV1:
    provider_event_id: str
    regions: tuple[str, ...]
    discovered_market_keys: tuple[str, ...]
    selected_market_keys: tuple[str, ...]
    contract_version: str = PLAYER_PROP_ODDS_PLAN_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_event_id", _event_id(self.provider_event_id))
        regions = tuple(_text(item, "region") for item in self.regions)
        if len(regions) != len(set(regions)) or any(_REGION_TOKEN_RE.fullmatch(item) is None for item in regions):
            raise PlayerPropsOddsError("player-prop request-plan regions are invalid")
        object.__setattr__(self, "regions", regions)
        discovered = tuple(sorted({_text(item, "discovered market") for item in self.discovered_market_keys}))
        selected = tuple(sorted({_text(item, "selected market") for item in self.selected_market_keys}))
        expected = tuple(item for item in discovered if item in V8_PLAYER_PROP_MARKET_SET)
        if selected != expected:
            raise PlayerPropsOddsError("selected V8 markets must equal supported discovered markets")
        object.__setattr__(self, "discovered_market_keys", discovered)
        object.__setattr__(self, "selected_market_keys", selected)
        if self.contract_version != PLAYER_PROP_ODDS_PLAN_CONTRACT_VERSION:
            raise PlayerPropsOddsError("unsupported V8 player-prop request-plan contract")

    @property
    def discovery_request_credits(self) -> int:
        return 1

    @property
    def planned_event_odds_requests(self) -> int:
        return 1 if self.selected_market_keys else 0

    @property
    def maximum_odds_credits(self) -> int:
        return len(self.selected_market_keys) * len(self.regions)

    @property
    def maximum_total_credits(self) -> int:
        return self.discovery_request_credits + self.maximum_odds_credits

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "discovered_market_keys": list(self.discovered_market_keys),
            "discovery_request_credits": self.discovery_request_credits,
            "maximum_odds_credits": self.maximum_odds_credits,
            "maximum_total_credits": self.maximum_total_credits,
            "planned_event_odds_requests": self.planned_event_odds_requests,
            "provider_event_id": self.provider_event_id,
            "regions": list(self.regions),
            "selected_market_keys": list(self.selected_market_keys),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def plan_player_prop_odds_requests(
    provider_event_id: str,
    available_market_keys: tuple[str, ...] | list[str],
    *,
    regions: str,
) -> PlayerPropsOddsRequestPlanV1:
    discovered = tuple(sorted({_text(item, "available market") for item in available_market_keys}))
    selected = tuple(item for item in discovered if item in V8_PLAYER_PROP_MARKET_SET)
    return PlayerPropsOddsRequestPlanV1(
        provider_event_id=provider_event_id,
        regions=_regions(regions),
        discovered_market_keys=discovered,
        selected_market_keys=selected,
    )


def _validate_event(event: Mapping[str, Any], expected_event_id: str) -> None:
    if str(event.get("id") or "") != expected_event_id:
        raise PlayerPropsOddsError("V8 provider event identity mismatch")
    if event.get("sport_key") != "baseball_mlb":
        raise PlayerPropsOddsError("V8 provider event sport_key must be baseball_mlb")
    home = _text(event.get("home_team"), "home_team")
    away = _text(event.get("away_team"), "away_team")
    if home == away:
        raise PlayerPropsOddsError("V8 provider event teams must differ")
    if not isinstance(event.get("bookmakers"), list):
        raise PlayerPropsOddsError("V8 provider event bookmakers must be a list")
    for book_index, bookmaker in enumerate(event.get("bookmakers", [])):
        if not isinstance(bookmaker, Mapping):
            raise PlayerPropsOddsError(f"bookmaker[{book_index}] must be an object")
        _text(bookmaker.get("key"), "bookmaker key")
        _text(bookmaker.get("title"), "bookmaker title")
        markets = bookmaker.get("markets")
        if not isinstance(markets, list):
            raise PlayerPropsOddsError("bookmaker markets must be a list")
        for market in markets:
            if not isinstance(market, Mapping):
                raise PlayerPropsOddsError("V8 player-prop market must be an object")
            market_key = str(market.get("key") or "")
            if market_key not in V8_PLAYER_PROP_MARKET_SET:
                raise PlayerPropsOddsError("event contains unsupported V8 player-prop market")
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list):
                raise PlayerPropsOddsError("V8 player-prop outcomes must be a list")
            for outcome in outcomes:
                if not isinstance(outcome, Mapping):
                    raise PlayerPropsOddsError("V8 player-prop outcome must be an object")
                if outcome.get("name") not in {"Over", "Under"}:
                    raise PlayerPropsOddsError("V8 count player props require Over/Under outcomes")
                provider_player_key(_text(outcome.get("description"), "player description"))
                point = _finite(outcome.get("point"), "player-prop point")
                if point < 0.0:
                    raise PlayerPropsOddsError("player-prop point must be nonnegative")
                _finite(outcome.get("price"), "player-prop price")


@dataclass(frozen=True, slots=True)
class PlayerPropsOddsEvidenceV1:
    provider_event_id: str
    retrieved_at: datetime
    raw_capture_checksum: str
    event: dict[str, Any]
    contract_version: str = PLAYER_PROP_ODDS_EVIDENCE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        event_id = _event_id(self.provider_event_id)
        object.__setattr__(self, "provider_event_id", event_id)
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at, "retrieved_at"))
        object.__setattr__(self, "raw_capture_checksum", _sha(self.raw_capture_checksum, "raw_capture_checksum"))
        _validate_event(self.event, event_id)
        if self.contract_version != PLAYER_PROP_ODDS_EVIDENCE_CONTRACT_VERSION:
            raise PlayerPropsOddsError("unsupported V8 player-prop odds evidence contract")

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


def _player_descriptions(event: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            for outcome in market.get("outcomes", []):
                description = str(outcome.get("description") or "")
                key = provider_player_key(description)
                result.setdefault(key, set()).add(description)
    return result


def _synthetic_event_for_player_market(
    event: Mapping[str, Any],
    *,
    player_key: str,
    market_key: str,
) -> dict[str, Any]:
    synthetic = {
        "id": event["id"],
        "sport_key": event["sport_key"],
        "sport_title": event.get("sport_title", "MLB"),
        "commence_time": event.get("commence_time"),
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "bookmakers": [],
    }
    for bookmaker in event.get("bookmakers", []):
        normalized_markets: list[dict[str, Any]] = []
        for market in bookmaker.get("markets", []):
            if market.get("key") != market_key:
                continue
            outcomes = [
                {key: value for key, value in outcome.items() if key != "description"}
                for outcome in market.get("outcomes", [])
                if provider_player_key(str(outcome.get("description") or "")) == player_key
            ]
            if outcomes:
                normalized_markets.append(
                    {
                        "key": "totals",
                        "last_update": market.get("last_update"),
                        "outcomes": outcomes,
                    }
                )
        if normalized_markets:
            synthetic["bookmakers"].append(
                {
                    "key": bookmaker.get("key"),
                    "title": bookmaker.get("title"),
                    "last_update": bookmaker.get("last_update"),
                    "markets": normalized_markets,
                }
            )
    return synthetic


def _restore_market_summary(market_summary: Mapping[str, Any], market_key: str) -> dict[str, Any]:
    restored = deepcopy(dict(market_summary))
    lines = restored.get("lines")
    if isinstance(lines, dict):
        for line in lines.values():
            if isinstance(line, dict):
                line["market_key"] = market_key
    restored["source_market_key"] = market_key
    restored["statistic"] = PLAYER_PROP_MARKET_TO_STATISTIC[market_key].value
    restored["alternate"] = market_key.endswith("_alternate")
    return restored


@dataclass(frozen=True, slots=True)
class PlayerPropsNormalizedOddsV1:
    provider_event_id: str
    source_evidence_checksum: str
    source_raw_capture_checksum: str
    summary: dict[str, Any]
    raw_snapshot_count: int
    normalized_market_count: int
    contract_version: str = PLAYER_PROP_ODDS_NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_event_id", _event_id(self.provider_event_id))
        object.__setattr__(self, "source_evidence_checksum", _sha(self.source_evidence_checksum, "source_evidence_checksum"))
        object.__setattr__(self, "source_raw_capture_checksum", _sha(self.source_raw_capture_checksum, "source_raw_capture_checksum"))
        if self.summary.get("event_id") != self.provider_event_id:
            raise PlayerPropsOddsError("normalized V8 event identity mismatch")
        if self.summary.get("player_binding") != "provider_description_only":
            raise PlayerPropsOddsError("normalized V8 player binding marker is invalid")
        if self.summary.get("contract_version") != self.contract_version:
            raise PlayerPropsOddsError("normalized V8 summary contract mismatch")
        for name in ("raw_snapshot_count", "normalized_market_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PlayerPropsOddsError(f"{name} must be nonnegative integer")
        if self.contract_version != PLAYER_PROP_ODDS_NORMALIZATION_CONTRACT_VERSION:
            raise PlayerPropsOddsError("unsupported normalized V8 player-prop odds contract")

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


def normalize_player_prop_odds(
    evidence: PlayerPropsOddsEvidenceV1,
    *,
    run_id: str = "",
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
) -> PlayerPropsNormalizedOddsV1:
    descriptions = _player_descriptions(evidence.event)
    source_market_keys = sorted(
        {
            str(market.get("key"))
            for bookmaker in evidence.event.get("bookmakers", [])
            for market in bookmaker.get("markets", [])
        }
    )
    players: dict[str, Any] = {}
    raw_snapshot_count = 0
    normalized_market_count = 0
    home_team_key: str | None = None
    away_team_key: str | None = None
    for player_key in sorted(descriptions):
        player_markets: dict[str, Any] = {}
        for market_key in source_market_keys:
            synthetic = _synthetic_event_for_player_market(
                evidence.event,
                player_key=player_key,
                market_key=market_key,
            )
            if not synthetic["bookmakers"]:
                continue
            processed = process_game(
                synthetic,
                run_id=run_id,
                retrieved_at=evidence.retrieved_at.isoformat(),
                freshness_thresholds=freshness_thresholds,
                consensus_thresholds=consensus_thresholds,
                history_rows=(),
            )
            home_team_key = home_team_key or processed.summary.get("home_team_key")
            away_team_key = away_team_key or processed.summary.get("away_team_key")
            totals = processed.summary.get("markets", {}).get("totals")
            if isinstance(totals, Mapping):
                player_markets[market_key] = _restore_market_summary(totals, market_key)
                raw_snapshot_count += processed.raw_snapshot_count
                normalized_market_count += processed.normalized_market_count
        if player_markets:
            players[player_key] = {
                "provider_player_key": player_key,
                "provider_descriptions": sorted(descriptions[player_key]),
                "markets": player_markets,
            }
    summary = {
        "away_team": evidence.event.get("away_team"),
        "away_team_key": away_team_key,
        "calculation_version": PLAYER_PROP_ODDS_NORMALIZATION_CALCULATION_VERSION,
        "contract_version": PLAYER_PROP_ODDS_NORMALIZATION_CONTRACT_VERSION,
        "event_id": evidence.provider_event_id,
        "home_team": evidence.event.get("home_team"),
        "home_team_key": home_team_key,
        "market_period": "full_game",
        "player_binding": "provider_description_only",
        "players": players,
        "source_market_keys": source_market_keys,
    }
    return PlayerPropsNormalizedOddsV1(
        provider_event_id=evidence.provider_event_id,
        source_evidence_checksum=evidence.checksum,
        source_raw_capture_checksum=evidence.raw_capture_checksum,
        summary=summary,
        raw_snapshot_count=raw_snapshot_count,
        normalized_market_count=normalized_market_count,
    )
