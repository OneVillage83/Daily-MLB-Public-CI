from __future__ import annotations

import json

import pytest

from app.predictions.contracts import RunDistributionV1
from app.predictions.first_five import (
    FIRST_FIVE_MODEL_SCOPE,
    FirstFiveScoringPredictionV1,
    build_first_five_moneyline_prediction,
    build_first_five_run_line_prediction,
    build_first_five_total_prediction,
    first_five_run_margin_distribution,
    first_five_total_runs_distribution,
)
from app.predictions.game_total import GAME_TOTAL_PREDICTION_CONTRACT_VERSION
from app.predictions.market_foundation import (
    ModelRolloutState,
    MultiMarketContractError,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    capabilities_for_version,
)
from app.predictions.production import PREDICTIONS_CONTRACT_VERSION
from app.predictions.run_line import RUN_LINE_PREDICTION_CONTRACT_VERSION


def _distribution() -> RunDistributionV1:
    return RunDistributionV1(
        home_run_rate=0.8,
        away_run_rate=0.7,
        maximum_run_support=2,
        home_run_probabilities=(0.4, 0.4, 0.2),
        away_run_probabilities=(0.5, 0.3, 0.2),
        home_tail_probability=0.0,
        away_tail_probability=0.0,
        retained_joint_probability=1.0,
        approximation_tail_bound=0.0,
    )


def _source(**overrides: object) -> FirstFiveScoringPredictionV1:
    values: dict[str, object] = {
        "source_game_id": "2026-08-09_NYY_BOS_1",
        "away_team_id": "NYY",
        "home_team_id": "BOS",
        "upstream_model_feature_checksum": "1" * 64,
        "model_manifest_checksum": "2" * 64,
        "model_id": "first-five-scoring",
        "model_version": "evaluation-v1",
        "distribution": _distribution(),
    }
    values.update(overrides)
    return FirstFiveScoringPredictionV1(**values)  # type: ignore[arg-type]


def test_v4a_does_not_change_v1_v2_v3_prediction_contracts() -> None:
    assert PREDICTIONS_CONTRACT_VERSION == "DSE_MLB_ML_PREDICTIONS_V1"
    assert RUN_LINE_PREDICTION_CONTRACT_VERSION == "DSE_MLB_RUN_LINE_PREDICTION_V1"
    assert GAME_TOTAL_PREDICTION_CONTRACT_VERSION == "DSE_MLB_GAME_TOTAL_PREDICTION_V1"


def test_v4_capabilities_are_three_first_five_reference_families() -> None:
    capabilities = capabilities_for_version(4)
    assert {item.family for item in capabilities} == {
        PredictionMarketFamily.FIRST_FIVE_MONEYLINE,
        PredictionMarketFamily.FIRST_FIVE_RUN_LINE,
        PredictionMarketFamily.FIRST_FIVE_TOTAL,
    }
    assert all(item.period is PredictionPeriod.FIRST_FIVE for item in capabilities)
    assert all(item.rollout_state is ModelRolloutState.REFERENCE for item in capabilities)
    assert all(item.recommendation_eligible is False for item in capabilities)


def test_first_five_scoring_source_requires_independent_reference_scope() -> None:
    source = _source()
    assert source.model_scope == FIRST_FIVE_MODEL_SCOPE
    assert source.rollout_state is ModelRolloutState.REFERENCE
    assert source.recommendation_eligible is False

    with pytest.raises(MultiMarketContractError, match="independent first-five model scope"):
        _source(model_scope="full_game")
    with pytest.raises(MultiMarketContractError, match="reference-only"):
        _source(recommendation_eligible=True)


def test_first_five_distributions_preserve_mass_and_use_first_five_kinds() -> None:
    margin = first_five_run_margin_distribution(_distribution())
    total = first_five_total_runs_distribution(_distribution())

    assert margin.kind is PredictionDistributionKind.FIRST_FIVE_RUN_MARGIN
    assert total.kind is PredictionDistributionKind.FIRST_FIVE_TOTAL_RUNS
    assert margin.outcomes[0].value == -2
    assert margin.outcomes[-1].value == 2
    assert total.outcomes[0].value == 0
    assert total.outcomes[-1].value == 4
    assert sum(item.probability for item in margin.outcomes) == pytest.approx(1.0)
    assert sum(item.probability for item in total.outcomes) == pytest.approx(1.0)
    assert margin.unresolved_probability == 0.0
    assert total.unresolved_probability == 0.0


def test_first_five_moneyline_keeps_tie_probability_explicit() -> None:
    prediction = build_first_five_moneyline_prediction(_source())

    assert prediction.target.family is PredictionMarketFamily.FIRST_FIVE_MONEYLINE
    assert prediction.home_win_probability == pytest.approx(0.36)
    assert prediction.away_win_probability == pytest.approx(0.28)
    assert prediction.tie_probability == pytest.approx(0.36)
    assert prediction.unresolved_probability == 0.0
    assert prediction.recommendation_eligible is False


def test_first_five_run_line_and_total_project_lines_without_storing_market_prices() -> None:
    source = _source()
    run_line = build_first_five_run_line_prediction(source)
    total = build_first_five_total_prediction(source)

    assert run_line.upstream_first_five_scoring_checksum == source.checksum
    assert total.upstream_first_five_scoring_checksum == source.checksum
    assert run_line.target.family is PredictionMarketFamily.FIRST_FIVE_RUN_LINE
    assert total.target.family is PredictionMarketFamily.FIRST_FIVE_TOTAL

    pickem = run_line.project(0.0)
    assert pickem.push_probability == pytest.approx(0.36)
    half_run = run_line.project(-0.5)
    assert half_run.push_probability == 0.0

    integer_total = total.project(2.0)
    assert integer_total.push_probability > 0.0
    half_total = total.project(2.5)
    assert half_total.push_probability == 0.0


def test_v4a_serialization_has_no_full_game_prediction_or_market_context() -> None:
    source = _source()
    documents = (
        source.as_dict(),
        build_first_five_moneyline_prediction(source).as_dict(),
        build_first_five_run_line_prediction(source).as_dict(),
        build_first_five_total_prediction(source).as_dict(),
    )
    serialized = json.dumps(documents, sort_keys=True)

    assert "upstream_game_prediction_checksum" not in serialized
    assert '"period": "full_game"' not in serialized
    assert "bookmaker" not in serialized
    assert "odds" not in serialized
    assert "no_vig" not in serialized
    assert "expected_value" not in serialized
