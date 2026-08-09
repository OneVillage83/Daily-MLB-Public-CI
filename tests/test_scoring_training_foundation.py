from __future__ import annotations

import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.predictions.contracts import ModelCalibrationStatus, ModelDeploymentStatus
from app.predictions.training import (
    FULL_GAME_SCORING_TRAINER_VERSION,
    HistoricalFeatureValueV1,
    HistoricalScoringRowV1,
    ScoringTrainingDatasetV1,
    ScoringTrainingError,
    ScoringTrainingResultV1,
    fit_empirical_scoring_model,
)

AWAY_OPS = "away.lineup.season_to_date.hitting.ops"
HOME_OPS = "home.lineup.season_to_date.hitting.ops"
FEATURES = (AWAY_OPS, HOME_OPS)
TRAINED_AT = datetime(2026, 8, 9, 18, 30, tzinfo=timezone.utc)


def _row(index: int, *, missing_home: bool = False) -> HistoricalScoringRowV1:
    away_ops = 0.660 + (index % 8) * 0.015
    home_ops = 0.675 + ((index * 3) % 8) * 0.015
    away_runs = 2 + (index % 3) + (1 if away_ops >= 0.720 else 0)
    home_runs = 2 + ((index * 2) % 4) + (1 if home_ops >= 0.735 else 0)
    return HistoricalScoringRowV1(
        source_game_id=f"synthetic-{index:03d}",
        game_date=date(2024, 1, 1) + timedelta(days=index),
        away_team_id="SF",
        home_team_id="LAD",
        away_runs=away_runs,
        home_runs=home_runs,
        features=(
            HistoricalFeatureValueV1(AWAY_OPS, away_ops),
            HistoricalFeatureValueV1(HOME_OPS, None if missing_home else home_ops),
        ),
    )


def _dataset() -> ScoringTrainingDatasetV1:
    return ScoringTrainingDatasetV1(
        rows=tuple(_row(index, missing_home=index in {7, 34}) for index in range(40))
    )


def _fit(dataset: ScoringTrainingDatasetV1 | None = None) -> ScoringTrainingResultV1:
    return fit_empirical_scoring_model(
        _dataset() if dataset is None else dataset,
        feature_names=FEATURES,
        train_through=date(2024, 1, 30),
        validation_start=date(2024, 1, 31),
        validation_end=date(2024, 2, 9),
        model_id="dse_mlb_empirical_full_game_scoring",
        model_version="0.1.0-evaluation",
        trained_at=TRAINED_AT,
    )


def test_training_dataset_is_chronological_unique_and_deterministic() -> None:
    first = _dataset()
    second = _dataset()
    assert first.checksum == second.checksum
    assert len(first.checksum) == 64
    assert first.rows[0].game_date == date(2024, 1, 1)
    assert first.rows[-1].game_date == date(2024, 2, 9)

    with pytest.raises(ScoringTrainingError, match="sorted chronologically"):
        ScoringTrainingDatasetV1(rows=(first.rows[1], first.rows[0]))
    with pytest.raises(ScoringTrainingError, match="duplicate source_game_id"):
        ScoringTrainingDatasetV1(rows=(first.rows[0], first.rows[0]))


def test_empirical_fit_uses_strict_temporal_holdout_and_is_deterministic() -> None:
    first = _fit()
    second = _fit()

    assert first.checksum == second.checksum
    assert first.manifest.checksum == second.manifest.checksum
    assert first.evaluation.checksum == second.evaluation.checksum
    assert first.train_through == date(2024, 1, 30)
    assert first.validation_start == date(2024, 1, 31)
    assert first.validation_end == date(2024, 2, 9)
    assert first.evaluation.training_row_count == 30
    assert first.evaluation.validation_row_count == 10
    assert first.evaluation.validation_start > first.train_through
    assert first.training_subset_checksum != first.validation_subset_checksum
    assert first.full_dataset_checksum == _dataset().checksum

    with pytest.raises(ScoringTrainingError, match="strictly after training"):
        fit_empirical_scoring_model(
            _dataset(),
            feature_names=FEATURES,
            train_through=date(2024, 1, 31),
            validation_start=date(2024, 1, 31),
            validation_end=date(2024, 2, 9),
            model_id="bad-split",
            model_version="1",
            trained_at=TRAINED_AT,
        )


def test_fitted_manifest_is_empirical_but_cannot_be_promoted_by_training_alone() -> None:
    result = _fit()
    manifest = result.manifest

    assert manifest.deployment_status is ModelDeploymentStatus.EVALUATION
    assert manifest.calibration_status is ModelCalibrationStatus.UNCALIBRATED
    assert manifest.recommendation_eligible is False
    assert manifest.parameter_origin == "empirical_temporal_poisson_fit"
    assert manifest.training_dataset_checksum == result.training_subset_checksum
    assert manifest.training_code_version == FULL_GAME_SCORING_TRAINER_VERSION
    assert manifest.trained_at == TRAINED_AT
    assert manifest.model_kind == "independent_poisson_log_rate"
    assert tuple(term.feature_name for term in manifest.terms) == FEATURES
    assert all(math.isfinite(term.home_coefficient) for term in manifest.terms)
    assert all(math.isfinite(term.away_coefficient) for term in manifest.terms)
    assert all(term.missing_value == pytest.approx(term.center) for term in manifest.terms)

    formally_promoted_manifest = replace(
        manifest,
        deployment_status=ModelDeploymentStatus.PRODUCTION,
        calibration_status=ModelCalibrationStatus.CALIBRATED,
        recommendation_eligible=True,
    )
    with pytest.raises(ScoringTrainingError, match="evaluation manifest"):
        replace(result, manifest=formally_promoted_manifest)


def test_holdout_evaluation_reports_model_and_naive_baseline_without_inventing_thresholds() -> None:
    evaluation = _fit().evaluation

    for value in (
        evaluation.model_poisson_nll_per_team,
        evaluation.baseline_poisson_nll_per_team,
        evaluation.home_runs_mae,
        evaluation.away_runs_mae,
        evaluation.total_runs_mae,
        evaluation.run_margin_mae,
        evaluation.moneyline_brier_score,
        evaluation.baseline_home_run_rate,
        evaluation.baseline_away_run_rate,
    ):
        assert math.isfinite(value)
        assert value >= 0.0
    assert 0 <= evaluation.decisive_validation_game_count <= evaluation.validation_row_count
    assert evaluation.holdout_nll_delta == pytest.approx(
        evaluation.model_poisson_nll_per_team - evaluation.baseline_poisson_nll_per_team
    )
    assert evaluation.model_improves_holdout_nll is (evaluation.holdout_nll_delta < 0.0)


def test_missing_training_and_holdout_feature_values_are_center_imputed() -> None:
    result = _fit()
    home_term = next(term for term in result.manifest.terms if term.feature_name == HOME_OPS)
    observed_training_home = [
        row.feature_map()[HOME_OPS]
        for row in _dataset().rows[:30]
        if row.feature_map()[HOME_OPS] is not None
    ]
    expected_center = sum(float(value) for value in observed_training_home if value is not None) / len(
        observed_training_home
    )
    assert home_term.center == pytest.approx(expected_center)
    assert home_term.missing_value == pytest.approx(expected_center)
    assert math.isfinite(result.evaluation.model_poisson_nll_per_team)
