from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.data_quality.contracts import DataQualityDisposition
from app.data_quality.engine import assess_data_quality
from app.matchup_packet.assembly import assemble_matchup_packet
from app.model_feature_set.builder import build_model_feature_set
from app.odds_weather.contracts import OddsAvailability, OddsSnapshotV1
from app.predictions.runtime import predict_model_feature_set
from app.processors.odds_processor import process_game
from app.value_engine.contracts import (
    ValueCalculationState,
    ValueEngineContractError,
    ValueGameWarning,
    ValueIneligibilityReason,
    ValueMarket,
    ValueSide,
)
from app.value_engine.engine import evaluate_value_engine
from app.value_engine.pricing import (
    american_net_profit_per_unit,
    american_to_implied_probability,
    calculate_value_math,
    fair_american_odds,
)
from tests.test_matchup_packet import _one_game_chain, _zero_game_chain

RETRIEVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
RAW_CHECKSUM = "c" * 64


def _market_event(*, home_h2h: int = -115) -> dict[str, object]:
    timestamp = RETRIEVED.isoformat()
    return {
        "id": "odds-900001",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-27T23:10:00+00:00",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "last_update": timestamp,
        "bookmakers": [
            {
                "key": "book_a",
                "title": "Book A",
                "last_update": timestamp,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": -120},
                            {"name": "San Francisco Giants", "price": 110},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": 130, "point": -1.0},
                            {"name": "San Francisco Giants", "price": -150, "point": 1.0},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Over", "price": -105, "point": 9.0},
                            {"name": "Under", "price": -115, "point": 9.0},
                        ],
                    },
                ],
            },
            {
                "key": "book_b",
                "title": "Book B",
                "last_update": timestamp,
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": home_h2h},
                            {"name": "San Francisco Giants", "price": 105},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Los Angeles Dodgers", "price": 135, "point": -1.0},
                            {"name": "San Francisco Giants", "price": -155, "point": 1.0},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": timestamp,
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 9.0},
                            {"name": "Under", "price": -110, "point": 9.0},
                        ],
                    },
                ],
            },
        ],
    }


def _packet_predictions(*, home_h2h: int = -115):
    slate, state, intelligence, odds_weather, _ = _one_game_chain()
    processed = process_game(
        _market_event(home_h2h=home_h2h),
        run_id="value-fixture",
        retrieved_at=RETRIEVED.isoformat(),
    )
    odds = OddsSnapshotV1(
        availability=OddsAvailability.AVAILABLE,
        provider_event_id="odds-900001",
        retrieved_at=RETRIEVED,
        raw_capture_checksum=RAW_CHECKSUM,
        event_match_offset_minutes=0.0,
        summary=processed.summary,
        normalized_market_count=processed.normalized_market_count,
        raw_snapshot_count=processed.raw_snapshot_count,
        freshness_counts=processed.freshness_counts,
    )
    odds_game = replace(odds_weather.games[0], odds=odds)
    odds_weather = replace(
        odds_weather,
        source_raw_capture_checksums=(RAW_CHECKSUM,),
        games=(odds_game,),
    )
    quality = assess_data_quality(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        observed_at=odds_weather.observed_at,
    ).snapshot
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        data_quality=quality,
    )
    feature_set = build_model_feature_set(packet)
    predictions = predict_model_feature_set(feature_set)
    return packet, predictions


def test_american_price_math_and_fair_odds() -> None:
    assert american_to_implied_probability(150) == pytest.approx(0.4)
    assert american_to_implied_probability(-150) == pytest.approx(0.6)
    assert american_net_profit_per_unit(150) == pytest.approx(1.5)
    assert american_net_profit_per_unit(-150) == pytest.approx(2 / 3)
    assert fair_american_odds(0.6) == pytest.approx(-150)
    assert fair_american_odds(0.4) == pytest.approx(150)


def test_push_aware_expected_value_uses_win_and_loss_only() -> None:
    result = calculate_value_math(
        win_probability=0.50,
        loss_probability=0.40,
        push_probability=0.10,
        price=110,
        retained_raw_implied_probability=100 / 210,
        no_vig_probability=0.48,
    )
    assert result.conditional_model_probability == pytest.approx(5 / 9)
    assert result.expected_value_per_unit == pytest.approx(0.15)
    assert result.expected_roi_percent == pytest.approx(15.0)
    assert result.raw_probability_edge == pytest.approx((5 / 9) - (100 / 210))


def test_value_engine_evaluates_all_sides_and_lines() -> None:
    packet, predictions = _packet_predictions()
    result = evaluate_value_engine(
        predictions=predictions,
        matchup_packet=packet,
    )
    assert len(result.games) == 1
    game = result.games[0]
    assert game.upstream_prediction_game_checksum == predictions.games[0].checksum
    assert game.upstream_matchup_packet_game_checksum == packet.games[0].checksum
    assert game.odds_summary_checksum == packet.games[0].odds_weather.odds.summary_checksum
    assert len(game.values) == 6
    assert {item.market for item in game.values} == {
        ValueMarket.MONEYLINE,
        ValueMarket.SPREAD,
        ValueMarket.TOTAL,
    }
    assert {item.side for item in game.values} == {
        ValueSide.HOME,
        ValueSide.AWAY,
        ValueSide.OVER,
        ValueSide.UNDER,
    }
    assert all(item.calculation_state is ValueCalculationState.COMPLETE for item in game.values)


def test_moneyline_uses_prediction_probability_and_exact_best_price() -> None:
    packet, predictions = _packet_predictions()
    result = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    home = next(
        item
        for item in result.games[0].values
        if item.market is ValueMarket.MONEYLINE and item.side is ValueSide.HOME
    )
    prediction = predictions.games[0]
    assert home.model_win_probability == prediction.home_win_probability
    assert home.model_loss_probability == prediction.away_win_probability
    assert home.model_push_probability == 0.0
    assert home.american_price == -115.0
    assert home.best_price_books == ("book_b",)
    assert home.raw_implied_probability == pytest.approx(115 / 215)
    assert home.expected_value_per_unit == pytest.approx(
        prediction.home_win_probability * (100 / 115)
        - prediction.away_win_probability
    )


def test_integer_spread_and_total_preserve_push_probability() -> None:
    packet, predictions = _packet_predictions()
    result = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    spread_home = next(
        item
        for item in result.games[0].values
        if item.market is ValueMarket.SPREAD and item.side is ValueSide.HOME
    )
    total_over = next(
        item
        for item in result.games[0].values
        if item.market is ValueMarket.TOTAL and item.side is ValueSide.OVER
    )
    assert spread_home.market_line == -1.0
    assert spread_home.model_push_probability > 0.0
    assert total_over.market_line == 9.0
    assert total_over.model_push_probability > 0.0
    assert spread_home.conditional_model_probability == pytest.approx(
        spread_home.model_win_probability
        / (spread_home.model_win_probability + spread_home.model_loss_probability)
    )


def test_positive_value_does_not_override_reference_model_ineligibility() -> None:
    packet, predictions = _packet_predictions(home_h2h=250)
    result = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    home = next(
        item
        for item in result.games[0].values
        if item.market is ValueMarket.MONEYLINE and item.side is ValueSide.HOME
    )
    assert home.expected_value_per_unit > 0.0
    assert home.recommendation_gate_input_eligible is False
    assert (
        ValueIneligibilityReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE
        in home.ineligibility_reasons
    )


def test_market_price_changes_value_not_model_output() -> None:
    first_packet, first_predictions = _packet_predictions(home_h2h=-115)
    second_packet, second_predictions = _packet_predictions(home_h2h=130)
    first_prediction = first_predictions.games[0]
    second_prediction = second_predictions.games[0]
    assert first_prediction.model_input_checksum == second_prediction.model_input_checksum
    assert first_prediction.home_win_probability == second_prediction.home_win_probability
    first_value = evaluate_value_engine(
        predictions=first_predictions,
        matchup_packet=first_packet,
    )
    second_value = evaluate_value_engine(
        predictions=second_predictions,
        matchup_packet=second_packet,
    )
    first_home = next(
        item
        for item in first_value.games[0].values
        if item.market is ValueMarket.MONEYLINE and item.side is ValueSide.HOME
    )
    second_home = next(
        item
        for item in second_value.games[0].values
        if item.market is ValueMarket.MONEYLINE and item.side is ValueSide.HOME
    )
    assert first_home.expected_value_per_unit != second_home.expected_value_per_unit


def test_odds_unavailable_game_remains_represented() -> None:
    slate, state, intelligence, odds_weather, quality = _one_game_chain()
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        data_quality=quality,
    )
    predictions = predict_model_feature_set(build_model_feature_set(packet))
    result = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    assert len(result.games) == 1
    assert result.games[0].values == ()
    assert result.games[0].warnings == (ValueGameWarning.ODDS_UNAVAILABLE,)
    assert result.games[0].calculation_state is ValueCalculationState.UNAVAILABLE


def test_insufficient_prediction_remains_represented_and_ineligible() -> None:
    packet, predictions = _packet_predictions()
    prediction = replace(
        predictions.games[0],
        quality_disposition=DataQualityDisposition.INSUFFICIENT,
        quality_issue_codes=("fixture_insufficient",),
    )
    predictions = replace(predictions, games=(prediction,))
    result = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    assert len(result.games) == 1
    assert result.games[0].quality_disposition is DataQualityDisposition.INSUFFICIENT
    assert all(
        ValueIneligibilityReason.DATA_QUALITY_INSUFFICIENT
        in item.ineligibility_reasons
        for item in result.games[0].values
    )


def test_market_reference_mismatch_fails_closed() -> None:
    packet, predictions = _packet_predictions()
    changed_game = replace(
        predictions.games[0],
        market_reference_checksum="f" * 64,
    )
    changed_predictions = replace(predictions, games=(changed_game,))
    with pytest.raises(ValueEngineContractError, match="market-reference"):
        evaluate_value_engine(
            predictions=changed_predictions,
            matchup_packet=packet,
        )


def test_zero_game_inputs_produce_deterministic_zero_game_value_snapshot() -> None:
    slate, state, intelligence, odds_weather, quality = _zero_game_chain()
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=intelligence,
        odds_weather=odds_weather,
        data_quality=quality,
    )
    predictions = predict_model_feature_set(build_model_feature_set(packet))
    first = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    second = evaluate_value_engine(predictions=predictions, matchup_packet=packet)
    assert first.games == ()
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
