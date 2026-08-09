from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.daily_slate.contracts import daily_mlb_game_id, edge_event_id
from app.data_quality.contracts import DataQualityDisposition
from app.model_feature_set.contracts import ModelFeatureGameV1, ModelFeatureSetV1
from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1
from app.predictions.training_data import (
    FinalGameScoreV1,
    ScoringTrainingMaterializationError,
    materialize_scoring_training_data,
)

AWAY_OPS = "away.lineup.season_to_date.hitting.ops"
HOME_OPS = "home.lineup.season_to_date.hitting.ops"
FEATURES = (AWAY_OPS, HOME_OPS)


def _feature_set(
    source_game_id: str = "123456",
    *,
    requested_date: str = "2024-06-01",
    as_of_time: datetime | None = None,
    observed_at: datetime | None = None,
) -> ModelFeatureSetV1:
    selected = {AWAY_OPS: 0.711, HOME_OPS: 0.748}
    values = tuple(selected.get(name) for name in MODEL_FEATURE_NAMES_V1)
    game = ModelFeatureGameV1(
        edge_event_id=edge_event_id(source_game_id),
        daily_mlb_game_id=daily_mlb_game_id(source_game_id),
        source_game_id=source_game_id,
        away_team_id="SF",
        home_team_id="LAD",
        upstream_matchup_packet_game_checksum="b" * 64,
        quality_disposition=DataQualityDisposition.READY,
        quality_issue_codes=(),
        market_reference_checksum=None,
        feature_values=values,
    )
    return ModelFeatureSetV1(
        requested_date=requested_date,
        as_of_time=as_of_time or datetime(2024, 6, 1, 18, 0, tzinfo=timezone.utc),
        observed_at=observed_at or datetime(2024, 6, 1, 18, 1, tzinfo=timezone.utc),
        upstream_matchup_packet_checksum="a" * 64,
        games=(game,),
    )


def _score(source_game_id: str = "123456") -> FinalGameScoreV1:
    return FinalGameScoreV1(
        source_game_id=source_game_id,
        game_date=date(2024, 6, 1),
        away_team_id="SF",
        home_team_id="LAD",
        away_runs=3,
        home_runs=5,
        scheduled_start_time=datetime(2024, 6, 1, 23, 10, tzinfo=timezone.utc),
        completed_at=datetime(2024, 6, 2, 2, 25, tzinfo=timezone.utc),
        source_provider="mlb_statsapi",
        source_payload_checksum="c" * 64,
    )


def test_materialization_joins_pregame_features_to_final_score_with_exact_lineage() -> None:
    feature_set = _feature_set()
    score = _score()
    first = materialize_scoring_training_data((feature_set,), (score,), feature_names=FEATURES)
    second = materialize_scoring_training_data((feature_set,), (score,), feature_names=FEATURES)

    assert first.checksum == second.checksum
    assert first.materialized_game_count == 1
    assert first.excluded_game_count == 0
    assert first.feature_names == FEATURES
    row = first.dataset.rows[0]
    assert row.source_game_id == "123456"
    assert row.game_date == date(2024, 6, 1)
    assert row.away_team_id == "SF"
    assert row.home_team_id == "LAD"
    assert row.away_runs == 3
    assert row.home_runs == 5
    assert tuple(item.feature_name for item in row.features) == FEATURES
    assert tuple(item.value for item in row.features) == pytest.approx((0.711, 0.748))

    lineage = first.lineages[0]
    feature_game = feature_set.games[0]
    assert lineage.source_game_id == row.source_game_id
    assert lineage.model_feature_set_checksum == feature_set.checksum
    assert lineage.model_feature_game_checksum == feature_game.checksum
    assert lineage.predictive_feature_checksum == feature_game.predictive_feature_checksum
    assert lineage.final_score_checksum == score.checksum
    assert lineage.feature_as_of_time < lineage.scheduled_start_time < lineage.score_completed_at
    assert first.dataset.checksum != first.checksum


def test_materialization_uses_predictive_features_only_and_never_market_context() -> None:
    materialized = materialize_scoring_training_data((_feature_set(),), (_score(),), feature_names=FEATURES)
    payload = materialized.dataset.as_dict()
    text = str(payload).casefold()

    assert "market_context" not in text
    assert "bookmaker" not in text
    assert "best_price" not in text
    assert "odds" not in text
    assert len(materialized.dataset.rows[0].features) == 2


def test_missing_final_score_is_explicitly_excluded_not_silently_dropped() -> None:
    feature_set = _feature_set()
    materialized = materialize_scoring_training_data((feature_set,), (), feature_names=FEATURES)

    assert materialized.dataset.rows == ()
    assert materialized.lineages == ()
    assert materialized.materialized_game_count == 0
    assert materialized.excluded_game_count == 1
    exclusion = materialized.exclusions[0]
    assert exclusion.source_game_id == "123456"
    assert exclusion.reason == "missing_final_score"
    assert exclusion.model_feature_set_checksum == feature_set.checksum
    assert exclusion.model_feature_game_checksum == feature_set.games[0].checksum


def test_post_start_feature_snapshot_is_rejected_as_training_leakage() -> None:
    score = _score()
    late = _feature_set(
        as_of_time=score.scheduled_start_time + timedelta(minutes=1),
        observed_at=score.scheduled_start_time + timedelta(minutes=2),
    )
    with pytest.raises(ScoringTrainingMaterializationError, match="as_of_time is not strictly pregame"):
        materialize_scoring_training_data((late,), (score,), feature_names=FEATURES)

    late_observation = _feature_set(
        as_of_time=score.scheduled_start_time - timedelta(minutes=2),
        observed_at=score.scheduled_start_time + timedelta(seconds=1),
    )
    with pytest.raises(ScoringTrainingMaterializationError, match="observed_at is not strictly pregame"):
        materialize_scoring_training_data((late_observation,), (score,), feature_names=FEATURES)


def test_score_date_and_team_identity_must_match_feature_evidence() -> None:
    feature_set = _feature_set()
    with pytest.raises(ScoringTrainingMaterializationError, match="requested date"):
        materialize_scoring_training_data(
            (feature_set,),
            (replace(_score(), game_date=date(2024, 6, 2)),),
            feature_names=FEATURES,
        )
    with pytest.raises(ScoringTrainingMaterializationError, match="team identity disagree"):
        materialize_scoring_training_data(
            (feature_set,),
            (replace(_score(), away_team_id="SD"),),
            feature_names=FEATURES,
        )


def test_duplicate_final_scores_and_duplicate_feature_games_are_rejected() -> None:
    score = _score()
    feature_set = _feature_set()
    with pytest.raises(ScoringTrainingMaterializationError, match="duplicate final-score"):
        materialize_scoring_training_data((feature_set,), (score, score), feature_names=FEATURES)

    duplicate_snapshot = replace(
        feature_set,
        as_of_time=feature_set.as_of_time - timedelta(minutes=5),
        observed_at=feature_set.observed_at - timedelta(minutes=5),
    )
    with pytest.raises(ScoringTrainingMaterializationError, match="duplicate feature game"):
        materialize_scoring_training_data(
            (duplicate_snapshot, feature_set),
            (score,),
            feature_names=FEATURES,
        )
