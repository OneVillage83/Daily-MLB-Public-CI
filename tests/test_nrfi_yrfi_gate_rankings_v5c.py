from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.rankings.nrfi_yrfi import (
    NrfiYrfiRankingsError,
    build_nrfi_yrfi_reference_rankings,
)
from app.recommendation_gate.nrfi_yrfi import (
    NRFI_YRFI_POLICY_PENDING_REASON,
    NRFI_YRFI_REFERENCE_GATE_REASON,
    NrfiYrfiGateError,
    evaluate_nrfi_yrfi_reference_gate,
)
from app.value_engine.nrfi_yrfi import (
    NRFI_YRFI_BINDING_PENDING_REASON,
    NRFI_YRFI_REFERENCE_REASON,
    evaluate_nrfi_yrfi_value,
)
from tests.test_nrfi_yrfi_value_v5b import _odds, _prediction


def _value():
    return evaluate_nrfi_yrfi_value(_prediction(), _odds())


def test_v5c_gate_retains_nrfi_and_yrfi_as_pass() -> None:
    value = _value()
    gate = evaluate_nrfi_yrfi_reference_gate(value)

    assert len(value.outcomes) == len(gate.outcomes) == 2
    assert {item.side for item in gate.outcomes} == {"nrfi", "yrfi"}
    assert gate.recommendation_count == 0
    assert all(item.decision == "pass" for item in gate.outcomes)
    for item in gate.outcomes:
        assert NRFI_YRFI_REFERENCE_REASON in item.reason_codes
        assert NRFI_YRFI_BINDING_PENDING_REASON in item.reason_codes
        assert NRFI_YRFI_REFERENCE_GATE_REASON in item.reason_codes
        assert NRFI_YRFI_POLICY_PENDING_REASON in item.reason_codes


def test_v5c_rankings_retain_both_sides_without_rank() -> None:
    gate = evaluate_nrfi_yrfi_reference_gate(_value())
    rankings = build_nrfi_yrfi_reference_rankings((gate,))

    assert rankings.recommendation_count == 0
    assert len(rankings.games) == 1
    assert len(rankings.games[0].entries) == 2
    assert {item.side for item in rankings.games[0].entries} == {"nrfi", "yrfi"}
    assert all(item.rank_eligible is False for item in rankings.games[0].entries)
    assert all(item.recommendation_rank is None for item in rankings.games[0].entries)


def test_v5c_rejects_manual_gate_and_ranking_promotion() -> None:
    gate = evaluate_nrfi_yrfi_reference_gate(_value())
    with pytest.raises(NrfiYrfiGateError, match="only emit PASS"):
        replace(gate.outcomes[0], decision="recommend")

    rankings = build_nrfi_yrfi_reference_rankings((gate,))
    with pytest.raises(NrfiYrfiRankingsError, match="cannot receive a rank"):
        replace(
            rankings.games[0].entries[0],
            rank_eligible=True,
            recommendation_rank=1,
        )


def test_v5c_gate_and_rankings_do_not_copy_value_metrics() -> None:
    gate = evaluate_nrfi_yrfi_reference_gate(_value())
    rankings = build_nrfi_yrfi_reference_rankings((gate,))
    serialized = json.dumps(
        {"gate": gate.as_dict(), "rankings": rankings.as_dict()},
        sort_keys=True,
    )

    assert "expected_value_per_unit" not in serialized
    assert "no_vig_probability" not in serialized
    assert "best_price_books" not in serialized
    assert '"recommendation_rank": null' in serialized


def test_v5c_checksum_lineage_is_lossless() -> None:
    value = _value()
    gate = evaluate_nrfi_yrfi_reference_gate(value)
    rankings = build_nrfi_yrfi_reference_rankings((gate,))

    assert gate.source_value_game_checksum == value.checksum
    assert {item.source_value_checksum for item in gate.outcomes} == {
        item.checksum for item in value.outcomes
    }
    assert rankings.games[0].upstream_gate_game_checksum == gate.checksum
    assert {
        item.upstream_gate_outcome_checksum for item in rankings.games[0].entries
    } == {item.checksum for item in gate.outcomes}
