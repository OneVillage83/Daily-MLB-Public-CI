from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.rankings.team_totals import build_team_totals_reference_rankings
from app.recommendation_gate.team_totals import (
    TEAM_TOTAL_POLICY_PENDING_REASON,
    TEAM_TOTAL_REFERENCE_GATE_REASON,
    TeamTotalsGateError,
    evaluate_team_totals_reference_gate,
)
from app.value_engine.team_totals import (
    TEAM_TOTAL_BINDING_PENDING_REASON,
    TEAM_TOTAL_REFERENCE_REASON,
    TeamTotalOutcomeValueV1,
    TeamTotalsGameValueV1,
)

RAW_IMPLIED_MINUS_110 = 110.0 / 210.0


def _row(
    *,
    team_id: str,
    opponent_team_id: str,
    side: str,
    line_key: str,
    line: float,
    prediction_checksum: str,
    marker: str,
) -> TeamTotalOutcomeValueV1:
    return TeamTotalOutcomeValueV1(
        source_game_id="2026-08-10_NYY_BOS_1",
        provider_event_id="event-1",
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        side=side,
        line_key=line_key,
        total_line=line,
        american_price=-110.0,
        best_price_books=("book-a",),
        bookmaker_count=2,
        consensus_confidence="low",
        freshness_status="fresh",
        complete_two_way_market=True,
        median_market_hold=0.04761904761904767,
        resolved_model_win_probability=0.55,
        resolved_model_loss_probability=0.45,
        resolved_model_push_probability=0.0,
        unresolved_probability=0.10,
        conditional_model_probability=0.55,
        raw_implied_probability=RAW_IMPLIED_MINUS_110,
        no_vig_probability=0.50,
        raw_probability_edge=0.55 - RAW_IMPLIED_MINUS_110,
        no_vig_probability_edge=0.05,
        net_profit_per_unit=100.0 / 110.0,
        expected_value_per_unit=0.05,
        expected_roi_percent=5.0,
        fair_decimal_odds=1.0 / 0.55,
        fair_american_odds=-100.0 * 0.55 / 0.45,
        market_evidence_checksum=marker * 64,
        source_prediction_checksum=prediction_checksum,
        source_projection_checksum=("d" if marker != "d" else "e") * 64,
        source_normalized_team_odds_checksum=("f" if marker != "f" else "e") * 64,
        source_normalized_odds_checksum="c" * 64,
        prediction_recommendation_eligible=False,
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=(
            TEAM_TOTAL_REFERENCE_REASON,
            TEAM_TOTAL_BINDING_PENDING_REASON,
        ),
    )


def _value_game() -> TeamTotalsGameValueV1:
    return TeamTotalsGameValueV1(
        source_game_id="2026-08-10_NYY_BOS_1",
        provider_event_id="event-1",
        away_team_id="NYY",
        home_team_id="BOS",
        source_prediction_checksums=("a" * 64, "b" * 64),
        source_normalized_odds_checksum="c" * 64,
        outcomes=(
            _row(
                team_id="NYY",
                opponent_team_id="BOS",
                side="over",
                line_key="4.5",
                line=4.5,
                prediction_checksum="a" * 64,
                marker="1",
            ),
            _row(
                team_id="BOS",
                opponent_team_id="NYY",
                side="under",
                line_key="5",
                line=5.0,
                prediction_checksum="b" * 64,
                marker="2",
            ),
        ),
    )


def test_v6c_retains_every_value_row_as_pass() -> None:
    value = _value_game()
    gate = evaluate_team_totals_reference_gate(value)

    assert len(gate.outcomes) == len(value.outcomes) == 2
    assert gate.source_value_game_checksum == value.checksum
    assert gate.source_prediction_checksums == value.source_prediction_checksums
    assert gate.source_normalized_odds_checksum == value.source_normalized_odds_checksum
    assert gate.recommendation_count == 0
    for source, outcome in zip(value.outcomes, gate.outcomes, strict=True):
        assert outcome.decision == "pass"
        assert outcome.source_value_checksum == source.checksum
        assert outcome.team_id == source.team_id
        assert outcome.side == source.side
        assert outcome.total_line == source.total_line
        assert TEAM_TOTAL_REFERENCE_REASON in outcome.reason_codes
        assert TEAM_TOTAL_BINDING_PENDING_REASON in outcome.reason_codes
        assert TEAM_TOTAL_REFERENCE_GATE_REASON in outcome.reason_codes
        assert TEAM_TOTAL_POLICY_PENDING_REASON in outcome.reason_codes


def test_positive_ev_still_passes_and_cannot_be_manually_promoted() -> None:
    value = _value_game()
    assert value.outcomes[0].expected_value_per_unit > 0.0
    gate = evaluate_team_totals_reference_gate(value)
    assert gate.outcomes[0].decision == "pass"

    with pytest.raises(TeamTotalsGateError, match="only emit PASS"):
        replace(gate.outcomes[0], decision="recommend")


def test_v6c_rankings_are_lossless_and_unranked() -> None:
    gate = evaluate_team_totals_reference_gate(_value_game())
    rankings = build_team_totals_reference_rankings((gate,))

    assert rankings.recommendation_count == 0
    assert len(rankings.games) == 1
    game = rankings.games[0]
    assert game.ordinal == 1
    assert game.upstream_gate_game_checksum == gate.checksum
    assert len(game.entries) == len(gate.outcomes)
    for gate_outcome, entry in zip(gate.outcomes, game.entries, strict=True):
        assert entry.upstream_gate_outcome_checksum == gate_outcome.checksum
        assert entry.decision == "pass"
        assert entry.rank_eligible is False
        assert entry.recommendation_rank is None
        assert entry.exclusion_reasons == gate_outcome.reason_codes


def test_gate_and_rankings_do_not_copy_value_price_or_ev_fields() -> None:
    gate = evaluate_team_totals_reference_gate(_value_game())
    rankings = build_team_totals_reference_rankings((gate,))
    serialized = json.dumps(
        {"gate": gate.as_dict(), "rankings": rankings.as_dict()},
        sort_keys=True,
    )

    assert "expected_value_per_unit" not in serialized
    assert "expected_roi_percent" not in serialized
    assert "no_vig_probability" not in serialized
    assert "best_price" not in serialized
    assert '"decision": "pass"' in serialized
    assert '"recommendation_rank": null' in serialized
