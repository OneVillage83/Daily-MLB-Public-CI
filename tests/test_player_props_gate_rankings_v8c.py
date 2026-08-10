from __future__ import annotations

from dataclasses import replace

import pytest

from app.rankings.player_props import build_player_props_reference_rankings
from app.recommendation_gate.player_props import (
    PLAYER_PROP_POLICY_PENDING_REASON,
    PLAYER_PROP_REFERENCE_GATE_REASON,
    PlayerPropsGateError,
    evaluate_player_props_reference_gate,
)
from app.value_engine.player_props import (
    PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
    PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
    PLAYER_PROP_REFERENCE_REASON,
    PlayerPropOutcomeValueV1,
    PlayerPropsGameValueV1,
)
from app.value_engine.pricing import calculate_value_math


def _value_game() -> PlayerPropsGameValueV1:
    math = calculate_value_math(
        win_probability=0.65,
        loss_probability=0.35,
        push_probability=0.0,
        price=-105,
        retained_raw_implied_probability=None,
        no_vig_probability=0.50,
    )
    outcome = PlayerPropOutcomeValueV1(
        source_game_id="statcast:12345",
        provider_event_id="event123",
        player_id="mlbam:592450",
        player_name="Example Batter",
        provider_player_key="example batter",
        team_id="NYY",
        opponent_team_id="BOS",
        role="batter",
        statistic="hits",
        market_key="batter_hits",
        side="over",
        line_key="1.5",
        line=1.5,
        american_price=math.american_price,
        best_price_books=("draftkings",),
        bookmaker_count=2,
        consensus_confidence="low",
        freshness_status="fresh",
        complete_two_way_market=True,
        median_market_hold=0.04,
        resolved_model_win_probability=math.model_win_probability,
        resolved_model_loss_probability=math.model_loss_probability,
        resolved_model_push_probability=math.model_push_probability,
        unresolved_probability=0.0,
        conditional_model_probability=math.conditional_model_probability,
        raw_implied_probability=math.raw_implied_probability,
        no_vig_probability=math.no_vig_probability,
        raw_probability_edge=math.raw_probability_edge,
        no_vig_probability_edge=math.no_vig_probability_edge,
        net_profit_per_unit=math.net_profit_per_unit,
        expected_value_per_unit=math.expected_value_per_unit,
        expected_roi_percent=math.expected_roi_percent,
        fair_decimal_odds=math.fair_decimal_odds,
        fair_american_odds=math.fair_american_odds,
        market_evidence_checksum="a" * 64,
        source_prediction_checksum="b" * 64,
        source_projection_checksum="c" * 64,
        source_normalized_odds_checksum="d" * 64,
        prediction_recommendation_eligible=False,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=(
            PLAYER_PROP_REFERENCE_REASON,
            PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
            PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
        ),
    )
    assert outcome.expected_value_per_unit > 0.0
    return PlayerPropsGameValueV1(
        source_game_id="statcast:12345",
        provider_event_id="event123",
        source_normalized_odds_checksum="d" * 64,
        source_prediction_checksums=("b" * 64,),
        outcomes=(outcome,),
    )


def test_positive_ev_value_survives_gate_as_pass_with_exact_lineage() -> None:
    value_game = _value_game()
    gate = evaluate_player_props_reference_gate(value_game)
    assert gate.recommendation_count == 0
    assert len(gate.outcomes) == len(value_game.outcomes) == 1
    row = gate.outcomes[0]
    assert row.decision == "pass"
    assert row.source_value_checksum == value_game.outcomes[0].checksum
    assert row.player_id == value_game.outcomes[0].player_id
    assert row.provider_player_key == value_game.outcomes[0].provider_player_key
    assert row.statistic == "hits"
    assert row.line == pytest.approx(1.5)
    for required in (
        PLAYER_PROP_REFERENCE_REASON,
        PLAYER_PROP_EVENT_BINDING_PENDING_REASON,
        PLAYER_PROP_PLAYER_BINDING_PENDING_REASON,
        PLAYER_PROP_REFERENCE_GATE_REASON,
        PLAYER_PROP_POLICY_PENDING_REASON,
    ):
        assert required in row.reason_codes


def test_rankings_retain_every_gate_row_but_never_rank_reference_props() -> None:
    gate = evaluate_player_props_reference_gate(_value_game())
    rankings = build_player_props_reference_rankings((gate,))
    assert rankings.recommendation_count == 0
    assert len(rankings.games) == 1
    assert len(rankings.games[0].entries) == len(gate.outcomes)
    entry = rankings.games[0].entries[0]
    assert entry.decision == "pass"
    assert entry.rank_eligible is False
    assert entry.recommendation_rank is None
    assert entry.upstream_gate_outcome_checksum == gate.outcomes[0].checksum
    assert entry.exclusion_reasons == gate.outcomes[0].reason_codes


def test_gate_and_rankings_do_not_copy_value_price_or_ev_metrics() -> None:
    value_game = _value_game()
    gate = evaluate_player_props_reference_gate(value_game)
    rankings = build_player_props_reference_rankings((gate,))
    assert value_game.outcomes[0].expected_value_per_unit > 0.0
    gate_text = str(gate.as_dict()).casefold()
    ranking_text = str(rankings.as_dict()).casefold()
    for forbidden in (
        "american_price",
        "best_price",
        "no_vig_probability",
        "expected_value_per_unit",
        "expected_roi_percent",
        "fair_american_odds",
    ):
        assert forbidden not in gate_text
        assert forbidden not in ranking_text


def test_reference_gate_rejects_manual_recommend_decision() -> None:
    gate = evaluate_player_props_reference_gate(_value_game())
    with pytest.raises(PlayerPropsGateError, match="only emit PASS"):
        replace(gate.outcomes[0], decision="recommend")
