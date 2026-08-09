from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from app.predictions.contracts import GamePredictionV1
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
from app.predictions.probability import build_run_distribution
from app.predictions.run_line import (
    RUN_LINE_CALCULATION_VERSION,
    RUN_LINE_PREDICTION_CONTRACT_VERSION,
    RunLinePredictionV1,
    build_run_line_prediction,
    full_game_run_margin_distribution,
)


def _run_distribution(*, home: float = 4.6, away: float = 4.1):
    return build_run_distribution(
        home_run_rate=home,
        away_run_rate=away,
        maximum_run_support=60,
    )


def _fake_game_prediction(*, home: float = 4.6, away: float = 4.1) -> GamePredictionV1:
    distribution = _run_distribution(home=home, away=away)
    return cast(
        GamePredictionV1,
        SimpleNamespace(
            source_game_id="123456",
            checksum="a" * 64,
            model_manifest_checksum="b" * 64,
            model_id="dse_mlb_reference_heuristic_poisson",
            model_version="1.0.0",
            recommendation_eligible=False,
            distribution=distribution,
            expected_home_runs=home,
            expected_away_runs=away,
        ),
    )


def test_v2_run_line_capability_remains_reference_and_ineligible() -> None:
    capability = capability_for_family(PredictionMarketFamily.RUN_LINE)
    assert capability.roadmap_version == 2
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_full_game_run_margin_distribution_preserves_probability_mass() -> None:
    source = _run_distribution()
    margin = full_game_run_margin_distribution(source)

    assert margin.kind is PredictionDistributionKind.FULL_GAME_RUN_MARGIN
    assert margin.outcomes[0].value == -60
    assert margin.outcomes[-1].value == 60
    assert sum(item.probability for item in margin.outcomes) + margin.unresolved_probability == pytest.approx(1.0)
    assert margin.unresolved_probability == pytest.approx(source.approximation_tail_bound)
    assert len(margin.checksum) == 64


def test_equal_run_rates_produce_symmetric_margin_distribution() -> None:
    margin = full_game_run_margin_distribution(_run_distribution(home=4.25, away=4.25))
    probabilities = {item.value: item.probability for item in margin.outcomes}
    for value in range(0, 15):
        assert probabilities[value] == pytest.approx(probabilities[-value], abs=1e-15)


def test_run_line_prediction_is_market_independent_and_projects_lines_later() -> None:
    prediction = build_run_line_prediction(_fake_game_prediction())

    assert prediction.contract_version == RUN_LINE_PREDICTION_CONTRACT_VERSION
    assert prediction.calculation_version == RUN_LINE_CALCULATION_VERSION
    assert prediction.rollout_state is ModelRolloutState.REFERENCE
    assert prediction.recommendation_eligible is False
    assert prediction.expected_home_margin == pytest.approx(0.5)
    assert prediction.target == PredictionTargetV1(
        source_game_id="123456",
        family=PredictionMarketFamily.RUN_LINE,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )

    payload = prediction.as_dict()
    forbidden = {"bookmaker", "sportsbook", "odds", "price", "best_price", "market_price", "home_spread"}
    assert forbidden.isdisjoint(payload)
    assert len(prediction.checksum) == 64

    favorite = prediction.project(-1.5)
    underdog = prediction.project(1.5)
    assert favorite.push_probability == 0.0
    assert underdog.push_probability == 0.0
    assert underdog.home_cover_probability > favorite.home_cover_probability
    assert (
        favorite.home_cover_probability
        + favorite.away_cover_probability
        + favorite.push_probability
        + favorite.unresolved_probability
        == pytest.approx(1.0)
    )
    assert favorite.source_distribution_checksum == prediction.distribution_checksum


def test_integer_run_line_retains_push_probability() -> None:
    prediction = build_run_line_prediction(_fake_game_prediction())
    projection = prediction.project(-1.0)
    margin_one = next(
        item.probability for item in prediction.run_margin_distribution.outcomes if item.value == 1
    )
    assert projection.push_probability == pytest.approx(margin_one)
    assert projection.push_probability > 0.0


def test_run_line_contract_rejects_wrong_distribution_kind() -> None:
    wrong = DiscreteDistributionV1(
        kind=PredictionDistributionKind.TEAM_RUNS,
        outcomes=(DiscreteOutcomeProbabilityV1(0, 1.0),),
    )
    target = PredictionTargetV1(
        source_game_id="123456",
        family=PredictionMarketFamily.RUN_LINE,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )
    with pytest.raises(MultiMarketContractError, match="run-margin"):
        RunLinePredictionV1(
            source_game_id="123456",
            target=target,
            rollout_state=ModelRolloutState.REFERENCE,
            recommendation_eligible=False,
            upstream_game_prediction_checksum="a" * 64,
            upstream_model_manifest_checksum="b" * 64,
            model_id="reference",
            model_version="1",
            expected_home_margin=0.0,
            run_margin_distribution=wrong,
        )
