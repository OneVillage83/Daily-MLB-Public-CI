from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.predictions.historical_materialization import (
    HistoricalScoringMaterializationArtifactV1,
    HistoricalScoringMaterializationError,
    _build_feature_coverage,
    load_historical_scoring_dataset,
    write_historical_scoring_materialization_artifact,
)
from app.predictions.historical_sources import (
    FinalGameScoreSourceExclusionV1,
    FinalGameScoreSourceInventoryV1,
    FinalGameScoreSourceLineageV1,
    HistoricalModelFeatureSetInventoryV1,
)
from app.predictions.training_data import materialize_scoring_training_data
from tests.test_scoring_training_data_materialization import (
    FEATURES,
    _feature_set,
    _score,
)


def _snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_id="mfs-20240601",
        run_id="run_20240601_fixture",
        phase_attempt=1,
        feature_set=_feature_set(),
        sealed_at=datetime(2024, 6, 1, 18, 2, tzinfo=timezone.utc),
    )


def _feature_inventory() -> HistoricalModelFeatureSetInventoryV1:
    return HistoricalModelFeatureSetInventoryV1(
        start_date=date(2024, 6, 1),
        end_date=date(2024, 6, 1),
        snapshots=(_snapshot(),),  # type: ignore[arg-type]
    )


def _score_inventory(
    feature_inventory: HistoricalModelFeatureSetInventoryV1,
) -> FinalGameScoreSourceInventoryV1:
    score = replace(
        _score(),
        source_provider="statcast",
        source_payload_checksum="a" * 64,
    )
    lineage = FinalGameScoreSourceLineageV1(
        source_game_id=score.source_game_id,
        run_id="run_20240601_fixture",
        model_feature_set_snapshot_id="mfs-20240601",
        model_feature_set_checksum=(
            feature_inventory.snapshots[0].feature_set.checksum
        ),
        daily_slate_snapshot_id="slate-20240601",
        daily_slate_game_checksum="d" * 64,
        result_provider="statcast",
        result_game_identity_id="game:statcast:123456",
        result_status_observation_id=1,
        result_revision_number=1,
        result_source_checksum="b" * 64,
        result_normalized_checksum="e" * 64,
        raw_payload_checksum="a" * 64,
        completion_contract="DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1",
        final_observed_at=score.completed_at,
    )
    return FinalGameScoreSourceInventoryV1(
        feature_inventory_checksum=feature_inventory.checksum,
        scores=(score,),
        lineages=(lineage,),
        exclusions=(),
    )


def _artifact() -> HistoricalScoringMaterializationArtifactV1:
    features = _feature_inventory()
    scores = _score_inventory(features)
    materialization = materialize_scoring_training_data(
        tuple(
            snapshot.feature_set for snapshot in features.snapshots
        ),
        scores.scores,
        feature_names=FEATURES,
    )
    return HistoricalScoringMaterializationArtifactV1(
        feature_inventory=features,
        final_score_inventory=scores,
        materialization=materialization,
        feature_coverage=_build_feature_coverage(materialization),
    )


def test_artifact_binds_source_inventories_materialization_and_feature_coverage() -> None:
    artifact = _artifact()
    coverage = artifact.coverage_as_dict()

    assert artifact.source_game_count == 1
    assert artifact.materialization.materialized_game_count == 1
    assert artifact.first_materialized_date == date(2024, 6, 1)
    assert artifact.last_materialized_date == date(2024, 6, 1)
    assert coverage["feature_snapshot_count"] == 1
    assert coverage["final_score_count"] == 1
    assert coverage["materialized_game_count"] == 1
    assert coverage["selected_feature_count"] == 2
    assert coverage["selected_dimension_minimum_total_rows_with_holdout"] == 5
    assert coverage["selected_dimension_total_row_floor_met"] is False
    feature_coverage = coverage["feature_coverage"]
    assert isinstance(feature_coverage, list)
    assert [item["feature_name"] for item in feature_coverage] == list(FEATURES)
    assert all(item["observed_game_count"] == 1 for item in feature_coverage)
    assert all(item["missing_game_count"] == 0 for item in feature_coverage)
    assert all(
        item["distinct_observed_value_count"] == 1
        for item in feature_coverage
    )
    assert len(artifact.checksum) == 64


def test_artifact_write_and_dataset_load_are_checksum_verified(tmp_path) -> None:
    artifact = _artifact()
    path = tmp_path / "historical-materialization.json"

    first = write_historical_scoring_materialization_artifact(artifact, path)
    second = write_historical_scoring_materialization_artifact(artifact, path)
    loaded = load_historical_scoring_dataset(path)

    assert first == second == path
    assert loaded.artifact_checksum == artifact.checksum
    assert loaded.materialization_checksum == artifact.materialization.checksum
    assert loaded.dataset.checksum == artifact.materialization.dataset.checksum
    assert loaded.feature_names == FEATURES

    document = json.loads(path.read_text(encoding="utf-8"))
    document["artifact"]["coverage"]["source_game_count"] = 999
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(
        HistoricalScoringMaterializationError,
        match="artifact checksum mismatch",
    ):
        load_historical_scoring_dataset(path)


def test_missing_score_source_is_visible_in_both_source_and_materialization_exclusions() -> None:
    features = _feature_inventory()
    snapshot = features.snapshots[0]
    game = snapshot.feature_set.games[0]
    scores = FinalGameScoreSourceInventoryV1(
        feature_inventory_checksum=features.checksum,
        scores=(),
        lineages=(),
        exclusions=(
            FinalGameScoreSourceExclusionV1(
                source_game_id=game.source_game_id,
                run_id=snapshot.run_id,
                model_feature_set_snapshot_id=snapshot.snapshot_id,
                model_feature_set_checksum=snapshot.feature_set.checksum,
                reason="missing_validated_statcast_final_score",
            ),
        ),
    )
    materialization = materialize_scoring_training_data(
        tuple(item.feature_set for item in features.snapshots),
        scores.scores,
        feature_names=FEATURES,
    )
    artifact = HistoricalScoringMaterializationArtifactV1(
        feature_inventory=features,
        final_score_inventory=scores,
        materialization=materialization,
        feature_coverage=_build_feature_coverage(materialization),
    )
    coverage = artifact.coverage_as_dict()

    assert coverage["source_game_count"] == 1
    assert coverage["final_score_count"] == 0
    assert coverage["final_score_source_exclusion_count"] == 1
    assert coverage["final_score_source_exclusion_reasons"] == {
        "missing_validated_statcast_final_score": 1
    }
    assert coverage["materialized_game_count"] == 0
    assert coverage["materialization_exclusion_reasons"] == {
        "missing_final_score": 1
    }


def test_artifact_contains_no_market_price_or_recommendation_context() -> None:
    text = json.dumps(_artifact().as_dict(), sort_keys=True).casefold()

    for forbidden in (
        "bookmaker",
        "sportsbook",
        "no_vig",
        "best_price",
        "market_context",
        "recommendation_eligible",
    ):
        assert forbidden not in text
