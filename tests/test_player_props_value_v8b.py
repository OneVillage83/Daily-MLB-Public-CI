from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.odds_weather.player_props import PlayerPropsOddsEvidenceV1, normalize_player_prop_odds
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    PredictionDistributionKind,
)
from app.predictions.player_props import (
    PlayerPropRole,
    PlayerPropStatistic,
    build_player_prop_prediction,
)
from app.value_engine.player_props import (
    PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
    PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
    PLAYER_PROP_REFERENCE_REASON,
    PlayerPropsValueError,
    evaluate_player_props_value,
)


def _prediction(player_name: str = "Aaron Judge", team_id: str = "NYY"):
    opponent = "BOS" if team_id != "BOS" else "NYY"
    return build_player_prop_prediction(
        source_game_id="statcast:12345",
        player_id="mlbam:592450",
        player_name=player_name,
        team_id=team_id,
        opponent_team_id=opponent,
        role=PlayerPropRole.BATTER,
        statistic=PlayerPropStatistic.BATTER_HITS,
        source_model_input_checksum="a" * 64,
        model_manifest_checksum="b" * 64,
        model_id="DSE_PLAYER_HITS_REFERENCE_V1",
        model_version="1.0.0",
        expected_stat_count=1.8,
        stat_distribution=DiscreteDistributionV1(
            kind=PredictionDistributionKind.PLAYER_STAT_COUNT,
            outcomes=(
                DiscreteOutcomeProbabilityV1(0, 0.10),
                DiscreteOutcomeProbabilityV1(1, 0.20),
                DiscreteOutcomeProbabilityV1(2, 0.40),
                DiscreteOutcomeProbabilityV1(3, 0.25),
            ),
            unresolved_probability=0.05,
        ),
    )


def _odds():
    event = {
        "id": "event123",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T00:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "batter_hits",
                        "last_update": "2026-08-09T19:59:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": -105, "point": 1.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -115, "point": 1.5},
                        ],
                    }
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "batter_hits",
                        "last_update": "2026-08-09T19:59:10Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": -110, "point": 1.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -110, "point": 1.5},
                        ],
                    }
                ],
            },
        ],
    }
    evidence = PlayerPropsOddsEvidenceV1(
        provider_event_id="event123",
        retrieved_at=datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
        raw_capture_checksum="c" * 64,
        event=event,
    )
    return normalize_player_prop_odds(evidence)


def test_value_evaluates_player_line_and_keeps_positive_ev_blocked() -> None:
    prediction = _prediction()
    odds = _odds()
    value = evaluate_player_props_value((prediction,), odds)
    assert len(value.outcomes) == 2
    over = next(item for item in value.outcomes if item.side == "over")
    assert over.player_id == prediction.player_id
    assert over.provider_player_key == "aaron judge"
    assert over.market_key == "batter_hits"
    assert over.line == pytest.approx(1.5)
    assert over.american_price == pytest.approx(-105.0)
    assert over.expected_value_per_unit > 0.0
    assert over.recommendation_gate_input_eligible is False
    assert over.source_prediction_checksum == prediction.checksum
    assert over.source_projection_checksum == prediction.project(1.5).checksum
    assert over.source_normalized_odds_checksum == odds.checksum
    assert PLAYER_PROP_REFERENCE_REASON in over.ineligibility_reasons
    assert PLAYER_PROP_EVENT_BINDING_PENDING_REASON in over.ineligibility_reasons
    assert PLAYER_PROP_PLAYER_BINDING_PENDING_REASON in over.ineligibility_reasons
    assert "unresolved_tail_exceeds_value_tolerance" in over.ineligibility_reasons
    assert value.recommendation_gate_input_count == 0


def test_provider_player_name_mismatch_is_retained_as_warning_not_guessed() -> None:
    value = evaluate_player_props_value((_prediction("Different Batter"),), _odds())
    assert value.outcomes == ()
    assert any(item.startswith("provider_player_unmatched:") for item in value.warnings)
    assert "no_evaluable_player_prop_market" in value.warnings


def test_event_team_mismatch_fails_closed() -> None:
    bad = replace(_prediction(), team_id="LAD", opponent_team_id="BOS")
    with pytest.raises(PlayerPropsValueError, match="teams do not match"):
        evaluate_player_props_value((bad,), _odds())


def test_statistic_without_matching_market_is_not_cross_mapped() -> None:
    prediction = replace(
        _prediction(),
        statistic=PlayerPropStatistic.BATTER_TOTAL_BASES,
        target=replace(_prediction().target, statistic="total_bases"),
    )
    value = evaluate_player_props_value((prediction,), _odds())
    assert value.outcomes == ()
    assert any(item.startswith("player_stat_market_unavailable:") for item in value.warnings)
