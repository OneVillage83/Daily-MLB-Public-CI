from __future__ import annotations

from dataclasses import replace

import pytest

from app.rankings.production import RANKINGS_PRODUCTION_CONTRACT
from app.rankings.run_line import (
    RUN_LINE_RANKINGS_CONTRACT_VERSION,
    RUN_LINE_REFERENCE_RANKING_POLICY_VERSION,
    RunLineRankingsError,
    build_run_line_reference_rankings,
)
from app.recommendation_gate.production import RECOMMENDATION_GATE_PRODUCTION_CONTRACT
from app.recommendation_gate.run_line import (
    RUN_LINE_GATE_GAME_CONTRACT_VERSION,
    RUN_LINE_POLICY_PENDING_REASON,
    RUN_LINE_REFERENCE_GATE_POLICY_VERSION,
    RUN_LINE_REFERENCE_REASON,
    RunLineGateError,
    evaluate_run_line_reference_gate,
)
from tests.test_run_line_value_v2b import _run_line_value_fixture


def test_v2c_preserves_frozen_v1_moneyline_gate_and_rankings_contracts() -> None:
    assert RECOMMENDATION_GATE_PRODUCTION_CONTRACT == "DSE_MLB_ML_RECOMMENDATION_GATE_V1"
    assert RANKINGS_PRODUCTION_CONTRACT == "DSE_MLB_ML_RANKINGS_V1"


def test_v2c_reference_gate_retains_every_value_row_as_pass() -> None:
    _, _, value_game = _run_line_value_fixture()
    gate = evaluate_run_line_reference_gate(value_game)

    assert gate.contract_version == RUN_LINE_GATE_GAME_CONTRACT_VERSION
    assert gate.policy_version == RUN_LINE_REFERENCE_GATE_POLICY_VERSION
    assert gate.source_game_id == value_game.source_game_id
    assert gate.source_value_game_checksum == value_game.checksum
    assert gate.source_prediction_checksum == value_game.source_prediction_checksum
    assert len(gate.outcomes) == len(value_game.values) == 2
    assert gate.recommendation_count == 0
    assert {outcome.decision for outcome in gate.outcomes} == {"pass"}
    assert {outcome.source_value_checksum for outcome in gate.outcomes} == {
        value.checksum for value in value_game.values
    }
    for outcome in gate.outcomes:
        assert RUN_LINE_REFERENCE_REASON in outcome.reason_codes
        assert RUN_LINE_POLICY_PENDING_REASON in outcome.reason_codes
        assert "prediction_not_recommendation_eligible" in outcome.reason_codes


def test_positive_run_line_ev_cannot_bypass_reference_lifecycle_block() -> None:
    _, _, value_game = _run_line_value_fixture()
    home = next(value for value in value_game.values if value.side == "home")
    positive = replace(
        home,
        expected_value_per_unit=0.50,
        expected_roi_percent=50.0,
    )
    modified = replace(
        value_game,
        values=tuple(positive if value.side == "home" else value for value in value_game.values),
    )
    gate = evaluate_run_line_reference_gate(modified)
    gated_home = next(outcome for outcome in gate.outcomes if outcome.side == "home")

    assert positive.expected_value_per_unit > 0.0
    assert gated_home.decision == "pass"
    assert RUN_LINE_REFERENCE_REASON in gated_home.reason_codes
    assert gate.recommendation_count == 0


def test_v2c_reference_rankings_retain_all_gate_rows_without_rank() -> None:
    _, _, value_game = _run_line_value_fixture()
    gate = evaluate_run_line_reference_gate(value_game)
    rankings = build_run_line_reference_rankings((gate,))

    assert rankings.contract_version == RUN_LINE_RANKINGS_CONTRACT_VERSION
    assert rankings.policy_version == RUN_LINE_REFERENCE_RANKING_POLICY_VERSION
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


def test_v2c_empty_market_warning_survives_gate_and_rankings() -> None:
    _, _, value_game = _run_line_value_fixture()
    empty = replace(value_game, values=(), warnings=("spreads_unavailable",))
    gate = evaluate_run_line_reference_gate(empty)
    rankings = build_run_line_reference_rankings((gate,))

    assert gate.outcomes == ()
    assert gate.warnings == ("spreads_unavailable",)
    assert rankings.games[0].entries == ()
    assert rankings.games[0].warnings == ("spreads_unavailable",)
    assert rankings.recommendation_count == 0


def test_v2c_contracts_reject_manual_recommend_or_rank_promotion() -> None:
    _, _, value_game = _run_line_value_fixture()
    gate = evaluate_run_line_reference_gate(value_game)
    outcome = gate.outcomes[0]
    with pytest.raises(RunLineGateError, match="only emit PASS"):
        replace(outcome, decision="recommend")

    rankings = build_run_line_reference_rankings((gate,))
    entry = rankings.games[0].entries[0]
    with pytest.raises(RunLineRankingsError, match="cannot receive a recommendation rank"):
        replace(entry, rank_eligible=True, recommendation_rank=1)
