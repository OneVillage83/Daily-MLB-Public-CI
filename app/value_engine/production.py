from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from statistics import median

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.predictions.production import MoneylinePredictionV1, PredictionsV1
from app.redaction import redact_value
from app.value_engine.pricing import (
    american_net_profit_per_unit,
    american_to_implied_probability,
)

VALUE_ENGINE_PRODUCTION_CONTRACT = "DSE_MLB_ML_VALUE_ENGINE_V1"
VALUE_ENGINE_PHASE_INPUT_CONTRACT = "DSE_MLB_ML_VALUE_ENGINE_PHASE_INPUT_V1"
VALUE_ENGINE_POLICY_VERSION = "DSE_MLB_ML_VALUE_POLICY_V1"


class ValueEngineProductionError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueEngineProductionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha(value: str, name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueEngineProductionError(f"{name} must be lowercase SHA-256")
    return value


def _canonical_object(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    try:
        result = json.loads(canonical_json_bytes(dict(value)))
    except (TypeError, ValueError) as exc:
        raise ValueEngineProductionError(f"{name} must be finite canonical JSON") from exc
    if not isinstance(result, dict):
        raise ValueEngineProductionError(f"{name} must be an object")
    return result


@dataclass(frozen=True, slots=True)
class ValuePolicyV1:
    maximum_odds_age_seconds: int = 120
    maximum_future_skew_seconds: int = 30
    maximum_pair_timestamp_skew_seconds: int = 5
    preferred_bookmaker_count: int = 4
    policy_version: str = VALUE_ENGINE_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "maximum_odds_age_seconds",
            "maximum_future_skew_seconds",
            "maximum_pair_timestamp_skew_seconds",
            "preferred_bookmaker_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueEngineProductionError(f"{name} must be a nonnegative integer")
        if self.preferred_bookmaker_count < 1:
            raise ValueEngineProductionError("preferred_bookmaker_count must be positive")

    def identity_dict(self) -> dict[str, object]:
        return {
            "maximum_future_skew_seconds": self.maximum_future_skew_seconds,
            "maximum_odds_age_seconds": self.maximum_odds_age_seconds,
            "maximum_pair_timestamp_skew_seconds": self.maximum_pair_timestamp_skew_seconds,
            "policy_version": self.policy_version,
            "preferred_bookmaker_count": self.preferred_bookmaker_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class EligibleBookPairV1:
    bookmaker_key: str
    home_price: float
    away_price: float
    home_implied_probability: float
    away_implied_probability: float
    home_no_vig_probability: float
    away_no_vig_probability: float
    effective_timestamp: datetime
    retrieval_timestamp: datetime
    evidence_checksum: str

    def __post_init__(self) -> None:
        if not self.bookmaker_key or self.bookmaker_key != self.bookmaker_key.strip():
            raise ValueEngineProductionError("bookmaker_key must be nonblank")
        for name in ("home_price", "away_price"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or abs(value) < 100:
                raise ValueEngineProductionError("book price is invalid")
            object.__setattr__(self, name, value)
        for name in (
            "home_implied_probability",
            "away_implied_probability",
            "home_no_vig_probability",
            "away_no_vig_probability",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueEngineProductionError(f"{name} is invalid")
            object.__setattr__(self, name, value)
        if not math.isclose(self.home_no_vig_probability + self.away_no_vig_probability, 1.0, abs_tol=1e-12):
            raise ValueEngineProductionError("book-level no-vig probabilities must be complementary")
        object.__setattr__(self, "effective_timestamp", _utc(self.effective_timestamp, "effective_timestamp"))
        object.__setattr__(self, "retrieval_timestamp", _utc(self.retrieval_timestamp, "retrieval_timestamp"))
        object.__setattr__(self, "evidence_checksum", _sha(self.evidence_checksum, "evidence_checksum"))

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_implied_probability": self.away_implied_probability,
            "away_no_vig_probability": self.away_no_vig_probability,
            "away_price": self.away_price,
            "bookmaker_key": self.bookmaker_key,
            "effective_timestamp": self.effective_timestamp.isoformat(),
            "evidence_checksum": self.evidence_checksum,
            "home_implied_probability": self.home_implied_probability,
            "home_no_vig_probability": self.home_no_vig_probability,
            "home_price": self.home_price,
            "retrieval_timestamp": self.retrieval_timestamp.isoformat(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class MoneylineOutcomeValueV1:
    side: str
    outcome_team_id: str
    prediction_probability: float
    probability_lower: float
    probability_upper: float
    availability: str
    eligible_pairs: tuple[EligibleBookPairV1, ...]
    exclusion_counts: Mapping[str, int]
    best_price: float | None
    best_price_bookmakers: tuple[str, ...]
    consensus_no_vig_probability: float | None
    break_even_probability: float | None
    edge: float | None
    expected_value_per_unit: float | None
    lower_bound_clearance: float | None
    freshness_state: str
    market_context_checksum: str
    prediction_checksum: str

    def __post_init__(self) -> None:
        if self.side not in {"home", "away"}:
            raise ValueEngineProductionError("moneyline side is invalid")
        if self.availability not in {"available", "unavailable"}:
            raise ValueEngineProductionError("outcome availability is invalid")
        for name in ("prediction_probability", "probability_lower", "probability_upper"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueEngineProductionError(f"{name} is invalid")
            object.__setattr__(self, name, value)
        if not self.probability_lower <= self.prediction_probability <= self.probability_upper:
            raise ValueEngineProductionError("prediction probability bounds are invalid")
        pairs = tuple(sorted(self.eligible_pairs, key=lambda pair: pair.bookmaker_key))
        if len({pair.bookmaker_key for pair in pairs}) != len(pairs):
            raise ValueEngineProductionError("eligible bookmaker pairs must be unique")
        object.__setattr__(self, "eligible_pairs", pairs)
        counts = _canonical_object(self.exclusion_counts, "exclusion_counts")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueEngineProductionError("exclusion counts must be nonnegative integers")
        object.__setattr__(self, "exclusion_counts", counts)
        available_values = (
            self.best_price,
            self.consensus_no_vig_probability,
            self.break_even_probability,
            self.edge,
            self.expected_value_per_unit,
            self.lower_bound_clearance,
        )
        if self.availability == "available":
            if not pairs or any(value is None for value in available_values):
                raise ValueEngineProductionError("available value outcome is incomplete")
        elif pairs or any(value is not None for value in available_values):
            raise ValueEngineProductionError("unavailable value outcome cannot contain selected value")
        for name in ("market_context_checksum", "prediction_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))

    def identity_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability,
            "best_price": self.best_price,
            "best_price_bookmakers": list(self.best_price_bookmakers),
            "bookmaker_count": len(self.eligible_pairs),
            "break_even_probability": self.break_even_probability,
            "consensus_no_vig_probability": self.consensus_no_vig_probability,
            "edge": self.edge,
            "eligible_pairs": [pair.as_dict() for pair in self.eligible_pairs],
            "excluded_offer_counts": dict(self.exclusion_counts),
            "expected_value_per_unit": self.expected_value_per_unit,
            "freshness_state": self.freshness_state,
            "lower_bound_clearance": self.lower_bound_clearance,
            "market_context_checksum": self.market_context_checksum,
            "outcome_team_id": self.outcome_team_id,
            "prediction_checksum": self.prediction_checksum,
            "prediction_probability": self.prediction_probability,
            "probability_lower": self.probability_lower,
            "probability_upper": self.probability_upper,
            "side": self.side,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class MoneylineGameValueV1:
    ordinal: int
    source_game_id: str
    market_context_checksum: str
    prediction_checksum: str
    outcomes: tuple[MoneylineOutcomeValueV1, MoneylineOutcomeValueV1]

    def __post_init__(self) -> None:
        if tuple(value.side for value in self.outcomes) != ("home", "away"):
            raise ValueEngineProductionError("game value must retain home and away outcomes")
        if any(
            value.market_context_checksum != self.market_context_checksum
            or value.prediction_checksum != self.prediction_checksum
            for value in self.outcomes
        ):
            raise ValueEngineProductionError("game value lineage mismatch")

    def identity_dict(self) -> dict[str, object]:
        return {
            "market_context_checksum": self.market_context_checksum,
            "ordinal": self.ordinal,
            "outcomes": [value.as_dict() for value in self.outcomes],
            "prediction_checksum": self.prediction_checksum,
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ProductionValueEngineV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    evaluated_at: datetime
    policy: ValuePolicyV1
    upstream_predictions_snapshot_id: str
    upstream_predictions_checksum: str
    upstream_model_feature_set_snapshot_id: str
    upstream_model_feature_set_checksum: str
    upstream_data_quality_snapshot_id: str
    upstream_data_quality_checksum: str
    market_inventory_checksum: str
    games: tuple[MoneylineGameValueV1, ...]
    warnings: tuple[Mapping[str, object], ...] = ()
    contract_version: str = VALUE_ENGINE_PRODUCTION_CONTRACT
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        for name in (
            "upstream_predictions_checksum",
            "upstream_model_feature_set_checksum",
            "upstream_data_quality_checksum",
            "market_inventory_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        games = tuple(self.games)
        if [game.ordinal for game in games] != list(range(1, len(games) + 1)):
            raise ValueEngineProductionError("value game ordinals must be contiguous")
        object.__setattr__(self, "games", games)
        warnings = tuple(_canonical_object(value, "warning") for value in self.warnings)
        object.__setattr__(self, "warnings", warnings)
        configured = tuple(str(value) for value in secret_values if str(value))
        if (
            redact_value(self.identity_dict(), configured, preserve_field_names=("bookmaker_key", "market_key"))
            != self.identity_dict()
        ):
            raise ValueEngineProductionError("Value Engine snapshot contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "games": [game.as_dict() for game in self.games],
            "market_inventory_checksum": self.market_inventory_checksum,
            "policy": self.policy.as_dict(),
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_data_quality_checksum": self.upstream_data_quality_checksum,
            "upstream_data_quality_snapshot_id": self.upstream_data_quality_snapshot_id,
            "upstream_model_feature_set_checksum": self.upstream_model_feature_set_checksum,
            "upstream_model_feature_set_snapshot_id": self.upstream_model_feature_set_snapshot_id,
            "upstream_predictions_checksum": self.upstream_predictions_checksum,
            "upstream_predictions_snapshot_id": self.upstream_predictions_snapshot_id,
            "warnings": [dict(value) for value in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None or parsed.utcoffset() is None else parsed.astimezone(timezone.utc)


def _numeric(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("market price must be numeric")
    return float(value)


def _offers(outcome: Mapping[str, object]) -> dict[str, tuple[Mapping[str, object], ...]]:
    raw = outcome.get("selected_offers_by_book")
    if isinstance(raw, Mapping):
        return {str(key): (value,) for key, value in raw.items() if isinstance(value, Mapping)}
    retained = outcome.get("offers")
    if not isinstance(retained, (list, tuple)):
        return {}
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for value in retained:
        if not isinstance(value, Mapping):
            continue
        bookmaker = value.get("bookmaker_key")
        if not isinstance(bookmaker, str) or not bookmaker.strip():
            continue
        grouped.setdefault(bookmaker, []).append(value)

    return {
        bookmaker: tuple(
            sorted(values, key=lambda value: canonical_sha256(dict(value)))
        )
        for bookmaker, values in grouped.items()
    }


def _provider_order(value: Mapping[str, object]) -> int:
    raw = value.get("provider_order")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _compatible_pair(
    bookmaker_key: str,
    home_offer: Mapping[str, object],
    away_offer: Mapping[str, object],
    *,
    evaluated_at: datetime,
    policy: ValuePolicyV1,
) -> tuple[EligibleBookPairV1 | None, str, tuple[object, ...] | None]:
    if home_offer.get("calculation_eligible") not in {None, True} or away_offer.get(
        "calculation_eligible"
    ) not in {None, True}:
        return None, "malformed", None
    try:
        home_price = _numeric(home_offer["price"])
        away_price = _numeric(away_offer["price"])
        home_implied = american_to_implied_probability(home_price)
        away_implied = american_to_implied_probability(away_price)
    except (KeyError, TypeError, ValueError):
        return None, "malformed", None
    home_effective = _timestamp(home_offer.get("effective_provider_timestamp"))
    away_effective = _timestamp(away_offer.get("effective_provider_timestamp"))
    home_retrieved = _timestamp(home_offer.get("provider_retrieved_at")) or _timestamp(
        home_offer.get("retrieved_at")
    )
    away_retrieved = _timestamp(away_offer.get("provider_retrieved_at")) or _timestamp(
        away_offer.get("retrieved_at")
    )
    if None in {home_effective, away_effective, home_retrieved, away_retrieved}:
        return None, "malformed", None
    assert home_effective is not None and away_effective is not None
    assert home_retrieved is not None and away_retrieved is not None
    if (
        abs((home_effective - away_effective).total_seconds())
        > policy.maximum_pair_timestamp_skew_seconds
        or abs((home_retrieved - away_retrieved).total_seconds())
        > policy.maximum_pair_timestamp_skew_seconds
    ):
        return None, "timestamp_skew", None
    effective = max(home_effective, away_effective)
    retrieval = max(home_retrieved, away_retrieved)
    age = (evaluated_at - effective).total_seconds()
    if age < -policy.maximum_future_skew_seconds:
        return None, "future", None
    if age > policy.maximum_odds_age_seconds:
        return None, "stale", None
    overround = home_implied + away_implied
    home_checksum = canonical_sha256(dict(home_offer))
    away_checksum = canonical_sha256(dict(away_offer))
    evidence = canonical_sha256(
        {"away": dict(away_offer), "bookmaker_key": bookmaker_key, "home": dict(home_offer)}
    )
    pair = EligibleBookPairV1(
        bookmaker_key,
        home_price,
        away_price,
        home_implied,
        away_implied,
        home_implied / overround,
        away_implied / overround,
        effective,
        retrieval,
        evidence,
    )
    selection_key: tuple[object, ...] = (
        min(home_effective, away_effective),
        min(home_retrieved, away_retrieved),
        max(home_effective, away_effective),
        max(home_retrieved, away_retrieved),
        min(_provider_order(home_offer), _provider_order(away_offer)),
        home_checksum,
        away_checksum,
    )
    return pair, "", selection_key


def calculate_game_value(
    prediction: MoneylinePredictionV1,
    market_context: Mapping[str, object],
    *,
    market_context_checksum: str,
    evaluated_at: datetime,
    policy: ValuePolicyV1,
) -> MoneylineGameValueV1:
    evaluated = _utc(evaluated_at, "evaluated_at")
    markets = market_context.get("markets") if isinstance(market_context, Mapping) else None
    h2h = markets.get("h2h") if isinstance(markets, Mapping) else None
    lines = h2h.get("lines") if isinstance(h2h, Mapping) else None
    line = lines.get("moneyline") if isinstance(lines, Mapping) else None
    outcomes = line.get("outcomes") if isinstance(line, Mapping) else None
    home = outcomes.get(prediction.home_team_id) if isinstance(outcomes, Mapping) else None
    away = outcomes.get(prediction.away_team_id) if isinstance(outcomes, Mapping) else None
    pairs: list[EligibleBookPairV1] = []
    excluded: dict[str, int] = {"incomplete_pair": 0, "future": 0, "stale": 0, "timestamp_skew": 0, "malformed": 0}
    if isinstance(home, Mapping) and isinstance(away, Mapping):
        home_offers, away_offers = _offers(home), _offers(away)
        for book in sorted(set(home_offers) | set(away_offers)):
            if book not in home_offers or book not in away_offers:
                excluded["incomplete_pair"] += 1
                continue
            compatible: list[tuple[tuple[object, ...], EligibleBookPairV1]] = []
            rejected_reasons: set[str] = set()
            for home_offer in home_offers[book]:
                for away_offer in away_offers[book]:
                    pair, reason, selection_key = _compatible_pair(
                        book,
                        home_offer,
                        away_offer,
                        evaluated_at=evaluated,
                        policy=policy,
                    )
                    if pair is None or selection_key is None:
                        rejected_reasons.add(reason)
                    else:
                        compatible.append((selection_key, pair))
            if compatible:
                pairs.append(max(compatible, key=lambda item: item[0])[1])
                continue
            for reason in ("malformed", "future", "stale", "timestamp_skew"):
                if reason in rejected_reasons:
                    excluded[reason] += 1
                    break
    pairs_tuple = tuple(pairs)

    def outcome_value(side: str) -> MoneylineOutcomeValueV1:
        is_home = side == "home"
        probability = prediction.home_probability if is_home else prediction.away_probability
        lower = prediction.home_lower if is_home else prediction.away_lower
        upper = prediction.home_upper if is_home else prediction.away_upper
        if not pairs_tuple:
            return MoneylineOutcomeValueV1(
                side,
                prediction.home_team_id if is_home else prediction.away_team_id,
                probability,
                lower,
                upper,
                "unavailable",
                (),
                excluded,
                None,
                (),
                None,
                None,
                None,
                None,
                None,
                "unavailable",
                market_context_checksum,
                prediction.checksum,
            )
        prices = [(pair.home_price if is_home else pair.away_price, pair.bookmaker_key) for pair in pairs_tuple]
        best_price = max(price for price, _ in prices)
        best_books = tuple(sorted(book for price, book in prices if price == best_price))
        no_vig_values = [
            pair.home_no_vig_probability if is_home else pair.away_no_vig_probability for pair in pairs_tuple
        ]
        baseline = float(median(no_vig_values))
        break_even = american_to_implied_probability(best_price)
        ev = probability * american_net_profit_per_unit(best_price) - (1.0 - probability)
        return MoneylineOutcomeValueV1(
            side,
            prediction.home_team_id if is_home else prediction.away_team_id,
            probability,
            lower,
            upper,
            "available",
            pairs_tuple,
            excluded,
            best_price,
            best_books,
            baseline,
            break_even,
            probability - baseline,
            ev,
            lower - baseline,
            "fresh",
            market_context_checksum,
            prediction.checksum,
        )

    return MoneylineGameValueV1(
        prediction.ordinal,
        prediction.source_game_id,
        market_context_checksum,
        prediction.checksum,
        (outcome_value("home"), outcome_value("away")),
    )


def build_value_engine(
    predictions: PredictionsV1,
    market_context_by_game: Mapping[str, Mapping[str, object]],
    market_checksum_by_game: Mapping[str, str],
    *,
    evaluated_at: datetime,
    policy: ValuePolicyV1,
    model_feature_set_snapshot_id: str,
    model_feature_set_checksum: str,
    data_quality_snapshot_id: str,
    data_quality_checksum: str,
    secret_values: Iterable[str] = (),
) -> ProductionValueEngineV1:
    games = tuple(
        calculate_game_value(
            prediction,
            market_context_by_game.get(prediction.source_game_id, {}),
            market_context_checksum=market_checksum_by_game[prediction.source_game_id],
            evaluated_at=evaluated_at,
            policy=policy,
        )
        for prediction in predictions.games
    )
    inventory_checksum = canonical_sha256(
        [
            {
                "market_context_checksum": market_checksum_by_game[game.source_game_id],
                "source_game_id": game.source_game_id,
            }
            for game in predictions.games
        ]
    )
    return ProductionValueEngineV1(
        run_id=predictions.run_id,
        requested_date=predictions.requested_date,
        as_of_time=predictions.as_of_time,
        evaluated_at=evaluated_at,
        policy=policy,
        upstream_predictions_snapshot_id=f"predictions:{predictions.checksum}",
        upstream_predictions_checksum=predictions.checksum,
        upstream_model_feature_set_snapshot_id=model_feature_set_snapshot_id,
        upstream_model_feature_set_checksum=model_feature_set_checksum,
        upstream_data_quality_snapshot_id=data_quality_snapshot_id,
        upstream_data_quality_checksum=data_quality_checksum,
        market_inventory_checksum=inventory_checksum,
        games=games,
        secret_values=secret_values,
    )
