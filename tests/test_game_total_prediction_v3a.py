from __future__ import annotations

import pytest

from app.predictions.game_total import (
    GAME_TOTAL_CALCULATION_VERSION,
    GAME_TOTAL_PREDICTION_CONTRACT_VERSION,
    GameTotalPredictionV1,
    build_game_total_prediction,
    full_game_total_runs_distribution,
)
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    ModelRolloutState,
    MultiMarketContractError,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    PredictionTargetV1,
    capability_for_family,
)
from app.predictions.production import PREDICTIONS_CONTRACT_VERSION
from app.predictions.run_line import RUN_LINE_PREDICTION_CONTRACT_VERSION
from tests.test_run_line_prediction_v2a import _fake_game_prediction, _run_distribution


def test_v3a_preserves_v1_moneyline_and_v2_run_line_contracts() -> None:
    assert PREDICTIONS_CONTRACT_VERSION == "DSE_MLB_ML_PREDICTIONS_V1"
    assert RUN_LINE_PREDICTION_CONTRACT_VERSION == "DSE_MLB_RUN_LINE_PREDICTION_V1"


def test_v3_game_total_capability_remains_reference_and_ineligible() -> None:
    capability = capability_for_family(PredictionMarketFamily.GAME_TOTAL)
    assert capability.roadmap_version == 3
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_full_game_total_distribution_preserves_probability_mass() -> None:
    source = _run_distribution()
    total = full_game_total_runs_distribution(source)

    assert total.kind is PredictionDistributionKind.FULL_GAME_TOTAL_RUNS
    assert total.outcomes[0].value == 0
    assert total.outcomes[-1].value == 120
    assert sum(item.probability for item in total.outcomes) + total.unresolved_probability == pytest.approx(1.0)
    assert total.unresolved_probability == pytest.approx(source.approximation_tail_bound)
    assert len(total.checksum) == 64


def test_game_total_prediction_is_market_independent_and_projects_lines_later() -> None:
    prediction = build_game_total_prediction(_fake_game_prediction(home=4.6, away=4.1))

    assert prediction.contract_version == GAME_TOTAL_PREDICTION_CONTRACT_VERSION
    assert prediction.calculation_version == GAME_TOTAL_CALCULATION_VERSION
    assert prediction.rollout_state is ModelRolloutState.REFERENCE
    assert prediction.recommendation_eligible is False
    assert prediction.away_team_id == "SF"
    assert prediction.home_team_id == "LAD"
    assert prediction.expected_total_runs == pytest.approx(8.7)
    assert prediction.target == PredictionTargetV1(
        source_game_id="123456",
        family=PredictionMarketFamily.GAME_TOTAL,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )

    payload = prediction.as_dict()
    forbidden = {"bookmaker", "sportsbook", "odds", "price", "best_price", "market_price", "total_line"}
    assert forbidden.isdisjoint(payload)
    assert len(prediction.checksum) == 64

    low = prediction.project(7.5)
    standard = prediction.project(8.5)
    high = prediction.project(9.5)
    assert standard.push_probability == 0.0
    assert low.over_probability > standard.over_probability > high.over_probability
    assert standard.source_distribution_checksum == prediction.distribution_checksum
    assert (
        standard.over_probability
        + standard.under_probability
        + standard.push_probability
        + standard.unresolved_probability
        == pytest.approx(1.0)
    )


def test_integer_game_total_retains_push_probability() -> None:
    prediction = build_game_total_prediction(_fake_game_prediction())
    projection = prediction.project(9.0)
    total_nine = next(
        item.probability for item in prediction.total_runs_distribution.outcomes if item.value == 9
    )
    assert projection.push_probability == pytest.approx(total_nine)
    assert projection.push_probability > 0.0


def test_game_total_contract_rejects_wrong_distribution_kind_and_team_identity() -> None:
    wrong = DiscreteDistributionV1(
        kind=PredictionDistributionKind.FULL_GAME_RUN_MARGIN,
        outcomes=(DiscreteOutcomeProbabilityV1(0, 1.0),),
    )
    target = PredictionTargetV1(
        source_game_id="123456",
        family=PredictionMarketFamily.GAME_TOTAL,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )
    with pytest.raises(MultiMarketContractError, match="total-runs"):
        GameTotalPredictionV1(
            source_game_id="123456",
            away_team_id="SF",
            home_team_id="LAD",
            target=target,
            rollout_state=ModelRolloutState.REFERENCE,
            recommendation_eligible=False,
            upstream_game_prediction_checksum="a" * 64,
            upstream_model_manifest_checksum="b" * 64,
            model_id="reference",
            model_version="1",
            expected_total_runs=8.0,
            total_runs_distribution=wrong,
        )
    with pytest.raises(MultiMarketContractError, match="team identity"):
        GameTotalPredictionV1(
            source_game_id="123456",
            away_team_id="LAD",
            home_team_id="LAD",
            target=target,
            rollout_state=ModelRolloutState.REFERENCE,
            recommendation_eligible=False,
            upstream_game_prediction_checksum="a" * 64,
            upstream_model_manifest_checksum="b" * 64,
            model_id="reference",
            model_version="1",
            expected_total_runs=8.0,
            total_runs_distribution=full_game_total_runs_distribution(_run_distribution()),
        )
