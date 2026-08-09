from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.predictions.market_foundation import PredictionMarketFamily
from app.rankings.first_five import (
    FirstFiveRankingsError,
    build_first_five_reference_rankings,
)
from app.recommendation_gate.first_five import (
    FIRST_FIVE_POLICY_PENDING_REASON,
    FIRST_FIVE_REFERENCE_GATE_REASON,
    FirstFiveGateError,
    evaluate_first_five_reference_gate,
)
from app.value_engine.first_five import (
    FIRST_FIVE_BINDING_PENDING_REASON,
    FIRST_FIVE_REFERENCE_REASON,
    evaluate_first_five_value,
)
from tests.test_first_five_value_v4b import _normalized_odds, _predictions


def _value():
    _, moneyline, run_line, total = _predictions()
    return evaluate_first_five_value(
        moneyline,
        run_line,
        total,
        _normalized_odds(),
    )


def test_v4c_gate_retains_every_value_row_as_pass() -> None:
    value = _value()
    gate = evaluate_first_five_reference_gate(value)

    assert len(gate.outcomes) == len(value.outcomes) == 6
    assert gate.recommendation_count == 0
    assert all(item.decision == "pass" for item in gate.outcomes)
    assert {item.market_family for item in gate.outcomes} == {
        PredictionMarketFamily.FIRST_FIVE_MONEYLINE.value,
        PredictionMarketFamily.FIRST_FIVE_RUN_LINE.value,
        PredictionMarketFamily.FIRST_FIVE_TOTAL.value,
    }
    for item in gate.outcomes:
        assert FIRST_FIVE_REFERENCE_GATE_REASON in item.reason_codes
        assert FIRST_FIVE_POLICY_PENDING_REASON in item.reason_codes
        assert FIRST_FIVE_REFERENCE_REASON in item.reason_codes
        assert FIRST_FIVE_BINDING_PENDING_REASON in item.reason_codes


def test_v4c_rankings_retain_every_gate_row_without_rank() -> None:
    gate = evaluate_first_five_reference_gate(_value())
    rankings = build_first_five_reference_rankings((gate,))

    assert rankings.recommendation_count == 0
    assert len(rankings.games) == 1
    assert len(rankings.games[0].entries) == len(gate.outcomes) == 6
    assert rankings.games[0].upstream_gate_game_checksum == gate.checksum
    assert all(item.decision == "pass" for item in rankings.games[0].entries)
    assert all(item.rank_eligible is False for item in rankings.games[0].entries)
    assert all(item.recommendation_rank is None for item in rankings.games[0].entries)


def test_v4c_reference_gate_rejects_manual_recommendation_promotion() -> None:
    gate = evaluate_first_five_reference_gate(_value())
    with pytest.raises(FirstFiveGateError, match="only emit PASS"):
        replace(gate.outcomes[0], decision="recommend")


def test_v4c_reference_rankings_reject_manual_rank_promotion() -> None:
    gate = evaluate_first_five_reference_gate(_value())
    rankings = build_first_five_reference_rankings((gate,))
    entry = rankings.games[0].entries[0]

    with pytest.raises(FirstFiveRankingsError, match="cannot receive"):
        replace(entry, rank_eligible=True, recommendation_rank=1)


def test_v4c_gate_and_rankings_do_not_copy_value_metrics() -> None:
    gate = evaluate_first_five_reference_gate(_value())
    rankings = build_first_five_reference_rankings((gate,))
    serialized = json.dumps(
        {"gate": gate.as_dict(), "rankings": rankings.as_dict()},
        sort_keys=True,
    )

    assert "expected_value_per_unit" not in serialized
    assert "expected_roi_percent" not in serialized
    assert "no_vig_probability" not in serialized
    assert "best_price_books" not in serialized
    assert "recommendation_rank" in serialized
    assert '"recommendation_rank": null' in serialized


def test_v4c_lineage_is_exact_value_to_gate_to_rankings() -> None:
    value = _value()
    gate = evaluate_first_five_reference_gate(value)
    rankings = build_first_five_reference_rankings((gate,))

    by_value_checksum = {item.checksum: item for item in value.outcomes}
    assert set(item.source_value_checksum for item in gate.outcomes) == set(by_value_checksum)
    assert gate.source_value_game_checksum == value.checksum

    by_gate_checksum = {item.checksum: item for item in gate.outcomes}
    entries = rankings.games[0].entries
    assert set(item.upstream_gate_outcome_checksum for item in entries) == set(by_gate_checksum)
