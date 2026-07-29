from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.rankings.contracts import (
    RankingBoardType,
    RankingExclusionReason,
    RankingGameWarning,
    RankingsContractError,
    RankingsV1,
)
from app.rankings.engine import rank_recommendations
from app.rankings.policy import RankingsPolicyError, RankingsPolicyV1
from app.recommendation_gate.contracts import (
    RecommendationDecision,
    RecommendationGateV1,
)
from app.recommendation_gate.engine import evaluate_recommendation_gate
from app.value_engine.contracts import ValueGameWarning, ValueIneligibilityReason
from tests.test_recommendation_gate import (
    _base_value_engine,
    _eligible_value_engine,
)


def _single_recommendation(*, edge: float, ev: float, books: int = 4):
    gate = evaluate_recommendation_gate(
        _eligible_value_engine(edge=edge, ev=ev, books=books)
    )
    return gate, gate.games[0].recommendations[0]


def _mixed_gate() -> tuple[RecommendationGateV1, dict[str, str]]:
    bet_conf_gate, bet_conf = _single_recommendation(edge=0.04, ev=0.03)
    _, bet_value = _single_recommendation(edge=0.08, ev=0.12, books=5)
    _, lean = _single_recommendation(edge=0.02, ev=0.01, books=3)
    _, passed = _single_recommendation(edge=0.005, ev=0.005)

    stale_engine = _eligible_value_engine(edge=0.10, ev=0.20, freshness="stale")
    stale_game = stale_engine.games[0]
    stale_value = replace(
        stale_game.values[0],
        recommendation_gate_input_eligible=False,
        ineligibility_reasons=(ValueIneligibilityReason.MARKET_STALE,),
    )
    stale_engine = replace(
        stale_engine,
        games=(replace(stale_game, values=(stale_value,)),),
    )
    avoid_gate = evaluate_recommendation_gate(stale_engine)
    avoided = avoid_gate.games[0].recommendations[0]

    bet_conf = replace(
        bet_conf,
        conditional_model_probability=0.70,
        no_vig_probability=0.66,
        no_vig_probability_edge=0.04,
    )
    bet_value = replace(
        bet_value,
        conditional_model_probability=0.58,
        no_vig_probability=0.50,
        no_vig_probability_edge=0.08,
    )
    lean = replace(
        lean,
        conditional_model_probability=0.90,
        no_vig_probability=0.88,
        no_vig_probability_edge=0.02,
    )

    recommendations = (bet_conf, bet_value, lean, passed, avoided)
    game = replace(
        bet_conf_gate.games[0],
        recommendations=recommendations,
        warnings=(),
    )
    gate = replace(bet_conf_gate, games=(game,))
    labels = {
        "bet_conf": bet_conf.checksum,
        "bet_value": bet_value.checksum,
        "lean": lean.checksum,
        "pass": passed.checksum,
        "avoid": avoided.checksum,
    }
    return gate, labels


def _placement_order(rankings: RankingsV1, board: RankingBoardType) -> list[str]:
    selected = (
        rankings.top_confidence
        if board is RankingBoardType.TOP_CONFIDENCE
        else rankings.best_value
    )
    return [item.upstream_recommendation_checksum for item in selected.placements]


def test_rankings_retains_every_recommendation_row() -> None:
    gate, labels = _mixed_gate()
    rankings = rank_recommendations(gate)
    entries = rankings.games[0].entries
    assert rankings.entry_count == 5
    assert rankings.eligible_count == 3
    assert rankings.unranked_count == 2
    assert {entry.upstream_recommendation_checksum for entry in entries} == set(
        labels.values()
    )


def test_only_bet_and_lean_receive_ranks() -> None:
    gate, labels = _mixed_gate()
    rankings = rank_recommendations(gate)
    by_upstream = {
        entry.upstream_recommendation_checksum: entry
        for entry in rankings.games[0].entries
    }
    for label in ("bet_conf", "bet_value", "lean"):
        entry = by_upstream[labels[label]]
        assert entry.ranking_eligible is True
        assert entry.top_confidence_rank is not None
        assert entry.best_value_rank is not None
        assert entry.exclusion_reason is None
    for label in ("pass", "avoid"):
        entry = by_upstream[labels[label]]
        assert entry.ranking_eligible is False
        assert entry.top_confidence_rank is None
        assert entry.best_value_rank is None
        assert (
            entry.exclusion_reason
            is RankingExclusionReason.RECOMMENDATION_NOT_ACTIONABLE
        )


def test_top_confidence_orders_tier_then_model_probability() -> None:
    gate, labels = _mixed_gate()
    rankings = rank_recommendations(gate)
    assert _placement_order(rankings, RankingBoardType.TOP_CONFIDENCE) == [
        labels["bet_conf"],
        labels["bet_value"],
        labels["lean"],
    ]
    assert rankings.top_confidence.placements[0].primary_metric_name == (
        "conditional_model_probability"
    )
    assert rankings.top_confidence.placements[0].primary_metric_value == 0.70


def test_best_value_orders_tier_then_ev_and_edge() -> None:
    gate, labels = _mixed_gate()
    rankings = rank_recommendations(gate)
    assert _placement_order(rankings, RankingBoardType.BEST_VALUE) == [
        labels["bet_value"],
        labels["bet_conf"],
        labels["lean"],
    ]
    assert rankings.best_value.placements[0].primary_metric_name == (
        "expected_value_per_unit"
    )
    assert rankings.best_value.placements[0].primary_metric_value == 0.12


def test_ranks_are_consecutive_and_match_embedded_entries() -> None:
    gate, _ = _mixed_gate()
    rankings = rank_recommendations(gate)
    entries = {
        entry.checksum: entry
        for game in rankings.games
        for entry in game.entries
    }
    assert [item.rank for item in rankings.top_confidence.placements] == [1, 2, 3]
    assert [item.rank for item in rankings.best_value.placements] == [1, 2, 3]
    for placement in rankings.top_confidence.placements:
        assert entries[placement.ranking_entry_checksum].top_confidence_rank == (
            placement.rank
        )
    for placement in rankings.best_value.placements:
        assert entries[placement.ranking_entry_checksum].best_value_rank == (
            placement.rank
        )


def test_sorting_does_not_depend_on_upstream_row_order() -> None:
    gate, _ = _mixed_gate()
    reversed_game = replace(
        gate.games[0],
        recommendations=tuple(reversed(gate.games[0].recommendations)),
    )
    reversed_gate = replace(gate, games=(reversed_game,))
    first = rank_recommendations(gate)
    second = rank_recommendations(reversed_gate)
    assert _placement_order(first, RankingBoardType.TOP_CONFIDENCE) == _placement_order(
        second,
        RankingBoardType.TOP_CONFIDENCE,
    )
    assert _placement_order(first, RankingBoardType.BEST_VALUE) == _placement_order(
        second,
        RankingBoardType.BEST_VALUE,
    )


def test_policy_prohibits_quotas_promotions_and_composite_scores() -> None:
    with pytest.raises(RankingsPolicyError, match="quota"):
        RankingsPolicyV1(maximum_ranked_candidates=5)
    with pytest.raises(RankingsPolicyError, match="promotion"):
        RankingsPolicyV1(allow_decision_promotion=True)
    with pytest.raises(RankingsPolicyError, match="composite"):
        RankingsPolicyV1(composite_score_enabled=True)


def test_pass_and_avoid_only_gate_produces_empty_boards() -> None:
    gate, labels = _mixed_gate()
    selected = tuple(
        item
        for item in gate.games[0].recommendations
        if item.decision in {RecommendationDecision.PASS, RecommendationDecision.AVOID}
    )
    gate = replace(gate, games=(replace(gate.games[0], recommendations=selected),))
    rankings = rank_recommendations(gate)
    assert rankings.top_confidence.placements == ()
    assert rankings.best_value.placements == ()
    assert rankings.eligible_count == 0
    assert rankings.unranked_count == 2
    assert {
        entry.upstream_recommendation_checksum
        for entry in rankings.games[0].entries
    } == {labels["pass"], labels["avoid"]}


def test_zero_recommendation_game_remains_represented() -> None:
    value_engine = _base_value_engine()
    empty_game = replace(
        value_engine.games[0],
        values=(),
        warnings=(ValueGameWarning.NO_EVALUABLE_MARKETS,),
    )
    gate = evaluate_recommendation_gate(replace(value_engine, games=(empty_game,)))
    rankings = rank_recommendations(gate)
    assert len(rankings.games) == 1
    assert rankings.games[0].entries == ()
    assert rankings.games[0].warnings == (
        RankingGameWarning.NO_RECOMMENDATION_ROWS,
    )
    assert rankings.top_confidence.placements == ()
    assert rankings.best_value.placements == ()


def test_zero_game_slate_is_deterministic() -> None:
    gate, _ = _mixed_gate()
    gate = replace(gate, games=())
    first = rank_recommendations(gate)
    second = rank_recommendations(gate)
    assert first.games == ()
    assert first.entry_count == 0
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()


def test_ranked_at_cannot_precede_recommendation_gate() -> None:
    gate, _ = _mixed_gate()
    with pytest.raises(RankingsContractError, match="cannot precede"):
        rank_recommendations(
            gate,
            ranked_at=gate.evaluated_at - timedelta(seconds=1),
        )


def test_rankings_is_deterministic() -> None:
    gate, _ = _mixed_gate()
    first = rank_recommendations(gate)
    second = rank_recommendations(gate)
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
