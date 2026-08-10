from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.odds_weather.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_ALTERNATE_MARKET,
    PITCHER_STRIKEOUT_MARKET,
    PitcherStrikeoutOddsEvidenceV1,
    normalize_pitcher_strikeout_odds,
)
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    PredictionDistributionKind,
)
from app.predictions.pitcher_strikeouts import (
    PitcherStarterBindingState,
    build_pitcher_strikeout_prediction,
)
from app.value_engine.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
    PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
    PITCHER_STRIKEOUT_REFERENCE_REASON,
    PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON,
    PitcherStrikeoutValueError,
    evaluate_pitcher_strikeout_value,
)


def _distribution() -> DiscreteDistributionV1:
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.PITCHER_STRIKEOUTS,
        outcomes=(
            DiscreteOutcomeProbabilityV1(3, 0.05),
            DiscreteOutcomeProbabilityV1(4, 0.05),
            DiscreteOutcomeProbabilityV1(5, 0.10),
            DiscreteOutcomeProbabilityV1(6, 0.30),
            DiscreteOutcomeProbabilityV1(7, 0.30),
            DiscreteOutcomeProbabilityV1(8, 0.20),
        ),
    )


def _prediction(state: PitcherStarterBindingState = PitcherStarterBindingState.CONFIRMED):
    return build_pitcher_strikeout_prediction(
        source_game_id="statcast:12345",
        pitcher_id="mlbam:543210",
        pitcher_name="Example Starter",
        team_id="NYY",
        opponent_team_id="BOS",
        starter_binding_state=state,
        starter_binding_checksum="c" * 64,
        source_model_input_checksum="a" * 64,
        model_manifest_checksum="b" * 64,
        model_id="DSE_PITCHER_K_REFERENCE_V1",
        model_version="1.0.0",
        expected_strikeouts=6.35,
        strikeout_distribution=_distribution(),
    )


def _event() -> dict:
    return {
        "id": "event-v7-value",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T02:00:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-10T01:00:00Z",
                "markets": [
                    {
                        "key": PITCHER_STRIKEOUT_MARKET,
                        "last_update": "2026-08-10T01:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": -105, "point": 5.5},
                            {"name": "Under", "description": "Example Starter", "price": -115, "point": 5.5},
                        ],
                    },
                    {
                        "key": PITCHER_STRIKEOUT_ALTERNATE_MARKET,
                        "last_update": "2026-08-10T01:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": 130, "point": 6.5},
                            {"name": "Under", "description": "Example Starter", "price": -160, "point": 6.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": "2026-08-10T01:00:20Z",
                "markets": [
                    {
                        "key": PITCHER_STRIKEOUT_MARKET,
                        "last_update": "2026-08-10T01:00:20Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": -110, "point": 5.5},
                            {"name": "Under", "description": "Example Starter", "price": -110, "point": 5.5},
                        ],
                    },
                    {
                        "key": PITCHER_STRIKEOUT_ALTERNATE_MARKET,
                        "last_update": "2026-08-10T01:00:20Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": 125, "point": 6.5},
                            {"name": "Under", "description": "Example Starter", "price": -155, "point": 6.5},
                        ],
                    },
                ],
            },
        ],
    }


def _odds():
    evidence = PitcherStrikeoutOddsEvidenceV1(
        provider_event_id="event-v7-value",
        retrieved_at=datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
        raw_capture_checksum="d" * 64,
        event=_event(),
    )
    return normalize_pitcher_strikeout_odds(evidence)


def test_v7b_evaluates_featured_and_alternate_lines_for_both_sides() -> None:
    prediction = _prediction()
    odds = _odds()
    value = evaluate_pitcher_strikeout_value(prediction, odds)
    assert len(value.outcomes) == 4
    identities = {(row.market_key, row.side, row.line) for row in value.outcomes}
    assert identities == {
        (PITCHER_STRIKEOUT_MARKET, "over", 5.5),
        (PITCHER_STRIKEOUT_MARKET, "under", 5.5),
        (PITCHER_STRIKEOUT_ALTERNATE_MARKET, "over", 6.5),
        (PITCHER_STRIKEOUT_ALTERNATE_MARKET, "under", 6.5),
    }
    featured_over = next(
        row for row in value.outcomes
        if row.market_key == PITCHER_STRIKEOUT_MARKET and row.side == "over"
    )
    assert featured_over.american_price == -105.0
    assert featured_over.best_price_books == ("draftkings",)
    assert featured_over.bookmaker_count == 2
    assert featured_over.no_vig_probability is not None
    assert featured_over.expected_value_per_unit > 0.0
    assert featured_over.recommendation_gate_input_eligible is False


def test_v7b_retains_exact_prediction_projection_and_odds_lineage() -> None:
    prediction = _prediction()
    odds = _odds()
    value = evaluate_pitcher_strikeout_value(prediction, odds)
    for row in value.outcomes:
        assert row.source_prediction_checksum == prediction.checksum
        assert row.source_projection_checksum == prediction.project(row.line).checksum
        assert row.source_normalized_odds_checksum == odds.checksum
        assert row.starter_binding_checksum == prediction.starter_binding_checksum
        assert row.provider_pitcher_key == "example starter"
        for required in (
            PITCHER_STRIKEOUT_REFERENCE_REASON,
            PITCHER_STRIKEOUT_EVENT_BINDING_PENDING_REASON,
            PITCHER_STRIKEOUT_PLAYER_BINDING_PENDING_REASON,
        ):
            assert required in row.ineligibility_reasons


def test_expected_starter_remains_confirmation_blocked_but_confirmed_does_not() -> None:
    odds = _odds()
    expected = evaluate_pitcher_strikeout_value(
        _prediction(PitcherStarterBindingState.EXPECTED), odds
    )
    confirmed = evaluate_pitcher_strikeout_value(
        _prediction(PitcherStarterBindingState.CONFIRMED), odds
    )
    assert expected.outcomes
    assert confirmed.outcomes
    assert all(
        PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON in row.ineligibility_reasons
        for row in expected.outcomes
    )
    assert all(
        PITCHER_STRIKEOUT_STARTER_CONFIRMATION_PENDING_REASON not in row.ineligibility_reasons
        for row in confirmed.outcomes
    )


def test_v7b_rejects_game_team_mismatch() -> None:
    with pytest.raises(PitcherStrikeoutValueError, match="teams do not match"):
        evaluate_pitcher_strikeout_value(replace(_prediction(), team_id="LAD"), _odds())


def test_v7b_value_keeps_value_metrics_out_of_recommendation_contract_fields() -> None:
    value = evaluate_pitcher_strikeout_value(_prediction(), _odds())
    serialized = str(value.as_dict()).casefold()
    assert "expected_value_per_unit" in serialized
    assert "no_vig_probability" in serialized
    assert "recommendation_rank" not in serialized
    assert "decision" not in serialized
