from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.predictions.contracts import RunDistributionV1
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    capabilities_for_version,
)
from app.predictions.team_total import (
    TEAM_TOTAL_CALCULATION_VERSION,
    build_team_total_predictions,
    team_run_distribution,
)


def _distribution() -> RunDistributionV1:
    return RunDistributionV1(
        home_run_rate=4.6,
        away_run_rate=3.9,
        maximum_run_support=3,
        home_run_probabilities=(0.10, 0.20, 0.25, 0.25),
        away_run_probabilities=(0.15, 0.25, 0.25, 0.20),
        home_tail_probability=0.20,
        away_tail_probability=0.15,
        retained_joint_probability=0.68,
        approximation_tail_bound=0.32,
    )


def _game_prediction() -> SimpleNamespace:
    return SimpleNamespace(
        source_game_id="2026-08-09_NYY_BOS_1",
        away_team_id="NYY",
        home_team_id="BOS",
        expected_away_runs=3.9,
        expected_home_runs=4.6,
        distribution=_distribution(),
        checksum="1" * 64,
        model_manifest_checksum="2" * 64,
        model_id="full-game-scoring",
        model_version="evaluation-v1",
    )


def test_v6_capability_is_reference_team_runs() -> None:
    capabilities = capabilities_for_version(6)
    assert len(capabilities) == 1
    capability = capabilities[0]
    assert capability.family is PredictionMarketFamily.TEAM_TOTAL
    assert capability.period is PredictionPeriod.FULL_GAME
    assert capability.subject_kind is PredictionSubjectKind.TEAM
    assert capability.required_distribution_kind is PredictionDistributionKind.TEAM_RUNS
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_team_run_distribution_uses_each_team_marginal_and_own_tail() -> None:
    distribution = _distribution()
    home = team_run_distribution(distribution, is_home=True)
    away = team_run_distribution(distribution, is_home=False)

    assert home.kind is PredictionDistributionKind.TEAM_RUNS
    assert away.kind is PredictionDistributionKind.TEAM_RUNS
    assert [item.probability for item in home.outcomes] == list(
        distribution.home_run_probabilities
    )
    assert [item.probability for item in away.outcomes] == list(
        distribution.away_run_probabilities
    )
    assert home.unresolved_probability == distribution.home_tail_probability
    assert away.unresolved_probability == distribution.away_tail_probability
    assert home.unresolved_probability != distribution.approximation_tail_bound


def test_build_team_total_predictions_returns_away_then_home_subjects() -> None:
    away, home = build_team_total_predictions(_game_prediction())  # type: ignore[arg-type]

    assert (away.team_id, home.team_id) == ("NYY", "BOS")
    assert away.opponent_team_id == "BOS"
    assert home.opponent_team_id == "NYY"
    assert away.expected_team_runs == pytest.approx(3.9)
    assert home.expected_team_runs == pytest.approx(4.6)
    assert away.target.subject_id == "NYY"
    assert home.target.subject_id == "BOS"
    assert away.target.statistic == "runs"
    assert home.target.statistic == "runs"
    assert away.rollout_state is ModelRolloutState.REFERENCE
    assert home.rollout_state is ModelRolloutState.REFERENCE
    assert away.recommendation_eligible is False
    assert home.recommendation_eligible is False
    assert away.calculation_version == TEAM_TOTAL_CALCULATION_VERSION
    assert home.calculation_version == TEAM_TOTAL_CALCULATION_VERSION


def test_team_total_projection_preserves_integer_push_and_half_run_no_push() -> None:
    away, _ = build_team_total_predictions(_game_prediction())  # type: ignore[arg-type]

    integer_line = away.project(2.0)
    assert integer_line.push_probability == pytest.approx(0.25)
    assert integer_line.unresolved_probability == pytest.approx(0.15)

    half_line = away.project(2.5)
    assert half_line.push_probability == 0.0
    assert half_line.over_probability == pytest.approx(0.20)
    assert half_line.under_probability == pytest.approx(0.65)
    assert half_line.unresolved_probability == pytest.approx(0.15)


def test_v6_prediction_serialization_contains_no_market_price_context() -> None:
    predictions = build_team_total_predictions(_game_prediction())  # type: ignore[arg-type]
    serialized = json.dumps([prediction.as_dict() for prediction in predictions], sort_keys=True)

    assert "bookmaker" not in serialized
    assert '"odds"' not in serialized
    assert "no_vig" not in serialized
    assert "expected_value" not in serialized
    assert '"family": "team_total"' in serialized
    assert '"subject_kind": "team"' in serialized
