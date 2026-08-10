from __future__ import annotations

from dataclasses import replace

import pytest

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
from app.predictions.player_props import (
    BATTER_PLAYER_PROP_STATISTICS,
    PITCHER_PLAYER_PROP_STATISTICS,
    PLAYER_PROP_MODEL_SCOPE,
    PlayerPropPredictionError,
    PlayerPropRole,
    PlayerPropStatistic,
    build_player_prop_prediction,
)


def _distribution() -> DiscreteDistributionV1:
    return DiscreteDistributionV1(
        kind=PredictionDistributionKind.PLAYER_STAT_COUNT,
        outcomes=(
            DiscreteOutcomeProbabilityV1(0, 0.20),
            DiscreteOutcomeProbabilityV1(1, 0.40),
            DiscreteOutcomeProbabilityV1(2, 0.30),
        ),
        unresolved_probability=0.10,
    )


def _prediction():
    return build_player_prop_prediction(
        source_game_id="statcast:12345",
        player_id="mlbam:592450",
        player_name="Example Batter",
        team_id="NYY",
        opponent_team_id="BOS",
        role=PlayerPropRole.BATTER,
        statistic=PlayerPropStatistic.BATTER_HITS,
        source_model_input_checksum="a" * 64,
        model_manifest_checksum="b" * 64,
        model_id="DSE_PLAYER_HITS_REFERENCE_V1",
        model_version="1.0.0",
        expected_stat_count=1.25,
        stat_distribution=_distribution(),
    )


def test_v8_capability_is_reference_player_count() -> None:
    capability = capability_for_family(PredictionMarketFamily.PLAYER_PROP)
    assert capability.roadmap_version == 8
    assert capability.period is PredictionPeriod.FULL_GAME
    assert capability.subject_kind is PredictionSubjectKind.PLAYER
    assert capability.required_distribution_kind is PredictionDistributionKind.PLAYER_STAT_COUNT
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_supported_statistics_keep_v7_pitcher_strikeouts_reserved() -> None:
    assert PlayerPropStatistic.BATTER_STRIKEOUTS in BATTER_PLAYER_PROP_STATISTICS
    assert PlayerPropStatistic.PITCHER_OUTS in PITCHER_PLAYER_PROP_STATISTICS
    assert all(statistic.value != "strikeouts" for statistic in (*BATTER_PLAYER_PROP_STATISTICS, *PITCHER_PLAYER_PROP_STATISTICS))
    with pytest.raises(MultiMarketContractError, match="strikeouts are reserved"):
        PredictionTargetV1(
            source_game_id="statcast:12345",
            family=PredictionMarketFamily.PLAYER_PROP,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.PLAYER,
            subject_id="mlbam:592450",
            statistic="strikeouts",
        )


def test_prediction_is_player_scoped_market_blind_and_reference_only() -> None:
    prediction = _prediction()
    assert prediction.model_scope == PLAYER_PROP_MODEL_SCOPE
    assert prediction.player_id == "mlbam:592450"
    assert prediction.target.subject_id == prediction.player_id
    assert prediction.target.statistic == "hits"
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
    integer = prediction.project(1.0)
    assert integer.under_probability == pytest.approx(0.20)
    assert integer.push_probability == pytest.approx(0.40)
    assert integer.over_probability == pytest.approx(0.30)
    assert integer.unresolved_probability == pytest.approx(0.10)

    half = prediction.project(1.5)
    assert half.under_probability == pytest.approx(0.60)
    assert half.push_probability == pytest.approx(0.0)
    assert half.over_probability == pytest.approx(0.30)
    assert half.unresolved_probability == pytest.approx(0.10)


def test_role_statistic_mismatch_is_rejected() -> None:
    with pytest.raises(PlayerPropPredictionError, match="role disagrees"):
        replace(_prediction(), role=PlayerPropRole.PITCHER)


def test_non_count_distribution_is_rejected() -> None:
    wrong = DiscreteDistributionV1(
        kind=PredictionDistributionKind.FULL_GAME_TOTAL_RUNS,
        outcomes=(DiscreteOutcomeProbabilityV1(0, 1.0),),
    )
    with pytest.raises(PlayerPropPredictionError, match="PLAYER_STAT_COUNT"):
        build_player_prop_prediction(
            source_game_id="statcast:12345",
            player_id="mlbam:592450",
            player_name="Example Batter",
            team_id="NYY",
            opponent_team_id="BOS",
            role=PlayerPropRole.BATTER,
            statistic=PlayerPropStatistic.BATTER_HITS,
            source_model_input_checksum="a" * 64,
            model_manifest_checksum="b" * 64,
            model_id="DSE_PLAYER_HITS_REFERENCE_V1",
            model_version="1.0.0",
            expected_stat_count=1.25,
            stat_distribution=wrong,
        )
