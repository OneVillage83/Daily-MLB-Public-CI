from __future__ import annotations

import math

import pytest

from app.predictions.distribution_diagnostics import (
    DISTRIBUTION_LOG_PROBABILITY_FLOOR,
    DistributionDiagnosticsError,
    evaluate_full_game_distribution_diagnostics,
)
from app.predictions.training import ScoringTrainingDatasetV1
from tests.test_scoring_training_foundation import _dataset, _fit


def test_distribution_diagnostics_are_deterministic_and_bind_holdout_lineage() -> None:
    dataset = _dataset()
    training_result = _fit(dataset)
    first = evaluate_full_game_distribution_diagnostics(dataset, training_result)
    second = evaluate_full_game_distribution_diagnostics(dataset, training_result)

    assert first == second
    assert first.checksum == second.checksum
    assert len(first.checksum) == 64
    assert first.training_result_checksum == training_result.checksum
    assert first.validation_subset_checksum == training_result.validation_subset_checksum
    assert first.validation_game_count == training_result.evaluation.validation_row_count == 10
    assert first.log_probability_floor == DISTRIBUTION_LOG_PROBABILITY_FLOOR


def test_distribution_diagnostics_score_run_line_and_total_distributions_against_baseline() -> None:
    diagnostics = evaluate_full_game_distribution_diagnostics(_dataset(), _fit())

    metrics = (
        diagnostics.model_total_log_loss,
        diagnostics.baseline_total_log_loss,
        diagnostics.model_total_crps,
        diagnostics.baseline_total_crps,
        diagnostics.model_run_margin_log_loss,
        diagnostics.baseline_run_margin_log_loss,
        diagnostics.model_run_margin_crps,
        diagnostics.baseline_run_margin_crps,
    )
    assert all(math.isfinite(value) and value >= 0.0 for value in metrics)
    assert diagnostics.total_log_loss_delta == pytest.approx(
        diagnostics.model_total_log_loss - diagnostics.baseline_total_log_loss
    )
    assert diagnostics.total_crps_delta == pytest.approx(
        diagnostics.model_total_crps - diagnostics.baseline_total_crps
    )
    assert diagnostics.run_margin_log_loss_delta == pytest.approx(
        diagnostics.model_run_margin_log_loss - diagnostics.baseline_run_margin_log_loss
    )
    assert diagnostics.run_margin_crps_delta == pytest.approx(
        diagnostics.model_run_margin_crps - diagnostics.baseline_run_margin_crps
    )
    assert diagnostics.model_improves_total_log_loss is (diagnostics.total_log_loss_delta < 0.0)
    assert diagnostics.model_improves_total_crps is (diagnostics.total_crps_delta < 0.0)
    assert diagnostics.model_improves_run_margin_log_loss is (
        diagnostics.run_margin_log_loss_delta < 0.0
    )
    assert diagnostics.model_improves_run_margin_crps is (
        diagnostics.run_margin_crps_delta < 0.0
    )
    assert 0.0 <= diagnostics.maximum_model_unresolved_probability <= 1.0
    assert 0.0 <= diagnostics.maximum_baseline_unresolved_probability <= 1.0


def test_distribution_diagnostics_remain_market_independent_evidence() -> None:
    payload = evaluate_full_game_distribution_diagnostics(_dataset(), _fit()).as_dict()
    text = str(payload).casefold()

    assert "bookmaker" not in text
    assert "sportsbook" not in text
    assert "best_price" not in text
    assert "american_price" not in text
    assert "recommendation_eligible" not in text
    assert "promotion_threshold" not in text


def test_distribution_diagnostics_reject_dataset_or_validation_checksum_drift() -> None:
    dataset = _dataset()
    training_result = _fit(dataset)
    shortened = ScoringTrainingDatasetV1(rows=dataset.rows[:-1])
    with pytest.raises(DistributionDiagnosticsError, match="dataset checksum"):
        evaluate_full_game_distribution_diagnostics(shortened, training_result)
