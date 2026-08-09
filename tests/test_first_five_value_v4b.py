from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.first_five import FirstFiveOddsEvidenceV1, normalize_first_five_odds
from app.predictions.first_five import (
    build_first_five_moneyline_prediction,
    build_first_five_run_line_prediction,
    build_first_five_total_prediction,
)
from app.predictions.market_foundation import PredictionMarketFamily
from app.value_engine.first_five import (
    FIRST_FIVE_BINDING_PENDING_REASON,
    FIRST_FIVE_REFERENCE_REASON,
    FIRST_FIVE_VALUE_GAME_CONTRACT_VERSION,
    evaluate_first_five_value,
)
from tests.test_first_five_odds_v4o import _event
from tests.test_first_five_prediction_v4a import _source


def _normalized_odds():
    event = _event()
    for bookmaker in event["bookmakers"]:  # type: ignore[union-attr]
        for market in bookmaker["markets"]:
            if market["key"] == "totals_1st_5_innings":
                for outcome in market["outcomes"]:
                    outcome["point"] = 2.5
    evidence = FirstFiveOddsEvidenceV1(
        provider_event_id="eventabc123",
        retrieved_at=datetime(2026, 8, 9, 21, 1, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=event,
    )
    return normalize_first_five_odds(evidence, run_id="run_v4b")


def _predictions():
    source = _source(
        source_game_id="2026-08-09_SF_LAD_1",
        away_team_id="SF",
        home_team_id="LAD",
    )
    return (
        source,
        build_first_five_moneyline_prediction(source),
        build_first_five_run_line_prediction(source),
        build_first_five_total_prediction(source),
    )


def test_v4b_evaluates_all_three_f5_families_and_keeps_gate_blocked() -> None:
    source, moneyline, run_line, total = _predictions()
    value = evaluate_first_five_value(moneyline, run_line, total, _normalized_odds())

    assert value.contract_version == FIRST_FIVE_VALUE_GAME_CONTRACT_VERSION
    assert value.source_game_id == source.source_game_id
    assert value.upstream_first_five_scoring_checksum == source.checksum
    assert value.recommendation_gate_input_count == 0
    assert len(value.outcomes) == 6
    assert {item.market_family for item in value.outcomes} == {
        PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value,
        PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value,
        PredictionMarketFamily.FIRST_FIVE_TOTAL.value,
    }
    assert all(item.recommendation_gate_input_eligible is False for item in value.outcomes)
    assert all(FIRST_FIVE_REFERENCE_REASON in item.ineligibility_reasons for item in value.outcomes)
    assert all(FIRST_FIVE_BINDING_PENDING_REASON in item.ineligibility_reasons for item in value.outcomes)


def test_moneyline_value_uses_best_book_and_treats_f5_tie_as_push() -> None:
    _, moneyline, run_line, total = _predictions()
    value = evaluate_first_five_value(moneyline, run_line, total, _normalized_odds())
    lad = next(
        item
        for item in value.outcomes
        if item.market_family == PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value
        and item.side == "LAD"
    )

    assert lad.market_key == "h2h_1st_5_innings"
    assert lad.american_price == -115.0
    assert lad.best_price_books == ("fanduel",)
    assert lad.bookmaker_count == 2
    assert lad.resolved_model_win_probability == pytest.approx(0.36)
    assert lad.resolved_model_loss_probability == pytest.approx(0.28)
    assert lad.resolved_model_push_probability == pytest.approx(0.36)
    assert lad.conditional_model_probability == pytest.approx(0.5625)
    assert lad.no_vig_probability is not None
    assert lad.expected_roi_percent == pytest.approx(lad.expected_value_per_unit * 100.0)


def test_run_line_and_total_value_keep_exact_line_projection_lineage() -> None:
    _, moneyline, run_line, total = _predictions()
    value = evaluate_first_five_value(moneyline, run_line, total, _normalized_odds())

    spread_rows = [
        item
        for item in value.outcomes
        if item.market_family == PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value
    ]
    total_rows = [
        item
        for item in value.outcomes
        if item.market_family == PredictionMarketFamily.FIRST_FIVE_TOTAL.value
    ]

    assert {item.market_line for item in spread_rows} == {-0.5}
    assert {item.market_key for item in spread_rows} == {"spreads_1st_5_innings"}
    assert {item.market_line for item in total_rows} == {2.5}
    assert {item.market_key for item in total_rows} == {"totals_1st_5_innings"}
    assert all(len(item.source_projection_checksum) == 64 for item in spread_rows + total_rows)
    assert all(item.complete_two_way_market is True for item in spread_rows + total_rows)


def test_v4b_rejects_odds_from_the_wrong_team_pair() -> None:
    _, moneyline, run_line, total = _predictions()
    odds = _normalized_odds()
    odds.summary["home_team_key"] = "NYY"

    with pytest.raises(Exception, match="teams do not match"):
        evaluate_first_five_value(moneyline, run_line, total, odds)


def test_v4b_value_contains_market_context_but_no_recommendation_decision() -> None:
    _, moneyline, run_line, total = _predictions()
    document = evaluate_first_five_value(
        moneyline,
        run_line,
        total,
        _normalized_odds(),
    ).as_dict()
    serialized = str(document)

    assert "no_vig_probability" in serialized
    assert "expected_value_per_unit" in serialized
    assert "best_price_books" in serialized
    assert "recommendation_gate_input_eligible" in serialized
    assert "BET" not in serialized
    assert "LEAN" not in serialized
