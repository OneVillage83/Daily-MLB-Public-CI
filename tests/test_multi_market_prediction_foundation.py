from __future__ import annotations

import pytest

from app.daily_slate.contracts import canonical_sha256
from app.predictions.market_foundation import (
    MULTI_MARKET_ROADMAP_CHECKSUM_V1,
    MULTI_MARKET_ROADMAP_V1,
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    ModelRolloutState,
    MultiMarketContractError,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    PredictionTargetV1,
    capabilities_for_version,
    capability_for_family,
)
from app.predictions.production import (
    PREDICTIONS_CONTRACT_VERSION,
    REVIEWED_ANALYST_PROVIDER_CONTRACT,
)


def test_multi_market_roadmap_is_frozen_and_preserves_v1_moneyline() -> None:
    assert PREDICTIONS_CONTRACT_VERSION == "DSE_MLB_ML_PREDICTIONS_V1"
    assert REVIEWED_ANALYST_PROVIDER_CONTRACT == "DSE_REVIEWED_ANALYST_V1"
    assert canonical_sha256([item.as_dict() for item in MULTI_MARKET_ROADMAP_V1]) == MULTI_MARKET_ROADMAP_CHECKSUM_V1

    expected = {
        1: (PredictionMarketFamily.MONEYLINE,),
        2: (PredictionMarketFamily.RUN_LINE,),
        3: (PredictionMarketFamily.GAME_TOTAL,),
        4: (
            PredictionMarketFamily.FIRST_FIVE_MONEYLINE,
            PredictionMarketFamily.FIRST_FIVE_RUN_LINE,
            PredictionMarketFamily.FIRST_FIVE_TOTAL,
        ),
        5: (PredictionMarketFamily.NRFI_YRFI,),
        6: (PredictionMarketFamily.TEAM_TOTAL,),
        7: (PredictionMarketFamily.PITCHER_STRIKEOUTS,),
        8: (PredictionMarketFamily.PLAYER_PROP,),
    }
    assert {
        version: tuple(capability.family for capability in capabilities_for_version(version))
        for version in range(1, 9)
    } == expected

    v1 = capability_for_family(PredictionMarketFamily.MONEYLINE)
    assert v1.rollout_state is ModelRolloutState.PRODUCTION
    assert v1.recommendation_eligible is True
    for capability in MULTI_MARKET_ROADMAP_V1:
        if capability.roadmap_version == 1:
            continue
        assert capability.rollout_state is ModelRolloutState.REFERENCE
        assert capability.recommendation_eligible is False


def test_prediction_targets_enforce_period_subject_and_v7_strikeout_boundary() -> None:
    game = PredictionTargetV1(
        source_game_id="game:mlb:1",
        family=PredictionMarketFamily.RUN_LINE,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.GAME,
    )
    assert game.subject_id is None
    assert game.statistic is None

    team = PredictionTargetV1(
        source_game_id="game:mlb:1",
        family=PredictionMarketFamily.TEAM_TOTAL,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.TEAM,
        subject_id="LAD",
    )
    assert team.statistic == "runs"

    strikeouts = PredictionTargetV1(
        source_game_id="game:mlb:1",
        family=PredictionMarketFamily.PITCHER_STRIKEOUTS,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.PLAYER,
        subject_id="player:123",
        statistic="Strikeouts",
    )
    assert strikeouts.statistic == "strikeouts"

    hits = PredictionTargetV1(
        source_game_id="game:mlb:1",
        family=PredictionMarketFamily.PLAYER_PROP,
        period=PredictionPeriod.FULL_GAME,
        subject_kind=PredictionSubjectKind.PLAYER,
        subject_id="player:456",
        statistic="hits",
    )
    assert hits.statistic == "hits"

    with pytest.raises(MultiMarketContractError, match="period"):
        PredictionTargetV1(
            source_game_id="game:mlb:1",
            family=PredictionMarketFamily.FIRST_FIVE_TOTAL,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.GAME,
        )
    with pytest.raises(MultiMarketContractError, match="subject"):
        PredictionTargetV1(
            source_game_id="game:mlb:1",
            family=PredictionMarketFamily.TEAM_TOTAL,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.GAME,
        )
    with pytest.raises(MultiMarketContractError, match="reserved"):
        PredictionTargetV1(
            source_game_id="game:mlb:1",
            family=PredictionMarketFamily.PLAYER_PROP,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.PLAYER,
            subject_id="player:789",
            statistic="strikeouts",
        )


def test_discrete_distribution_supports_market_independent_threshold_queries() -> None:
    distribution = DiscreteDistributionV1(
        kind=PredictionDistributionKind.FULL_GAME_RUN_MARGIN,
        outcomes=(
            DiscreteOutcomeProbabilityV1(-1, 0.20),
            DiscreteOutcomeProbabilityV1(0, 0.30),
            DiscreteOutcomeProbabilityV1(1, 0.49),
        ),
        unresolved_probability=0.01,
    )
    at_zero = distribution.threshold_projection(0)
    assert at_zero.below_probability == pytest.approx(0.20)
    assert at_zero.equal_probability == pytest.approx(0.30)
    assert at_zero.above_probability == pytest.approx(0.49)
    assert at_zero.unresolved_probability == pytest.approx(0.01)

    half_run = distribution.threshold_projection(0.5)
    assert half_run.below_probability == pytest.approx(0.50)
    assert half_run.equal_probability == 0.0
    assert half_run.above_probability == pytest.approx(0.49)
    assert half_run.unresolved_probability == pytest.approx(0.01)
    assert len(distribution.checksum) == 64
    assert len(half_run.checksum) == 64


def test_discrete_distribution_rejects_bad_mass_and_unsorted_support() -> None:
    with pytest.raises(MultiMarketContractError, match="sum to one"):
        DiscreteDistributionV1(
            kind=PredictionDistributionKind.TEAM_RUNS,
            outcomes=(DiscreteOutcomeProbabilityV1(0, 0.4), DiscreteOutcomeProbabilityV1(1, 0.4)),
        )
    with pytest.raises(MultiMarketContractError, match="unique and sorted"):
        DiscreteDistributionV1(
            kind=PredictionDistributionKind.PITCHER_STRIKEOUTS,
            outcomes=(DiscreteOutcomeProbabilityV1(1, 0.5), DiscreteOutcomeProbabilityV1(0, 0.5)),
        )
