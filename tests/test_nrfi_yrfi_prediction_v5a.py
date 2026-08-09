from __future__ import annotations

import json

import pytest

from app.predictions.contracts import RunDistributionV1
from app.predictions.first_five import FIRST_FIVE_SCORING_PREDICTION_CONTRACT_VERSION
from app.predictions.first_inning import (
    FIRST_INNING_MODEL_SCOPE,
    FirstInningPredictionError,
    FirstInningScoringPredictionV1,
    build_nrfi_yrfi_prediction,
    first_inning_total_runs_distribution,
)
from app.predictions.market_foundation import (
    ModelRolloutState,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    capability_for_family,
)


def _distribution() -> RunDistributionV1:
    return RunDistributionV1(
        home_run_rate=0.4,
        away_run_rate=0.3,
        maximum_run_support=1,
        home_run_probabilities=(0.7, 0.2),
        away_run_probabilities=(0.8, 0.15),
        home_tail_probability=0.1,
        away_tail_probability=0.05,
        retained_joint_probability=0.855,
        approximation_tail_bound=0.145,
    )


def _source(**overrides: object) -> FirstInningScoringPredictionV1:
    values: dict[str, object] = {
        "source_game_id": "2026-08-09_SF_LAD_1",
        "away_team_id": "SF",
        "home_team_id": "LAD",
        "upstream_model_feature_checksum": "1" * 64,
        "model_manifest_checksum": "2" * 64,
        "model_id": "first-inning-scoring",
        "model_version": "evaluation-v1",
        "distribution": _distribution(),
    }
    values.update(overrides)
    return FirstInningScoringPredictionV1(**values)  # type: ignore[arg-type]


def test_v5a_keeps_v4_first_five_contract_unchanged() -> None:
    assert (
        FIRST_FIVE_SCORING_PREDICTION_CONTRACT_VERSION
        == "DSE_MLB_FIRST_FIVE_SCORING_PREDICTION_V1"
    )


def test_v5_capability_remains_first_inning_reference_and_ineligible() -> None:
    capability = capability_for_family(PredictionMarketFamily.NRFI_YRFI)

    assert capability.roadmap_version == 5
    assert capability.period is PredictionPeriod.FIRST_INNING
    assert capability.rollout_state is ModelRolloutState.REFERENCE
    assert capability.recommendation_eligible is False


def test_first_inning_scoring_requires_independent_scope() -> None:
    source = _source()

    assert source.model_scope == FIRST_INNING_MODEL_SCOPE
    assert source.recommendation_eligible is False

    with pytest.raises(FirstInningPredictionError, match="independent first-inning"):
        _source(model_scope="first_five")
    with pytest.raises(FirstInningPredictionError, match="reference-only"):
        _source(recommendation_eligible=True)


def test_first_inning_total_distribution_preserves_retained_and_tail_mass() -> None:
    total = first_inning_total_runs_distribution(_distribution())

    assert total.kind is PredictionDistributionKind.FIRST_INNING_TOTAL_RUNS
    assert sum(item.probability for item in total.outcomes) == pytest.approx(0.855)
    assert total.unresolved_probability == pytest.approx(0.145)
    assert total.outcomes[0].value == 0
    assert total.outcomes[0].probability == pytest.approx(0.56)


def test_nrfi_yrfi_assigns_positive_tail_mass_to_yrfi() -> None:
    source = _source()
    prediction = build_nrfi_yrfi_prediction(source)

    assert prediction.nrfi_probability == pytest.approx(0.56)
    assert prediction.yrfi_probability == pytest.approx(0.44)
    assert prediction.nrfi_probability + prediction.yrfi_probability == pytest.approx(1.0)
    assert prediction.total_runs_distribution.unresolved_probability == pytest.approx(0.145)
    assert prediction.yrfi_probability == pytest.approx(
        sum(
            item.probability
            for item in prediction.total_runs_distribution.outcomes
            if item.value > 0
        )
        + prediction.total_runs_distribution.unresolved_probability
    )
    assert prediction.upstream_first_inning_scoring_checksum == source.checksum
    assert prediction.recommendation_eligible is False


def test_v5a_serialization_is_market_price_blind() -> None:
    source = _source()
    prediction = build_nrfi_yrfi_prediction(source)
    serialized = json.dumps(
        {"source": source.as_dict(), "prediction": prediction.as_dict()},
        sort_keys=True,
    )

    assert "bookmaker" not in serialized
    assert "american_price" not in serialized
    assert "no_vig" not in serialized
    assert "expected_value" not in serialized
    assert "totals_1st_1_innings" not in serialized
