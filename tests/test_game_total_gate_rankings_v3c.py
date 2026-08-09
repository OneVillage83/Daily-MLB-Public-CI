from __future__ import annotations

from dataclasses import replace

import pytest

from app.rankings.game_total import (
    GAME_TOTAL_RANKINGS_CONTRACT_VERSION,
    GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION,
    GameTotalRankingsError,
    build_game_total_reference_rankings,
)
from app.rankings.production import RANKINGS_PRODUCTION_CONTRACT
from app.recommendation_gate.game_total import (
    GAME_TOTAL_GATE_GAME_CONTRACT_VERSION,
    GAME_TOTAL_POLICY_PENDING_REASON,
    GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION,
    GAME_TOTAL_REFERENCE_REASON,
    GameTotalGateError,
    evaluate_game_total_reference_gate,
)
from app.recommendation_gate.production import RECOMMENDATION_GATE_PRODUCTION_CONTRACT
from tests.test_game_total_value_v3b import _game_total_value_fixture


def test_v3c_preserves_v1_moneyline_gate_and_rankings_contracts() -> None:
    assert RECOMMENDATION_GATE_PRODUCTION_CONTRACT == "DSE_MLB_ML_RECOMMENDATION_GATE_V1"
    assert RANKINGS_PRODUCTION_CONTRACT == "DSE_MLB_ML_RANKINGS_V1"


def test_v3c_reference_gate_retains_every_total_value_row_as_pass() -> None:
    _, _, value_game = _game_total_value_fixture()
    gate = evaluate_game_total_reference_gate(value_game)

    assert gate.contract_version == GAME_TOTAL_GATE_GAME_CONTRACT_VERSION
    assert gate.policy_version == GAME_TOTAL_REFERENCE_GATE_POLICY_VERSION
    assert gate.source_game_id == value_game.source_game_id
    assert gate.source_value_game_checksum == value_game.checksum
    assert gate.source_prediction_checksum == value_game.source_prediction_checksum
    assert len(gate.outcomes) == len(value_game.values) == 2
    assert gate.recommendation_count == 0
    assert {outcome.decision for outcome in gate.outcomes} == {"pass"}
    assert {outcome.side for outcome in gate.outcomes} == {"over", "under"}
    assert {outcome.total_line for outcome in gate.outcomes} == {9.0}
    assert {outcome.source_value_checksum for outcome in gate.outcomes} == {
        value.checksum for value in value_game.values
    }
    for outcome in gate.outcomes:
        assert GAME_TOTAL_REFERENCE_REASON in outcome.reason_codes
        assert GAME_TOTAL_POLICY_PENDING_REASON in outcome.reason_codes
        assert "prediction_not_recommendation_eligible" in outcome.reason_codes


def test_positive_game_total_ev_cannot_bypass_reference_lifecycle_block() -> None:
    _, _, value_game = _game_total_value_fixture()
    over = next(value for value in value_game.values if value.side == "over")
    positive = replace(
        over,
        expected_value_per_unit=0.50,
        expected_roi_percent=50.0,
    )
    modified = replace(
        value_game,
        values=tuple(positive if value.side == "over" else value for value in value_game.values),
    )
    gate = evaluate_game_total_reference_gate(modified)
    gated_over = next(outcome for outcome in gate.outcomes if outcome.side == "over")

    assert positive.expected_value_per_unit > 0.0
    assert gated_over.decision == "pass"
    assert GAME_TOTAL_REFERENCE_REASON in gated_over.reason_codes
    assert gate.recommendation_count == 0


def test_v3c_reference_rankings_retain_all_gate_rows_without_rank() -> None:
    _, _, value_game = _game_total_value_fixture()
    gate = evaluate_game_total_reference_gate(value_game)
    rankings = build_game_total_reference_rankings((gate,))

    assert rankings.contract_version == GAME_TOTAL_RANKINGS_CONTRACT_VERSION
    assert rankings.policy_version == GAME_TOTAL_REFERENCE_RANKING_POLICY_VERSION
    assert rankings.recommendation_count == 0
    assert len(rankings.games) == 1
    game = rankings.games[0]
    assert game.ordinal == 1
    assert game.source_game_id == gate.source_game_id
    assert game.upstream_gate_game_checksum == gate.checksum
    assert len(game.entries) == len(gate.outcomes)
    assert all(entry.decision == "pass" for entry in game.entries)
    assert all(entry.rank_eligible is False for entry in game.entries)
    assert all(entry.recommendation_rank is None for entry in game.entries)
    assert {entry.upstream_gate_outcome_checksum for entry in game.entries} == {
        outcome.checksum for outcome in gate.outcomes
    }


def test_v3c_empty_market_warning_survives_gate_and_rankings() -> None:
    _, _, value_game = _game_total_value_fixture()
    empty = replace(value_game, values=(), warnings=("totals_unavailable",))
    gate = evaluate_game_total_reference_gate(empty)
    rankings = build_game_total_reference_rankings((gate,))

    assert gate.outcomes == ()
    assert gate.warnings == ("totals_unavailable",)
    assert rankings.games[0].entries == ()
    assert rankings.games[0].warnings == ("totals_unavailable",)
    assert rankings.recommendation_count == 0


def test_v3c_contracts_reject_manual_recommend_or_rank_promotion() -> None:
    _, _, value_game = _game_total_value_fixture()
    gate = evaluate_game_total_reference_gate(value_game)
    outcome = gate.outcomes[0]
    with pytest.raises(GameTotalGateError, match="only emit PASS"):
        replace(outcome, decision="recommend")

    rankings = build_game_total_reference_rankings((gate,))
    entry = rankings.games[0].entries[0]
    with pytest.raises(GameTotalRankingsError, match="cannot receive a recommendation rank"):
        replace(entry, rank_eligible=True, recommendation_rank=1)
