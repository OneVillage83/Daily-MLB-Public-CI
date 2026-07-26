from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from statistics import fmean, median, pstdev
from typing import Any

from app.team_aliases import team_key

CALCULATION_VERSION = "odds-v3-provider-snapshots"
ODDS_CONSENSUS_CONTRACT_VERSION = "odds-consensus-v2"
SUPPORTED_MARKETS = frozenset({"h2h", "spreads", "totals"})


class FreshnessStatus(str, Enum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


class OddsWarningCode(str, Enum):
    UNKNOWN_TEAM = "unknown_team"
    MALFORMED_EVENT = "malformed_event"
    MALFORMED_BOOKMAKER = "malformed_bookmaker"
    MALFORMED_MARKET = "malformed_market"
    MALFORMED_OUTCOME = "malformed_outcome"
    UNSUPPORTED_MARKET = "unsupported_market"
    INCOMPLETE_TWO_WAY_MARKET = "incomplete_two_way_market"
    INVALID_SPREAD_PAIR = "invalid_spread_pair"
    MISMATCHED_TOTAL_PAIR = "mismatched_total_pair"
    MISSING_PROVIDER_TIMESTAMP = "missing_provider_timestamp"
    STALE_MARKET = "stale_market"
    INSUFFICIENT_BOOKMAKERS = "insufficient_bookmakers"
    PRIMARY_LINE_TIE = "primary_line_tie"
    DUPLICATE_NORMALIZED_OFFER = "duplicate_normalized_offer"
    INVALID_PROVIDER_TIMESTAMP = "invalid_provider_timestamp"
    FUTURE_PROVIDER_TIMESTAMP = "future_provider_timestamp"


@dataclass(frozen=True, slots=True)
class FreshnessThresholds:
    fresh_seconds: int = 120
    stale_seconds: int = 300
    future_tolerance_seconds: int = 30

    def __post_init__(self) -> None:
        if not 0 <= self.fresh_seconds < self.stale_seconds:
            raise ValueError("freshness thresholds must be nonnegative and ordered")
        if self.future_tolerance_seconds < 0:
            raise ValueError("future timestamp tolerance must be nonnegative")


@dataclass(frozen=True, slots=True)
class ConsensusThresholds:
    minimum_books: int = 2
    moderate_books: int = 4
    high_books: int = 7

    def __post_init__(self) -> None:
        if not 1 <= self.minimum_books <= self.moderate_books <= self.high_books:
            raise ValueError("consensus book thresholds must be positive and ordered")


@dataclass(frozen=True, slots=True)
class OddsProcessingResult:
    summary: dict[str, Any]
    annotated_game: dict[str, Any]
    warnings: tuple[dict[str, Any], ...]
    raw_snapshot_count: int
    normalized_market_count: int
    freshness_counts: dict[str, int]


def _finite_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _american_price(value: object) -> float | None:
    price = _finite_number(value)
    if price is None or abs(price) < 100.0:
        return None
    return price


def american_to_probability(price: object) -> float | None:
    normalized = _american_price(price)
    if normalized is None:
        return None
    if normalized > 0:
        return 100.0 / (normalized + 100.0)
    return abs(normalized) / (abs(normalized) + 100.0)


def best_price(prices: Iterable[object]) -> float | None:
    valid_prices = [price for value in prices if (price := _american_price(value)) is not None]
    return max(valid_prices) if valid_prices else None


def no_vig_probabilities(
    first_probability: object, second_probability: object
) -> tuple[float, float] | None:
    first = _finite_number(first_probability)
    second = _finite_number(second_probability)
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    total = first + second
    if not math.isfinite(total) or total <= 0:
        return None
    return first / total, second / total


def calculate_hold(first_probability: object, second_probability: object) -> float | None:
    first = _finite_number(first_probability)
    second = _finite_number(second_probability)
    if first is None or second is None or first <= 0 or second <= 0:
        return None
    return first + second - 1.0


def _utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _warning(
    code: OddsWarningCode,
    *,
    run_id: str,
    event_id: str | None,
    bookmaker: str | None,
    market: str | None,
    message: str,
    created_at: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "event_id": event_id,
        "bookmaker": bookmaker,
        "market": market,
        "code": code.value,
        "message": message,
        "created_at": created_at,
    }


def _timestamp_age(
    value: object,
    retrieved_at: datetime,
    thresholds: FreshnessThresholds,
) -> tuple[datetime | None, float | None, OddsWarningCode | None]:
    if value is None or value == "":
        return None, None, None
    parsed = _utc(value)
    if parsed is None:
        return None, None, OddsWarningCode.INVALID_PROVIDER_TIMESTAMP
    age = (retrieved_at - parsed).total_seconds()
    if age < -thresholds.future_tolerance_seconds:
        return None, None, OddsWarningCode.FUTURE_PROVIDER_TIMESTAMP
    return parsed, max(age, 0.0), None


def _freshness(age: float | None, thresholds: FreshnessThresholds) -> FreshnessStatus:
    if age is None:
        return FreshnessStatus.UNKNOWN
    if age <= thresholds.fresh_seconds:
        return FreshnessStatus.FRESH
    if age <= thresholds.stale_seconds:
        return FreshnessStatus.AGING
    return FreshnessStatus.STALE


def _display_median_price(prices: Iterable[object]) -> float | None:
    valid_prices = [price for value in prices if (price := _american_price(value)) is not None]
    if not valid_prices:
        return None
    candidate = float(median(valid_prices))
    return candidate if _american_price(candidate) is not None else None


def _line_key(market_key: str, line: float | None, valid: bool) -> str:
    if market_key == "h2h":
        return "moneyline"
    if not valid or line is None:
        return "unpointed"
    value = 0.0 if line == 0 else line
    return f"{value:+g}" if market_key == "spreads" else f"{value:g}"


def _canonical_line(
    market_key: str,
    outcome_name: str,
    point: float | None,
    home_team: str,
    away_team: str,
) -> tuple[float | None, bool, str]:
    if market_key == "h2h":
        return None, True, "moneyline"
    if point is None:
        return None, False, "unknown"
    if market_key == "spreads":
        if outcome_name == home_team:
            return point, True, "home_team_spread"
        if outcome_name == away_team:
            return -point, True, "home_team_spread"
        return None, False, "unknown"
    if market_key == "totals" and outcome_name in {"Over", "Under"}:
        return point, True, "total"
    return None, False, "unknown"


def _expected_sides(
    market_key: str,
    home_side: str,
    away_side: str,
) -> tuple[str, str] | None:
    if market_key in {"h2h", "spreads"}:
        return home_side, away_side
    if market_key == "totals":
        return "Over", "Under"
    return None


def _confidence(book_count: int, thresholds: ConsensusThresholds) -> str:
    if book_count < thresholds.minimum_books:
        return "insufficient"
    if book_count < thresholds.moderate_books:
        return "low"
    if book_count < thresholds.high_books:
        return "moderate"
    return "high"


def _bookmaker_identity(book: Mapping[str, Any], index: int) -> str:
    return str(book.get("key") or book.get("title") or f"book-{index}")


def _probability_metrics(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "max": None, "range": None, "standard_deviation": None}
    minimum = min(values)
    maximum = max(values)
    return {
        "min": minimum,
        "max": maximum,
        "range": maximum - minimum,
        "standard_deviation": pstdev(values),
    }


def _choose_offer(offers: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        offers,
        key=lambda offer: (
            offer.get("effective_timestamp_epoch") is not None,
            offer.get("effective_timestamp_epoch") or float("-inf"),
            int(offer["provider_order"]),
        ),
    )


def _summarize_outcome(
    offers: list[dict[str, Any]],
    *,
    thresholds: ConsensusThresholds,
    warnings: list[dict[str, Any]],
    warning_context: dict[str, Any],
) -> dict[str, Any]:
    by_book: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        by_book[str(offer["bookmaker_key"])].append(offer)

    selected: dict[str, dict[str, Any]] = {}
    for bookmaker, candidates in by_book.items():
        if len(candidates) > 1:
            warnings.append(
                _warning(
                    OddsWarningCode.DUPLICATE_NORMALIZED_OFFER,
                    bookmaker=bookmaker,
                    message=(
                        "Bookmaker supplied multiple offers for the same normalized "
                        "market, side, and point; one contribution was selected for consensus"
                    ),
                    **warning_context,
                )
            )
        selected[bookmaker] = _choose_offer(candidates)

    prices_by_book = {
        bookmaker: price
        for bookmaker, offer in selected.items()
        if offer.get("calculation_eligible", True)
        if (price := _american_price(offer.get("price"))) is not None
    }
    probabilities_by_book = {
        bookmaker: probability
        for bookmaker, price in prices_by_book.items()
        if (probability := american_to_probability(price)) is not None
    }
    prices = list(prices_by_book.values())
    probabilities = list(probabilities_by_book.values())
    selected_best = best_price(prices)
    best_books = sorted(
        bookmaker for bookmaker, price in prices_by_book.items() if price == selected_best
    )
    book_count = len(prices_by_book)
    if book_count < thresholds.minimum_books:
        warnings.append(
            _warning(
                OddsWarningCode.INSUFFICIENT_BOOKMAKERS,
                bookmaker=None,
                message=f"Only {book_count} distinct bookmaker(s) contributed a valid price",
                **warning_context,
            )
        )

    return {
        "name": str(offers[0]["side_key"]),
        "raw_names": sorted({str(offer["raw_name"]) for offer in offers}),
        "point": offers[0].get("point"),
        "bookmaker_count": book_count,
        "book_count": book_count,
        "consensus_confidence": _confidence(book_count, thresholds),
        "offer_count": len(offers),
        "prices_by_book": prices_by_book,
        "best_price": selected_best,
        "best_price_book": best_books[0] if len(best_books) == 1 else None,
        "best_price_books": best_books,
        "best_price_implied_probability": american_to_probability(selected_best),
        "median_american_price": _display_median_price(prices),
        "median_american_price_display_only": _display_median_price(prices),
        "median_price": _display_median_price(prices),
        "implied_probabilities": probabilities_by_book,
        "median_implied_probability": float(median(probabilities)) if probabilities else None,
        "mean_implied_probability": fmean(probabilities) if probabilities else None,
        "consensus_implied_probability": float(median(probabilities)) if probabilities else None,
        "implied_probability_disagreement": _probability_metrics(probabilities),
        "no_vig_probabilities": {},
        "median_no_vig_probability": None,
        "mean_no_vig_probability": None,
        "no_vig_probability": None,
        "no_vig_probability_disagreement": _probability_metrics([]),
        "selected_offers_by_book": selected,
        "offers": offers,
    }


def _pair_line(
    market_key: str,
    line: dict[str, Any],
    expected: tuple[str, str] | None,
) -> None:
    outcomes = line["outcomes"]
    holds: dict[str, float] = {}
    if (
        not line.get("valid_line")
        or expected is None
        or any(side not in outcomes for side in expected)
    ):
        for outcome in outcomes.values():
            outcome.pop("selected_offers_by_book", None)
            outcome["no_vig_probability"] = None
        line["complete_two_way_market"] = False
        line["market_hold_by_book"] = holds
        line["median_market_hold"] = None
        line["market_hold"] = None
        return

    first_side, second_side = expected
    first_selected = outcomes[first_side].pop("selected_offers_by_book")
    second_selected = outcomes[second_side].pop("selected_offers_by_book")
    paired_books = sorted(set(first_selected) & set(second_selected))
    first_no_vig: dict[str, float] = {}
    second_no_vig: dict[str, float] = {}
    for bookmaker in paired_books:
        first_offer = first_selected[bookmaker]
        second_offer = second_selected[bookmaker]
        if not first_offer.get("calculation_eligible", True) or not second_offer.get(
            "calculation_eligible", True
        ):
            continue
        first_probability = american_to_probability(first_offer.get("price"))
        second_probability = american_to_probability(second_offer.get("price"))
        if first_probability is None or second_probability is None:
            continue
        if market_key == "spreads":
            first_point = _finite_number(first_offer.get("point"))
            second_point = _finite_number(second_offer.get("point"))
            if first_point is None or second_point is None or not math.isclose(
                first_point, -second_point, abs_tol=1e-9
            ):
                continue
        if market_key == "totals":
            first_point = _finite_number(first_offer.get("point"))
            second_point = _finite_number(second_offer.get("point"))
            if first_point is None or second_point is None or not math.isclose(
                first_point, second_point, abs_tol=1e-9
            ):
                continue
        no_vig = no_vig_probabilities(first_probability, second_probability)
        hold = calculate_hold(first_probability, second_probability)
        if no_vig is None or hold is None:
            continue
        first_no_vig[bookmaker], second_no_vig[bookmaker] = no_vig
        holds[bookmaker] = hold

    for side, values in ((first_side, first_no_vig), (second_side, second_no_vig)):
        probabilities = list(values.values())
        outcomes[side]["no_vig_probabilities"] = values
        outcomes[side]["median_no_vig_probability"] = (
            float(median(probabilities)) if probabilities else None
        )
        outcomes[side]["mean_no_vig_probability"] = (
            fmean(probabilities) if probabilities else None
        )
        outcomes[side]["no_vig_probability_disagreement"] = _probability_metrics(
            probabilities
        )
        outcomes[side]["no_vig_probability"] = outcomes[side][
            "median_no_vig_probability"
        ]

    for side, outcome in outcomes.items():
        outcome.pop("selected_offers_by_book", None)
        if side not in expected:
            outcome["no_vig_probability"] = None

    line["complete_two_way_market"] = bool(holds)
    line["market_hold_by_book"] = holds
    line["median_market_hold"] = float(median(holds.values())) if holds else None
    line["market_hold"] = line["median_market_hold"]


def _median_timestamp_epoch(line: dict[str, Any]) -> float | None:
    values: dict[str, float] = {}
    for outcome in line["outcomes"].values():
        for offer in outcome["offers"]:
            if not offer.get("calculation_eligible", True):
                continue
            if _american_price(offer.get("price")) is None:
                continue
            bookmaker = str(offer["bookmaker_key"])
            timestamp = offer.get("effective_timestamp_epoch")
            if timestamp is not None:
                values[bookmaker] = max(values.get(bookmaker, float("-inf")), float(timestamp))
    return float(median(values.values())) if values else None


def _point_metrics(lines: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    valid = [
        line
        for line in lines.values()
        if line.get("valid_line")
        and line.get("line") is not None
        and int(line.get("bookmaker_count") or 0) > 0
    ]
    if not valid:
        return {
            "unique_point_count": 0,
            "min_point": None,
            "max_point": None,
            "point_range": None,
            "modal_point": None,
            "books_at_modal_point": [],
        }
    points = [float(line["line"]) for line in valid]
    counts = {float(line["line"]): int(line["bookmaker_count"]) for line in valid}
    max_count = max(counts.values())
    modal = min(point for point, count in counts.items() if count == max_count)
    modal_line = next(line for line in valid if float(line["line"]) == modal)
    modal_books = sorted(
        {
            str(offer["bookmaker_key"])
            for outcome in modal_line["outcomes"].values()
            for offer in outcome["offers"]
            if offer.get("calculation_eligible", True)
            if _american_price(offer.get("price")) is not None
        }
    )
    return {
        "unique_point_count": len(set(points)),
        "min_point": min(points),
        "max_point": max(points),
        "point_range": max(points) - min(points),
        "modal_point": modal,
        "books_at_modal_point": modal_books,
    }


def _select_primary_line(
    lines: Mapping[str, dict[str, Any]],
    *,
    warnings: list[dict[str, Any]],
    warning_context: dict[str, Any],
) -> str | None:
    candidates = [
        (key, line)
        for key, line in lines.items()
        if line.get("valid_line")
        and line.get("line") is not None
        and int(line.get("bookmaker_count") or 0) > 0
    ]
    if not candidates:
        return None
    highest_count = max(int(line["bookmaker_count"]) for _, line in candidates)
    tied = [(key, line) for key, line in candidates if int(line["bookmaker_count"]) == highest_count]
    tie_stage: str | None = None
    if len(tied) > 1:
        tie_stage = "median_recency"
        known = [(key, line) for key, line in tied if line["median_effective_timestamp_epoch"] is not None]
        if known:
            newest = max(float(line["median_effective_timestamp_epoch"]) for _, line in known)
            tied = [
                (key, line)
                for key, line in known
                if float(line["median_effective_timestamp_epoch"]) == newest
            ]
        if len(tied) > 1:
            tie_stage = "numeric_lowest"
            lowest = min(float(line["line"]) for _, line in tied)
            tied = [(key, line) for key, line in tied if float(line["line"]) == lowest]
        warnings.append(
            _warning(
                OddsWarningCode.PRIMARY_LINE_TIE,
                bookmaker=None,
                message=f"Primary line tie resolved using {tie_stage}",
                **warning_context,
            )
        )
    return tied[0][0]


def _pairing_warnings(
    offers: list[dict[str, Any]],
    *,
    market_key: str,
    expected: tuple[str, str] | None,
    warnings: list[dict[str, Any]],
    context: dict[str, Any],
) -> None:
    if expected is None:
        return
    by_book: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for offer in offers:
        by_book[str(offer["bookmaker_key"])].append(offer)
    for bookmaker, book_offers in by_book.items():
        first_offers = [offer for offer in book_offers if offer["side_key"] == expected[0]]
        second_offers = [offer for offer in book_offers if offer["side_key"] == expected[1]]
        if not first_offers or not second_offers:
            continue
        first_points = [
            point
            for offer in first_offers
            if (point := _finite_number(offer.get("point"))) is not None
        ]
        second_points = [
            point
            for offer in second_offers
            if (point := _finite_number(offer.get("point"))) is not None
        ]
        valid_spread_pair = any(
            math.isclose(first, -second, abs_tol=1e-9)
            for first in first_points
            for second in second_points
        )
        if market_key == "spreads" and not valid_spread_pair:
            warnings.append(
                _warning(
                    OddsWarningCode.INVALID_SPREAD_PAIR,
                    bookmaker=bookmaker,
                    message="Spread outcomes do not have mathematically opposite points",
                    **context,
                )
            )
        valid_total_pair = any(
            math.isclose(first, second, abs_tol=1e-9)
            for first in first_points
            for second in second_points
        )
        if market_key == "totals" and not valid_total_pair:
            warnings.append(
                _warning(
                    OddsWarningCode.MISMATCHED_TOTAL_PAIR,
                    bookmaker=bookmaker,
                    message="Over and Under outcomes do not have the same total point",
                    **context,
                )
            )


def process_game(
    game: dict[str, Any],
    *,
    run_id: str = "",
    retrieved_at: str | None = None,
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
    history_rows: Sequence[Mapping[str, Any]] = (),
) -> OddsProcessingResult:
    freshness_thresholds = freshness_thresholds or FreshnessThresholds()
    consensus_thresholds = consensus_thresholds or ConsensusThresholds()
    annotated = deepcopy(game)
    event_id = str(game.get("id")) if game.get("id") is not None else None
    retrieved_text = retrieved_at or str(game.get("retrieved_at") or datetime.now(timezone.utc).isoformat())
    retrieved = _utc(retrieved_text)
    if retrieved is None:
        raise ValueError("retrieved_at must be a timezone-aware ISO-8601 timestamp")
    created_at = retrieved.isoformat()
    warnings: list[dict[str, Any]] = []

    home_raw = str(game.get("home_team") or "")
    away_raw = str(game.get("away_team") or "")
    home_key = team_key(home_raw)
    away_key = team_key(away_raw)
    home_side = home_key or home_raw
    away_side = away_key or away_raw
    for raw_name, canonical in ((home_raw, home_key), (away_raw, away_key)):
        if raw_name and canonical is None:
            warnings.append(
                _warning(
                    OddsWarningCode.UNKNOWN_TEAM,
                    run_id=run_id,
                    event_id=event_id,
                    bookmaker=None,
                    market=None,
                    message="Provider team name has no exact canonical MLB mapping",
                    created_at=created_at,
                )
            )

    provider_value = game.get("last_update")
    provider_timestamp, provider_age, provider_warning = _timestamp_age(
        provider_value, retrieved, freshness_thresholds
    )
    if provider_warning is not None:
        warnings.append(
            _warning(
                provider_warning,
                run_id=run_id,
                event_id=event_id,
                bookmaker=None,
                market=None,
                message="Provider-level payload timestamp is invalid or outside allowed future skew",
                created_at=created_at,
            )
        )

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    all_market_offers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    freshness_counts: Counter[str] = Counter()
    bookmakers = annotated.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        bookmakers = []
    raw_snapshot_count = sum(
        len(market.get("outcomes", []))
        for bookmaker in bookmakers
        if isinstance(bookmaker, dict)
        for market in bookmaker.get("markets", [])
        if isinstance(market, dict) and isinstance(market.get("outcomes", []), list)
    )
    provider_order = 0

    for book_index, book in enumerate(bookmakers):
        if not isinstance(book, dict):
            continue
        bookmaker_key = _bookmaker_identity(book, book_index)
        book_timestamp, book_age, book_warning = _timestamp_age(
            book.get("last_update"), retrieved, freshness_thresholds
        )
        if book_warning is not None:
            warnings.append(
                _warning(
                    book_warning,
                    run_id=run_id,
                    event_id=event_id,
                    bookmaker=bookmaker_key,
                    market=None,
                    message="Bookmaker last_update is invalid or outside allowed future skew",
                    created_at=created_at,
                )
            )
        book["provider_age_seconds"] = provider_age
        book["bookmaker_age_seconds"] = book_age
        markets = book.get("markets", [])
        if not isinstance(markets, list):
            continue
        for market_index, market in enumerate(markets):
            if not isinstance(market, dict):
                continue
            market_key = str(market.get("key") or "unknown")
            if market_key not in SUPPORTED_MARKETS:
                warnings.append(
                    _warning(
                        OddsWarningCode.UNSUPPORTED_MARKET,
                        run_id=run_id,
                        event_id=event_id,
                        bookmaker=bookmaker_key,
                        market=market_key,
                        message="Unsupported market excluded from consensus",
                        created_at=created_at,
                    )
                )
                continue
            market_timestamp, market_age, market_warning = _timestamp_age(
                market.get("last_update"), retrieved, freshness_thresholds
            )
            if market_warning is not None:
                warnings.append(
                    _warning(
                        market_warning,
                        run_id=run_id,
                        event_id=event_id,
                        bookmaker=bookmaker_key,
                        market=market_key,
                        message="Market last_update is invalid or outside allowed future skew",
                        created_at=created_at,
                    )
                )
            effective_timestamp = market_timestamp or book_timestamp
            effective_age = market_age if market_timestamp is not None else book_age
            status = _freshness(effective_age, freshness_thresholds)
            if effective_timestamp is None:
                warnings.append(
                    _warning(
                        OddsWarningCode.MISSING_PROVIDER_TIMESTAMP,
                        run_id=run_id,
                        event_id=event_id,
                        bookmaker=bookmaker_key,
                        market=market_key,
                        message="No usable market or bookmaker provider timestamp",
                        created_at=created_at,
                    )
                )
            if status is FreshnessStatus.STALE:
                warnings.append(
                    _warning(
                        OddsWarningCode.STALE_MARKET,
                        run_id=run_id,
                        event_id=event_id,
                        bookmaker=bookmaker_key,
                        market=market_key,
                        message="Market capture exceeds the configured stale threshold",
                        created_at=created_at,
                    )
                )
            freshness_counts[status.value] += 1
            market.update(
                {
                    "provider_age_seconds": provider_age,
                    "bookmaker_age_seconds": book_age,
                    "market_age_seconds": market_age,
                    "freshness_status": status.value,
                    "effective_provider_timestamp": _iso(effective_timestamp),
                }
            )
            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue
            outcome_names = {
                str(outcome.get("name") or "")
                for outcome in outcomes
                if isinstance(outcome, dict)
            }
            allowed_names = (
                {home_raw, away_raw}
                if market_key in {"h2h", "spreads"}
                else {"Over", "Under"}
            )
            valid_point_shape = market_key != "h2h" or all(
                not isinstance(outcome, dict) or outcome.get("point") is None
                for outcome in outcomes
            )
            calculation_eligible = outcome_names <= allowed_names and valid_point_shape
            if not calculation_eligible:
                warnings.append(
                    _warning(
                        OddsWarningCode.MALFORMED_OUTCOME,
                        run_id=run_id,
                        event_id=event_id,
                        bookmaker=bookmaker_key,
                        market=market_key,
                        message="Market contains an outcome outside its supported two-way shape",
                        created_at=created_at,
                    )
                )
            for outcome_index, outcome in enumerate(outcomes):
                if not isinstance(outcome, dict):
                    continue
                provider_order += 1
                raw_name = str(outcome.get("name") or "")
                point = _finite_number(outcome.get("point"))
                if market_key in {"h2h", "spreads"}:
                    if raw_name == home_raw:
                        side_key = home_side
                    elif raw_name == away_raw:
                        side_key = away_side
                    else:
                        warnings.append(
                            _warning(
                                OddsWarningCode.UNKNOWN_TEAM,
                                run_id=run_id,
                                event_id=event_id,
                                bookmaker=bookmaker_key,
                                market=market_key,
                                message="Outcome team does not exactly match either event team",
                                created_at=created_at,
                            )
                        )
                        continue
                else:
                    side_key = raw_name
                line, valid_line, line_basis = _canonical_line(
                    market_key, raw_name, point, home_raw, away_raw
                )
                key = _line_key(market_key, line, valid_line)
                if not valid_line and market_key != "h2h":
                    key = f"unpointed:{bookmaker_key}:{market_index}:{outcome_index}"
                group = groups.setdefault(
                    (market_key, key),
                    {
                        "market_key": market_key,
                        "line_key": key,
                        "line": line,
                        "line_basis": line_basis,
                        "valid_line": valid_line,
                        "offers": [],
                    },
                )
                offer = {
                    "bookmaker_key": bookmaker_key,
                    "bookmaker_title": book.get("title"),
                    "raw_name": raw_name,
                    "side_key": side_key,
                    "price": outcome.get("price"),
                    "normalized_price": _american_price(outcome.get("price")),
                    "implied_probability": american_to_probability(outcome.get("price")),
                    "point": point,
                    "raw_point": outcome.get("point"),
                    "bookmaker_last_update": book.get("last_update"),
                    "market_last_update": market.get("last_update"),
                    "effective_provider_timestamp": _iso(effective_timestamp),
                    "effective_timestamp_epoch": (
                        effective_timestamp.timestamp() if effective_timestamp is not None else None
                    ),
                    "provider_retrieved_at": retrieved.isoformat(),
                    "provider_age_seconds": provider_age,
                    "bookmaker_age_seconds": book_age,
                    "market_age_seconds": market_age,
                    "freshness_status": status.value,
                    "provider_order": provider_order,
                    "calculation_eligible": calculation_eligible,
                }
                group["offers"].append(offer)
                all_market_offers[market_key].append(offer)

    markets_summary: dict[str, dict[str, Any]] = {}
    for market_key in sorted({key[0] for key in groups}):
        context: dict[str, Any] = {
            "run_id": run_id,
            "event_id": event_id,
            "market": market_key,
            "created_at": created_at,
        }
        expected = _expected_sides(market_key, home_side, away_side)
        _pairing_warnings(
            all_market_offers[market_key],
            market_key=market_key,
            expected=expected,
            warnings=warnings,
            context=context,
        )
        lines: dict[str, dict[str, Any]] = {}
        market_groups = [group for (key, _), group in groups.items() if key == market_key]
        for group in sorted(market_groups, key=lambda item: str(item["line_key"])):
            by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for offer in group["offers"]:
                by_side[str(offer["side_key"])].append(offer)
            outcomes = {
                side: _summarize_outcome(
                    offers,
                    thresholds=consensus_thresholds,
                    warnings=warnings,
                    warning_context=context,
                )
                for side, offers in sorted(by_side.items())
            }
            valid_books = {
                str(offer["bookmaker_key"])
                for offer in group["offers"]
                if offer.get("calculation_eligible", True)
                if _american_price(offer.get("price")) is not None
            }
            line_summary: dict[str, Any] = {
                "market_key": market_key,
                "line_key": group["line_key"],
                "line": group["line"],
                "line_point": group["line"],
                "line_basis": group["line_basis"],
                "valid_line": group["valid_line"],
                "bookmaker_count": len(valid_books),
                "book_count": len(valid_books),
                "median_effective_timestamp_epoch": None,
                "median_effective_provider_timestamp": None,
                "outcomes": outcomes,
            }
            line_summary["median_effective_timestamp_epoch"] = _median_timestamp_epoch(
                line_summary
            )
            epoch = line_summary["median_effective_timestamp_epoch"]
            line_summary["median_effective_provider_timestamp"] = (
                datetime.fromtimestamp(epoch, timezone.utc).isoformat()
                if epoch is not None
                else None
            )
            _pair_line(market_key, line_summary, expected)
            if not line_summary["complete_two_way_market"]:
                warnings.append(
                    _warning(
                        OddsWarningCode.INCOMPLETE_TWO_WAY_MARKET,
                        bookmaker=None,
                        message=f"No valid per-book two-way pair for line {group['line_key']}",
                        **context,
                    )
                )
            lines[str(group["line_key"])] = line_summary

        market_summary: dict[str, Any] = {"lines": lines}
        if market_key in {"spreads", "totals"}:
            primary_key = _select_primary_line(
                lines,
                warnings=warnings,
                warning_context=context,
            )
            market_summary["primary_line_key"] = primary_key
            market_summary["primary_market_point"] = (
                lines[primary_key]["line"] if primary_key is not None else None
            )
            market_summary["primary_line"] = lines.get(primary_key) if primary_key else None
            market_summary["alternate_line_keys"] = [
                key
                for key in lines
                if key != primary_key
                and lines[key].get("valid_line")
                and int(lines[key].get("bookmaker_count") or 0) > 0
            ]
            market_summary["alternate_market_points"] = [
                lines[key]["line"] for key in market_summary["alternate_line_keys"]
            ]
            market_summary["alternate_lines"] = {
                key: lines[key] for key in market_summary["alternate_line_keys"]
            }
            market_summary["point_disagreement"] = _point_metrics(lines)
        markets_summary[market_key] = market_summary

    for status in FreshnessStatus:
        freshness_counts.setdefault(status.value, 0)
    summary: dict[str, Any] = {
        "contract_version": ODDS_CONSENSUS_CONTRACT_VERSION,
        "event_id": event_id,
        "sport_key": game.get("sport_key"),
        "commence_time": game.get("commence_time"),
        "raw_home_team": home_raw,
        "raw_away_team": away_raw,
        "home_team": home_raw,
        "away_team": away_raw,
        "home_team_key": home_key,
        "away_team_key": away_key,
        "retrieval_summary": {
            "retrieved_at": retrieved.isoformat(),
            "provider_timestamp": _iso(provider_timestamp),
            "provider_age_seconds": provider_age,
        },
        "provider_freshness_summary": dict(freshness_counts),
        "calculation_version": CALCULATION_VERSION,
        "markets": markets_summary,
        "best_prices": {
            market: {
                line_key: {
                    side: {
                        "best_price": outcome["best_price"],
                        "best_price_book": outcome["best_price_book"],
                        "best_price_books": outcome["best_price_books"],
                    }
                    for side, outcome in line["outcomes"].items()
                }
                for line_key, line in market_summary["lines"].items()
            }
            for market, market_summary in markets_summary.items()
        },
        "line_movement": calculate_line_movement(history_rows),
        "warnings": warnings,
    }
    h2h_line = markets_summary.get("h2h", {}).get("lines", {}).get("moneyline")
    if h2h_line is not None:
        summary["market_hold"] = h2h_line["median_market_hold"]
    normalized_market_count = sum(
        len(market_summary["lines"]) for market_summary in markets_summary.values()
    )
    return OddsProcessingResult(
        summary=summary,
        annotated_game=annotated,
        warnings=tuple(warnings),
        raw_snapshot_count=raw_snapshot_count,
        normalized_market_count=normalized_market_count,
        freshness_counts=dict(freshness_counts),
    )


def summarize_game(game: dict[str, Any]) -> dict[str, Any]:
    return process_game(game).summary


def calculate_line_movement(
    history_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped: dict[
        tuple[str, str, str, float | None],
        dict[tuple[str, str], list[Mapping[str, Any]]],
    ] = defaultdict(lambda: defaultdict(list))
    eligible_groups = _eligible_history_groups(history_rows)
    for row in history_rows:
        market = str(row.get("market_key") or "")
        if market not in SUPPORTED_MARKETS:
            continue
        group_key = _history_group_key(row)
        if group_key not in eligible_groups:
            continue
        if american_to_probability(row.get("price")) is None:
            continue
        if market == "h2h" and row.get("point") is not None:
            continue
        outcome = _history_side(row, market)
        point = _finite_number(row.get("point")) if market != "h2h" else None
        capture = (str(row.get("run_id") or ""), str(row.get("retrieved_at") or ""))
        grouped[(str(row.get("bookmaker_key") or ""), market, outcome, point)][
            capture
        ].append(row)

    offers: list[dict[str, Any]] = []
    for (bookmaker, market, outcome, point), captures in sorted(
        grouped.items(), key=lambda item: str(item[0])
    ):
        rows = [_select_history_capture(rows) for rows in captures.values()]
        ordered = sorted(
            rows,
            key=lambda row: (str(row.get("retrieved_at") or ""), int(row.get("id") or 0)),
        )
        first = ordered[0]
        latest = ordered[-1]
        first_price = _american_price(first.get("price"))
        latest_price = _american_price(latest.get("price"))
        first_probability = american_to_probability(first_price)
        latest_probability = american_to_probability(latest_price)
        has_movement = len(ordered) > 1
        offers.append(
            {
                "bookmaker_key": bookmaker,
                "market": market,
                "side": outcome,
                "point": point,
                "first_observed_price": first_price,
                "latest_observed_price": latest_price,
                "american_price_change": (
                    latest_price - first_price
                    if has_movement and first_price is not None and latest_price is not None
                    else None
                ),
                "first_implied_probability": first_probability,
                "latest_implied_probability": latest_probability,
                "implied_probability_change": (
                    latest_probability - first_probability
                    if has_movement
                    and first_probability is not None
                    and latest_probability is not None
                    else None
                ),
                "first_observed_at": first.get("retrieved_at"),
                "latest_observed_at": latest.get("retrieved_at"),
                "observation_count": len(ordered),
            }
        )
    return {
        "terminology": "first_observed_and_latest_observed",
        "american_price_change_interpretation": "traceability_only_not_linear_market_movement",
        "offers": offers,
        "market_primary_line_movement": _primary_market_movement(history_rows),
    }


def _history_group_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("run_id") or ""),
        str(row.get("retrieved_at") or ""),
        str(row.get("bookmaker_key") or ""),
        str(row.get("market_key") or ""),
    )


def _eligible_history_groups(
    history_rows: Sequence[Mapping[str, Any]],
) -> set[tuple[str, str, str, str]]:
    groups: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in history_rows:
        groups[_history_group_key(row)].append(row)
    eligible: set[tuple[str, str, str, str]] = set()
    for key, rows in groups.items():
        market = key[3]
        if market not in SUPPORTED_MARKETS:
            continue
        home_raw = str(rows[0].get("home_team") or "")
        away_raw = str(rows[0].get("away_team") or "")
        home_side = str(rows[0].get("home_team_key") or team_key(home_raw) or home_raw)
        away_side = str(rows[0].get("away_team_key") or team_key(away_raw) or away_raw)
        allowed = {home_side, away_side} if market in {"h2h", "spreads"} else {"Over", "Under"}
        sides = {_history_side(row, market) for row in rows}
        valid_h2h_points = market != "h2h" or all(row.get("point") is None for row in rows)
        if sides <= allowed and valid_h2h_points:
            eligible.add(key)
    return eligible


def _history_side(row: Mapping[str, Any], market: str) -> str:
    raw = str(row.get("outcome_name") or "")
    if market in {"h2h", "spreads"}:
        return team_key(raw) or raw
    return raw


def _history_effective_timestamp(row: Mapping[str, Any]) -> datetime | None:
    return _utc(row.get("market_last_update")) or _utc(
        row.get("bookmaker_last_update")
    )


def _select_history_capture(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    return max(rows, key=_history_capture_sort_key)


def _history_capture_sort_key(row: Mapping[str, Any]) -> tuple[bool, float, int]:
    effective = _history_effective_timestamp(row)
    return (
        effective is not None,
        effective.timestamp() if effective is not None else float("-inf"),
        int(row.get("id") or 0),
    )


def _primary_market_movement(
    history_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    captures: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in history_rows:
        market = str(row.get("market_key") or "")
        if market in {"spreads", "totals"}:
            captures[(str(row.get("run_id") or ""), str(row.get("retrieved_at") or ""), market)].append(row)
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (_, retrieved_at, market), rows in captures.items():
        point_books: dict[float, set[str]] = defaultdict(set)
        point_timestamps: dict[float, dict[str, float]] = defaultdict(dict)
        side_probabilities: dict[float, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        home_team = str(rows[0].get("home_team") or "")
        away_team = str(rows[0].get("away_team") or "")
        home_side = str(rows[0].get("home_team_key") or team_key(home_team) or home_team)
        away_side = str(rows[0].get("away_team_key") or team_key(away_team) or away_team)
        by_book: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_book[str(row.get("bookmaker_key") or "")].append(row)
        eligible_books = {
            book
            for book, book_rows in by_book.items()
            if {
                _history_side(row, market)
                for row in book_rows
            }
            <= ({home_side, away_side} if market == "spreads" else {"Over", "Under"})
        }
        selected_rows: dict[tuple[str, float, str], Mapping[str, Any]] = {}
        for row in rows:
            book = str(row.get("bookmaker_key") or "")
            if book not in eligible_books:
                continue
            probability = american_to_probability(row.get("price"))
            if probability is None:
                continue
            raw_point = _finite_number(row.get("point"))
            if raw_point is None:
                continue
            outcome = _history_side(row, market)
            if market == "spreads":
                if outcome == home_side:
                    point = raw_point
                elif outcome == away_side:
                    point = -raw_point
                else:
                    continue
            else:
                point = raw_point
            selection_key = (book, point, outcome)
            existing = selected_rows.get(selection_key)
            if existing is None or _history_capture_sort_key(
                row
            ) > _history_capture_sort_key(existing):
                selected_rows[selection_key] = row

        for (book, point, outcome), row in selected_rows.items():
            probability = american_to_probability(row.get("price"))
            if probability is None:
                continue
            point_books[point].add(book)
            side_probabilities[point][outcome][book] = probability
            effective = _history_effective_timestamp(row)
            if effective is not None:
                point_timestamps[point][book] = max(
                    point_timestamps[point].get(book, float("-inf")),
                    effective.timestamp(),
                )
        if not point_books:
            continue
        max_books = max(len(books) for books in point_books.values())
        candidates = [point for point, books in point_books.items() if len(books) == max_books]
        known_recency = {
            point: float(median(point_timestamps[point].values()))
            for point in candidates
            if point_timestamps[point]
        }
        if known_recency:
            newest = max(known_recency.values())
            candidates = [point for point in candidates if known_recency.get(point) == newest]
        primary = min(candidates)
        by_market[market].append(
            {
                "observed_at": retrieved_at,
                "primary_point": primary,
                "consensus_probabilities": {
                    side: float(median(values.values()))
                    for side, values in side_probabilities[primary].items()
                    if values
                },
            }
        )

    result: list[dict[str, Any]] = []
    for market, observations in sorted(by_market.items()):
        ordered = sorted(observations, key=lambda item: str(item["observed_at"]))
        first = ordered[0]
        latest = ordered[-1]
        sides = sorted(
            set(first["consensus_probabilities"]) | set(latest["consensus_probabilities"])
        )
        has_movement = len(ordered) > 1
        result.append(
            {
                "market": market,
                "first_observed_primary_point": first["primary_point"],
                "latest_observed_primary_point": latest["primary_point"],
                "point_movement": (
                    latest["primary_point"] - first["primary_point"] if has_movement else None
                ),
                "first_observed_at": first["observed_at"],
                "latest_observed_at": latest["observed_at"],
                "observation_count": len(ordered),
                "probability_movement_by_side": {
                    side: {
                        "first_observed_probability": first["consensus_probabilities"].get(side),
                        "latest_observed_probability": latest["consensus_probabilities"].get(side),
                        "probability_movement": (
                            latest["consensus_probabilities"][side]
                            - first["consensus_probabilities"][side]
                            if has_movement
                            and side in first["consensus_probabilities"]
                            and side in latest["consensus_probabilities"]
                            else None
                        ),
                    }
                    for side in sides
                },
            }
        )
    return result
