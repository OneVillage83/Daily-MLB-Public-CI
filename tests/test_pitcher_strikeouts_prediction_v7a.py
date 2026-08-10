from __future__ import annotations

from dataclasses import replace

import pytest

from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    ModelRolloutState,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    capability_for_family,
)
from app.predictions.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_MODEL_SCOPE,
    PitcherStarterBindingState,
    PitcherStrikeoutPredictionError,
    build_pitcher_strikeout_prediction,
)


def _distribution() -> DiscreteDistributionV1:
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.PITCHER_STRIKEOUTS,
        outcomes=(
            DiscreteOutcomeProbabilityV1(3, 0.15),
            DiscreteOutcomeProbabilityV1(4, 0.20),
            DiscreteOutcomeProbabilityV1(5, 0.25),
            DiscreteOutcomeProbabilityV1(6, 0.20),
            DiscreteOutcomeProbabilityV1(7, 0.10),
        ),
        unresolved_probability=0.10,
    )


def _prediction(state: PitcherStarterBindingState = PitcherStarterBindingState.EXPECTED):
    return build_pitcher_strikeout_prediction(
        source_game_id="statcast:12345",
        pitcher_id="mlbam:543210",
        pitcher_name="Example Starter",
        team_id="NYY",
        opponent_team_id="BOS",
        starter_binding_state=state,
        starter_binding_checksum="c" * 64,
        source_model_input_checksum="a" * 64,
        model_manifest_checksum="b" * 64,
        model_id="DSE_PITCHER_K_REFERENCE_V1",
        model_version="1.0.0",
        expected_strikeouts=5.2,
        strikeout_distribution=_distribution(),
    )


def test_v7_capability_is_reference_pitcher_strikeout_count() -> None:
    capability = capability_for_family(PredictionMarketFamily.PITCHER_STRIKEOUTS)
    assert capability.roadmap_version == 7
    assert capability.period is PredictionPeriod.FULL_GAME
    assert capability.subject_kind is PredictionSubjectKind.PLAYER
    assert capability.required_distribution_kind is PredictionDistributionKind.PITCHER_STRIKEOUTS
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_prediction_is_starter_bound_market_blind_and_reference_only() -> None:
    prediction = _prediction()
    assert prediction.model_scope == PITCHER_STRIKEOUT_MODEL_SCOPE
    assert prediction.target.subject_id == prediction.pitcher_id
    assert prediction.target.statistic == "strikeouts"
    assert prediction.starter_binding_state is PitcherStarterBindingState.EXPECTED
    assert prediction.recommendation_eligible is False
    serialized = str(prediction.as_dict()).casefold()
    for forbidden in (
        "american_price",
        "bookmaker",
        "no_vig",
        "expected_value_per_unit",
        "recommendation_rank",
    ):
        assert forbidden not in serialized


def test_projection_preserves_integer_push_and_unresolved_tail() -> None:
    prediction = _prediction()
    integer = prediction.project(5.0)
    assert integer.under_probability == pytest.approx(0.35)
    assert integer.push_probability == pytest.approx(0.25)
    assert integer.over_probability == pytest.approx(0.30)
    assert integer.unresolved_probability == pytest.approx(0.10)

    half = prediction.project(5.5)
    assert half.under_probability == pytest.approx(0.60)
    assert half.push_probability == pytest.approx(0.0)
    assert half.over_probability == pytest.approx(0.30)
    assert half.unresolved_probability == pytest.approx(0.10)


def test_expected_and_confirmed_starter_states_are_retained() -> None:
    expected = _prediction(PitcherStarterBindingState.EXPECTED)
    confirmed = _prediction(PitcherStarterBindingState.CONFIRMED)
    assert expected.starter_binding_state is PitcherStarterBindingState.EXPECTED
    assert confirmed.starter_binding_state is PitcherStarterBindingState.CONFIRMED
    assert expected.starter_binding_checksum == confirmed.starter_binding_checksum


def test_non_pitcher_strikeout_distribution_is_rejected() -> None:
    wrong = DiscreteDistributionV1(
        kind=PredictionDistributionKind.PLAYER_STAT_COUNT,
        outcomes=(DiscreteOutcomeProbabilityV1(0, 1.0),),
    )
    with pytest.raises(PitcherStrikeoutPredictionError, match="PITCHER_STRIKEOUTS"):
        build_pitcher_strikeout_prediction(
            source_game_id="statcast:12345",
            pitcher_id="mlbam:543210",
            pitcher_name="Example Starter",
            team_id="NYY",
            opponent_team_id="BOS",
            starter_binding_state=PitcherStarterBindingState.CONFIRMED,
            starter_binding_checksum="c" * 64,
            source_model_input_checksum="a" * 64,
            model_manifest_checksum="b" * 64,
            model_id="DSE_PITCHER_K_REFERENCE_V1",
            model_version="1.0.0",
            expected_strikeouts=5.2,
            strikeout_distribution=wrong,
        )


def test_prediction_rejects_non_independent_model_scope() -> None:
    with pytest.raises(PitcherStrikeoutPredictionError, match="independent"):
        replace(_prediction(), model_scope="team_runs_derived")
