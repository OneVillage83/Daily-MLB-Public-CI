from __future__ import annotations

import pytest

from app.odds_weather.first_inning import normalize_first_inning_odds
from app.predictions.first_inning import build_nrfi_yrfi_prediction
from app.value_engine.nrfi_yrfi import (
    NRFI_YRFI_BINDING_PENDING_REASON,
    NRFI_YRFI_REFERENCE_REASON,
    NRFI_YRFI_TOTAL_LINE,
    NrfiYrfiValueError,
    evaluate_nrfi_yrfi_value,
)
from tests.test_first_inning_odds_v5o import _evidence
from tests.test_nrfi_yrfi_prediction_v5a import _source


def _prediction():
    return build_nrfi_yrfi_prediction(_source())


def _odds():
    return normalize_first_inning_odds(_evidence(), run_id="run_v5b")


def test_v5b_maps_under_to_nrfi_and_over_to_yrfi() -> None:
    prediction = _prediction()
    value = evaluate_nrfi_yrfi_value(prediction, _odds())

    assert value.recommendation_gate_input_count == 0
    assert len(value.outcomes) == 2
    assert {item.side for item in value.outcomes} == {"nrfi", "yrfi"}

    nrfi = next(item for item in value.outcomes if item.side == "nrfi")
    yrfi = next(item for item in value.outcomes if item.side == "yrfi")

    assert nrfi.sportsbook_outcome == "Under"
    assert yrfi.sportsbook_outcome == "Over"
    assert nrfi.total_line == NRFI_YRFI_TOTAL_LINE
    assert yrfi.total_line == NRFI_YRFI_TOTAL_LINE
    assert nrfi.model_win_probability == pytest.approx(prediction.nrfi_probability)
    assert yrfi.model_win_probability == pytest.approx(prediction.yrfi_probability)


def test_v5b_uses_best_multi_book_prices_and_no_vig() -> None:
    value = evaluate_nrfi_yrfi_value(_prediction(), _odds())
    nrfi = next(item for item in value.outcomes if item.side == "nrfi")
    yrfi = next(item for item in value.outcomes if item.side == "yrfi")

    assert nrfi.american_price == -105.0
    assert nrfi.best_price_books == ("draftkings",)
    assert yrfi.american_price == -110.0
    assert yrfi.best_price_books == ("fanduel",)
    assert nrfi.bookmaker_count == 2
    assert yrfi.bookmaker_count == 2
    assert nrfi.no_vig_probability is not None
    assert yrfi.no_vig_probability is not None
    assert nrfi.expected_roi_percent == pytest.approx(nrfi.expected_value_per_unit * 100.0)
    assert yrfi.expected_roi_percent == pytest.approx(yrfi.expected_value_per_unit * 100.0)


def test_v5b_preserves_reference_and_binding_blockers() -> None:
    value = evaluate_nrfi_yrfi_value(_prediction(), _odds())

    for item in value.outcomes:
        assert item.prediction_recommendation_eligible is False
        assert item.recommendation_gate_input_eligible is False
        assert NRFI_YRFI_REFERENCE_REASON in item.ineligibility_reasons
        assert NRFI_YRFI_BINDING_PENDING_REASON in item.ineligibility_reasons
        assert item.source_prediction_checksum == value.source_prediction_checksum
        assert item.source_normalized_odds_checksum == value.source_normalized_odds_checksum


def test_v5b_rejects_wrong_team_pair() -> None:
    odds = _odds()
    odds.summary["home_team_key"] = "NYY"

    with pytest.raises(NrfiYrfiValueError, match="teams do not match"):
        evaluate_nrfi_yrfi_value(_prediction(), odds)


def test_v5b_does_not_emit_recommendation_decisions() -> None:
    serialized = str(evaluate_nrfi_yrfi_value(_prediction(), _odds()).as_dict())

    assert "no_vig_probability" in serialized
    assert "expected_value_per_unit" in serialized
    assert "best_price_books" in serialized
    assert "BET" not in serialized
    assert "LEAN" not in serialized
    assert "AVOID" not in serialized
