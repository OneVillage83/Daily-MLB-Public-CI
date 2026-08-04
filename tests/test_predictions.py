from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.daily_slate.contracts import canonical_sha256
from app.data_quality.contracts import DataQualityDisposition
from app.model_feature_set.builder import build_model_feature_set
from app.model_feature_set.contracts import ModelFeatureSetV1
from app.model_feature_set.schema import FEATURE_INDEX_V1, MODEL_FEATURE_NAMES_V1
from app.predictions.contracts import (
    ModelCalibrationStatus,
    ModelDeploymentStatus,
    PredictionFeatureTermV1,
    PredictionInputState,
    PredictionModelManifestV1,
    PredictionsContractError,
)
from app.predictions.probability import (
    home_spread_projection,
    total_line_projection,
)
from app.predictions.runtime import (
    REFERENCE_HEURISTIC_POISSON_MANIFEST_CHECKSUM_V1,
    REFERENCE_HEURISTIC_POISSON_V1,
    predict_model_feature_set,
)
from tests.test_matchup_packet import _zero_game_chain
from tests.test_model_feature_set import _packet_from_chain


def _feature_set(
    *,
    disposition: DataQualityDisposition = DataQualityDisposition.READY,
) -> ModelFeatureSetV1:
    return build_model_feature_set(_packet_from_chain(disposition=disposition))


def _with_feature(
    feature_set: ModelFeatureSetV1,
    feature_name: str,
    value: float | None,
) -> ModelFeatureSetV1:
    game = feature_set.games[0]
    values = list(game.feature_values)
    values[FEATURE_INDEX_V1[feature_name]] = value
    updated_game = replace(game, feature_values=tuple(values))
    return replace(feature_set, games=(updated_game,))


def test_reference_manifest_is_frozen_and_recommendation_ineligible() -> None:
    manifest = REFERENCE_HEURISTIC_POISSON_V1
    assert manifest.checksum == REFERENCE_HEURISTIC_POISSON_MANIFEST_CHECKSUM_V1
    assert manifest.deployment_status is ModelDeploymentStatus.REFERENCE_ONLY
    assert (
        manifest.calibration_status
        is ModelCalibrationStatus.UNCALIBRATED_REFERENCE
    )
    assert manifest.recommendation_eligible is False
    assert manifest.training_dataset_checksum is None
    assert manifest.parameter_origin == (
        "transparent_expert_heuristic_not_empirically_fitted"
    )


def test_zero_game_feature_set_builds_deterministic_empty_predictions() -> None:
    slate, state, bia, odds_weather, quality = _zero_game_chain()
    from app.matchup_packet.assembly import assemble_matchup_packet

    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=odds_weather,
        data_quality=quality,
    )
    feature_set = build_model_feature_set(packet)
    first = predict_model_feature_set(feature_set)
    second = predict_model_feature_set(feature_set)
    assert first.games == ()
    assert first.upstream_model_feature_set_checksum == feature_set.checksum
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()


def test_every_game_is_predicted_even_when_data_quality_is_insufficient() -> None:
    feature_set = _feature_set(
        disposition=DataQualityDisposition.INSUFFICIENT
    )
    predictions = predict_model_feature_set(feature_set)
    assert len(predictions.games) == len(feature_set.games) == 1
    game = predictions.games[0]
    assert game.quality_disposition is DataQualityDisposition.INSUFFICIENT
    assert game.recommendation_eligible is False
    assert game.upstream_model_feature_game_checksum == feature_set.games[0].checksum


def test_reference_prediction_is_prior_only_when_all_model_terms_are_missing() -> None:
    predictions = predict_model_feature_set(_feature_set())
    game = predictions.games[0]
    assert game.input_state is PredictionInputState.PRIOR_ONLY
    assert game.imputed_feature_names == game.used_feature_names
    assert len(game.used_feature_names) == len(
        REFERENCE_HEURISTIC_POISSON_V1.terms
    )
    assert game.expected_home_runs == pytest.approx(4.55)
    assert game.expected_away_runs == pytest.approx(4.35)
    assert game.expected_total_runs == pytest.approx(8.90)


def test_moneyline_probabilities_and_distribution_are_valid() -> None:
    game = predict_model_feature_set(_feature_set()).games[0]
    assert game.home_win_probability + game.away_win_probability == pytest.approx(
        1.0
    )
    assert 0.0 < game.tied_after_regulation_probability < 1.0
    distribution = game.distribution
    assert sum(distribution.home_run_probabilities) + (
        distribution.home_tail_probability
    ) == pytest.approx(1.0)
    assert sum(distribution.away_run_probabilities) + (
        distribution.away_tail_probability
    ) == pytest.approx(1.0)
    assert distribution.approximation_tail_bound < 1e-12


def test_home_offense_feature_changes_home_rate_without_market_input() -> None:
    base = _feature_set()
    stronger_home = _with_feature(
        base,
        "home.lineup.season_to_date.hitting.ops",
        0.820,
    )
    baseline_prediction = predict_model_feature_set(base).games[0]
    stronger_prediction = predict_model_feature_set(stronger_home).games[0]
    assert stronger_prediction.expected_home_runs > (
        baseline_prediction.expected_home_runs
    )
    assert stronger_prediction.expected_away_runs == pytest.approx(
        baseline_prediction.expected_away_runs
    )
    assert stronger_prediction.home_win_probability > (
        baseline_prediction.home_win_probability
    )


def test_market_reference_changes_lineage_but_not_model_output() -> None:
    feature_set = _feature_set()
    original_game = feature_set.games[0]
    changed_market_context = {
        "markets": [
            {
                "bookmaker_count": 2,
                "market_key": "h2h",
            }
        ]
    }
    changed_market_checksum = canonical_sha256(changed_market_context)
    changed_game = replace(
        original_game,
        market_context=changed_market_context,
        market_reference_checksum=changed_market_checksum,
    )
    changed_set = replace(feature_set, games=(changed_game,))
    original = predict_model_feature_set(feature_set).games[0]
    changed = predict_model_feature_set(changed_set).games[0]
    assert changed.market_reference_checksum == changed_market_checksum
    assert changed.model_input_checksum == original.model_input_checksum
    assert changed.expected_home_runs == original.expected_home_runs
    assert changed.expected_away_runs == original.expected_away_runs
    assert changed.home_win_probability == original.home_win_probability


def test_total_and_run_line_helpers_support_arbitrary_lines() -> None:
    prediction = predict_model_feature_set(_feature_set()).games[0]
    total = total_line_projection(prediction.distribution, 8.5)
    assert (
        total.over_probability
        + total.under_probability
        + total.push_probability
    ) == pytest.approx(1.0)
    assert total.push_probability == 0.0
    integer_total = total_line_projection(prediction.distribution, 9.0)
    assert integer_total.push_probability > 0.0

    spread = home_spread_projection(prediction.distribution, -1.5)
    assert (
        spread.home_cover_probability
        + spread.away_cover_probability
        + spread.push_probability
    ) == pytest.approx(1.0)
    assert spread.push_probability == 0.0
    integer_spread = home_spread_projection(prediction.distribution, -1.0)
    assert integer_spread.push_probability > 0.0


def test_predictions_observed_at_cannot_precede_feature_set() -> None:
    feature_set = _feature_set()
    with pytest.raises(PredictionsContractError, match="cannot precede"):
        predict_model_feature_set(
            feature_set,
            observed_at=feature_set.observed_at - timedelta(seconds=1),
        )


def test_manifest_rejects_recommendation_eligible_uncalibrated_model() -> None:
    with pytest.raises(PredictionsContractError, match="production and calibrated"):
        PredictionModelManifestV1(
            model_id="bad",
            model_version="1",
            model_kind="independent_poisson_log_rate",
            deployment_status=ModelDeploymentStatus.EVALUATION,
            calibration_status=ModelCalibrationStatus.UNCALIBRATED,
            recommendation_eligible=True,
            parameter_origin="test",
            home_log_rate_intercept=1.0,
            away_log_rate_intercept=1.0,
            minimum_run_rate=1.0,
            maximum_run_rate=10.0,
            terms=(),
            tail_tolerance=1e-12,
            maximum_run_support=60,
        )


def test_manifest_rejects_feature_outside_frozen_schema() -> None:
    with pytest.raises(PredictionsContractError, match="not in ModelFeatureSet"):
        PredictionFeatureTermV1(
            feature_name="sportsbook.consensus_probability",
            center=0.5,
            scale=0.1,
            missing_value=0.5,
            home_coefficient=1.0,
            away_coefficient=0.0,
        )


def test_used_features_are_all_from_frozen_model_feature_schema() -> None:
    prediction = predict_model_feature_set(_feature_set()).games[0]
    assert set(prediction.used_feature_names).issubset(
        set(MODEL_FEATURE_NAMES_V1)
    )
    assert not any(
        token in name
        for name in prediction.used_feature_names
        for token in (
            "odds",
            "bookmaker",
            "implied_probability",
            "no_vig",
            "consensus_probability",
            "moneyline",
        )
    )
