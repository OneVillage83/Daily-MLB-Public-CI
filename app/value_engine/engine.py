from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from app.daily_slate.contracts import canonical_sha256
from app.matchup_packet.contracts import MatchupPacketGameV1, MatchupPacketV1
from app.odds_weather.contracts import OddsAvailability, thaw_mapping
from app.predictions.contracts import GamePredictionV1, PredictionsV1
from app.predictions.probability import home_spread_projection, total_line_projection
from app.value_engine.contracts import (
    MarketValueV1,
    ValueCalculationState,
    ValueEngineContractError,
    ValueEngineV1,
    ValueGameV1,
    ValueGameWarning,
    ValueIneligibilityReason,
    ValueMarket,
    ValueSide,
)
from app.value_engine.pricing import ValueMathError, calculate_value_math

_FRESHNESS_RANK = {"fresh": 0, "aging": 1, "unknown": 2, "stale": 3}
_MARKET_ORDER = {"h2h": 0, "spreads": 1, "totals": 2}


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueEngineContractError(f"{name} must be a mapping")
    return value


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _float_optional(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identity(game: object) -> tuple[str, str, str, str, str]:
    return (
        str(getattr(game, "edge_event_id")),
        str(getattr(game, "daily_mlb_game_id")),
        str(getattr(game, "source_game_id")),
        str(getattr(game, "away_team_id")),
        str(getattr(game, "home_team_id")),
    )


def _side_sort_key(side_key: object, prediction: GamePredictionV1) -> int:
    side = str(side_key)
    if side == prediction.home_team_id:
        return 0
    if side == prediction.away_team_id:
        return 1
    if side == "Over":
        return 2
    if side == "Under":
        return 3
    return 99


def _selected_best_offer_evidence(
    outcome: Mapping[str, Any],
    *,
    best_price: float,
    best_books: tuple[str, ...],
) -> tuple[tuple[datetime, ...], str, list[dict[str, object]]]:
    timestamps: set[datetime] = set()
    statuses: list[str] = []
    selected: list[dict[str, object]] = []
    for raw_offer in _sequence(outcome.get("offers")):
        if not isinstance(raw_offer, Mapping):
            continue
        bookmaker = str(raw_offer.get("bookmaker_key") or "")
        offer_price = _float_optional(raw_offer.get("normalized_price"))
        if (
            bookmaker not in best_books
            or offer_price is None
            or not math.isclose(offer_price, best_price, abs_tol=1e-12)
            or raw_offer.get("calculation_eligible", True) is not True
        ):
            continue
        status = str(raw_offer.get("freshness_status") or "unknown")
        if status not in _FRESHNESS_RANK:
            status = "unknown"
        timestamp = _parse_utc(raw_offer.get("effective_provider_timestamp"))
        if timestamp is not None:
            timestamps.add(timestamp)
        statuses.append(status)
        selected.append(
            {
                "bookmaker_key": bookmaker,
                "effective_provider_timestamp": (
                    None if timestamp is None else timestamp.isoformat()
                ),
                "freshness_status": status,
                "price": best_price,
            }
        )
    freshness = (
        max(statuses, key=lambda status: _FRESHNESS_RANK[status])
        if statuses
        else "unknown"
    )
    return tuple(sorted(timestamps)), freshness, selected


def _model_probabilities(
    prediction: GamePredictionV1,
    *,
    market_key: str,
    side_key: str,
    line: float | None,
) -> tuple[ValueMarket, ValueSide, float, float, float, float]:
    tail = prediction.distribution.approximation_tail_bound
    if market_key == "h2h":
        if side_key == prediction.home_team_id:
            return (
                ValueMarket.MONEYLINE,
                ValueSide.HOME,
                prediction.home_win_probability,
                prediction.away_win_probability,
                0.0,
                tail,
            )
        if side_key == prediction.away_team_id:
            return (
                ValueMarket.MONEYLINE,
                ValueSide.AWAY,
                prediction.away_win_probability,
                prediction.home_win_probability,
                0.0,
                tail,
            )
    if market_key == "spreads" and line is not None:
        spread_projection = home_spread_projection(prediction.distribution, line)
        if side_key == prediction.home_team_id:
            return (
                ValueMarket.SPREAD,
                ValueSide.HOME,
                spread_projection.home_cover_probability,
                spread_projection.away_cover_probability,
                spread_projection.push_probability,
                spread_projection.approximation_tail_bound,
            )
        if side_key == prediction.away_team_id:
            return (
                ValueMarket.SPREAD,
                ValueSide.AWAY,
                spread_projection.away_cover_probability,
                spread_projection.home_cover_probability,
                spread_projection.push_probability,
                spread_projection.approximation_tail_bound,
            )
    if market_key == "totals" and line is not None:
        total_projection = total_line_projection(prediction.distribution, line)
        if side_key == "Over":
            return (
                ValueMarket.TOTAL,
                ValueSide.OVER,
                total_projection.over_probability,
                total_projection.under_probability,
                total_projection.push_probability,
                total_projection.approximation_tail_bound,
            )
        if side_key == "Under":
            return (
                ValueMarket.TOTAL,
                ValueSide.UNDER,
                total_projection.under_probability,
                total_projection.over_probability,
                total_projection.push_probability,
                total_projection.approximation_tail_bound,
            )
    raise ValueEngineContractError(
        f"unsupported normalized market side {market_key}/{side_key}/{line}"
    )


def _ineligibility_reasons(
    prediction: GamePredictionV1,
    *,
    complete_two_way_market: bool,
    no_vig_probability: float | None,
    freshness_status: str,
    calculation_state: ValueCalculationState,
) -> tuple[ValueIneligibilityReason, ...]:
    reasons: list[ValueIneligibilityReason] = []
    if not prediction.recommendation_eligible:
        reasons.append(ValueIneligibilityReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE)
    if prediction.quality_disposition.value == "insufficient":
        reasons.append(ValueIneligibilityReason.DATA_QUALITY_INSUFFICIENT)
    if not complete_two_way_market:
        reasons.append(ValueIneligibilityReason.INCOMPLETE_TWO_WAY_MARKET)
    if no_vig_probability is None:
        reasons.append(ValueIneligibilityReason.MISSING_NO_VIG_PROBABILITY)
    if freshness_status == "stale":
        reasons.append(ValueIneligibilityReason.MARKET_STALE)
    elif freshness_status == "unknown":
        reasons.append(ValueIneligibilityReason.MARKET_FRESHNESS_UNKNOWN)
    if calculation_state is not ValueCalculationState.COMPLETE:
        reasons.append(ValueIneligibilityReason.CALCULATION_INCOMPLETE)
    return tuple(sorted(set(reasons), key=lambda reason: reason.value))


def _market_value(
    prediction: GamePredictionV1,
    *,
    summary_checksum: str,
    market_retrieved_at: datetime,
    market_key: str,
    line_key: str,
    line: Mapping[str, Any],
    side_key: str,
    outcome: Mapping[str, Any],
) -> MarketValueV1 | None:
    if market_key not in _MARKET_ORDER:
        return None
    if market_key != "h2h" and line.get("valid_line") is not True:
        return None
    market_line = None if market_key == "h2h" else _float_optional(line.get("line"))
    if market_key != "h2h" and market_line is None:
        return None
    price = _float_optional(outcome.get("best_price"))
    raw_implied = _float_optional(outcome.get("best_price_implied_probability"))
    if price is None or raw_implied is None or abs(price) < 100.0:
        return None
    books = tuple(
        sorted(
            {
                str(book)
                for book in _sequence(outcome.get("best_price_books"))
                if str(book)
            }
        )
    )
    if not books:
        return None
    timestamps, freshness, selected_offers = _selected_best_offer_evidence(
        outcome,
        best_price=price,
        best_books=books,
    )
    market, side, win, loss, push, tail = _model_probabilities(
        prediction,
        market_key=market_key,
        side_key=side_key,
        line=market_line,
    )
    no_vig = _float_optional(outcome.get("no_vig_probability"))
    try:
        value_math = calculate_value_math(
            win_probability=win,
            loss_probability=loss,
            push_probability=push,
            price=price,
            retained_raw_implied_probability=raw_implied,
            no_vig_probability=no_vig,
        )
    except ValueMathError as exc:
        raise ValueEngineContractError(
            f"invalid value math for {market_key}/{line_key}/{side_key}"
        ) from exc
    complete_two_way = line.get("complete_two_way_market") is True
    state = (
        ValueCalculationState.COMPLETE
        if no_vig is not None
        else ValueCalculationState.PARTIAL
    )
    reasons = _ineligibility_reasons(
        prediction,
        complete_two_way_market=complete_two_way,
        no_vig_probability=no_vig,
        freshness_status=freshness,
        calculation_state=state,
    )
    evidence_checksum = canonical_sha256(
        {
            "line": {
                "complete_two_way_market": complete_two_way,
                "line": market_line,
                "line_key": line_key,
                "market_hold": line.get("median_market_hold"),
                "market_key": market_key,
            },
            "odds_summary_checksum": summary_checksum,
            "outcome": {
                "best_price": price,
                "best_price_books": list(books),
                "best_price_implied_probability": raw_implied,
                "bookmaker_count": outcome.get("bookmaker_count"),
                "consensus_confidence": outcome.get("consensus_confidence"),
                "no_vig_probability": no_vig,
                "selected_best_offers": selected_offers,
                "side_key": side_key,
            },
        }
    )
    return MarketValueV1(
        market=market,
        line_key=line_key,
        side=side,
        market_line=market_line,
        american_price=value_math.american_price,
        best_price_books=books,
        bookmaker_count=_nonnegative_int(outcome.get("bookmaker_count")),
        consensus_confidence=str(
            outcome.get("consensus_confidence") or "insufficient"
        ),
        market_retrieved_at=market_retrieved_at,
        effective_provider_timestamps=timestamps,
        freshness_status=freshness,
        complete_two_way_market=complete_two_way,
        median_market_hold=_float_optional(line.get("median_market_hold")),
        market_evidence_checksum=evidence_checksum,
        model_win_probability=value_math.model_win_probability,
        model_loss_probability=value_math.model_loss_probability,
        model_push_probability=value_math.model_push_probability,
        conditional_model_probability=value_math.conditional_model_probability,
        raw_implied_probability=value_math.raw_implied_probability,
        no_vig_probability=value_math.no_vig_probability,
        raw_probability_edge=value_math.raw_probability_edge,
        no_vig_probability_edge=value_math.no_vig_probability_edge,
        net_profit_per_unit=value_math.net_profit_per_unit,
        expected_value_per_unit=value_math.expected_value_per_unit,
        expected_roi_percent=value_math.expected_roi_percent,
        fair_decimal_odds=value_math.fair_decimal_odds,
        fair_american_odds=value_math.fair_american_odds,
        approximation_tail_bound=tail,
        calculation_state=state,
        recommendation_gate_input_eligible=not reasons,
        ineligibility_reasons=reasons,
    )


def evaluate_game_value(
    prediction: GamePredictionV1,
    packet_game: MatchupPacketGameV1,
) -> ValueGameV1:
    if _identity(prediction) != _identity(packet_game):
        raise ValueEngineContractError("prediction and packet game identity mismatch")
    odds = packet_game.odds_weather.odds
    summary_checksum = odds.summary_checksum
    if prediction.market_reference_checksum != summary_checksum:
        raise ValueEngineContractError("prediction market-reference checksum mismatch")
    values: list[MarketValueV1] = []
    warnings: list[ValueGameWarning] = []
    if odds.availability is OddsAvailability.UNAVAILABLE:
        warnings.append(ValueGameWarning.ODDS_UNAVAILABLE)
    else:
        if odds.summary is None or odds.retrieved_at is None or summary_checksum is None:
            raise ValueEngineContractError("available odds lack canonical summary lineage")
        summary = thaw_mapping(odds.summary)
        markets = _mapping(summary.get("markets"), "odds-summary markets")
        market_keys = sorted(
            markets,
            key=lambda key: (_MARKET_ORDER.get(str(key), 99), str(key)),
        )
        for raw_market_key in market_keys:
            market_key = str(raw_market_key)
            if market_key not in _MARKET_ORDER:
                continue
            market = _mapping(markets[raw_market_key], f"market {market_key}")
            lines = _mapping(market.get("lines"), f"market {market_key} lines")
            for raw_line_key in sorted(lines):
                line_key = str(raw_line_key)
                line = _mapping(lines[raw_line_key], f"line {market_key}/{line_key}")
                outcomes = _mapping(
                    line.get("outcomes"),
                    f"outcomes {market_key}/{line_key}",
                )
                for raw_side_key in sorted(
                    outcomes,
                    key=lambda key: _side_sort_key(key, prediction),
                ):
                    side_key = str(raw_side_key)
                    outcome = _mapping(
                        outcomes[raw_side_key],
                        f"outcome {market_key}/{line_key}/{side_key}",
                    )
                    value = _market_value(
                        prediction,
                        summary_checksum=summary_checksum,
                        market_retrieved_at=odds.retrieved_at,
                        market_key=market_key,
                        line_key=line_key,
                        line=line,
                        side_key=side_key,
                        outcome=outcome,
                    )
                    if value is not None:
                        values.append(value)
        if not values:
            warnings.append(ValueGameWarning.NO_EVALUABLE_MARKETS)
    return ValueGameV1(
        edge_event_id=prediction.edge_event_id,
        daily_mlb_game_id=prediction.daily_mlb_game_id,
        source_game_id=prediction.source_game_id,
        away_team_id=prediction.away_team_id,
        home_team_id=prediction.home_team_id,
        upstream_prediction_game_checksum=prediction.checksum,
        upstream_matchup_packet_game_checksum=packet_game.checksum,
        model_manifest_checksum=prediction.model_manifest_checksum,
        model_id=prediction.model_id,
        model_version=prediction.model_version,
        deployment_status=prediction.deployment_status,
        calibration_status=prediction.calibration_status,
        model_recommendation_eligible=prediction.recommendation_eligible,
        quality_disposition=prediction.quality_disposition,
        quality_issue_codes=prediction.quality_issue_codes,
        market_reference_checksum=prediction.market_reference_checksum,
        odds_summary_checksum=summary_checksum,
        values=tuple(values),
        warnings=tuple(warnings),
    )


def evaluate_value_engine(
    *,
    predictions: PredictionsV1,
    matchup_packet: MatchupPacketV1,
    observed_at: datetime | None = None,
) -> ValueEngineV1:
    if predictions.requested_date != matchup_packet.requested_date:
        raise ValueEngineContractError("requested_date mismatch")
    if predictions.as_of_time != matchup_packet.as_of_time:
        raise ValueEngineContractError("as_of_time mismatch")
    if len(predictions.games) != len(matchup_packet.games):
        raise ValueEngineContractError("upstream game-count mismatch")
    latest_upstream = max(predictions.observed_at, matchup_packet.observed_at)
    selected_observed = latest_upstream if observed_at is None else observed_at
    if selected_observed.tzinfo is None or selected_observed.utcoffset() is None:
        raise ValueEngineContractError("observed_at must be timezone-aware")
    selected_observed = selected_observed.astimezone(timezone.utc)
    if selected_observed < latest_upstream:
        raise ValueEngineContractError("observed_at cannot precede upstream evidence")
    games = tuple(
        evaluate_game_value(prediction, packet_game)
        for prediction, packet_game in zip(
            predictions.games,
            matchup_packet.games,
            strict=True,
        )
    )
    return ValueEngineV1(
        requested_date=predictions.requested_date,
        as_of_time=predictions.as_of_time,
        observed_at=selected_observed,
        upstream_predictions_checksum=predictions.checksum,
        upstream_matchup_packet_checksum=matchup_packet.checksum,
        model_manifest_checksum=predictions.model_manifest.checksum,
        games=games,
    )
