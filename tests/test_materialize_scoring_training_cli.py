from __future__ import annotations

import argparse
from typing import cast

import pytest

from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1
from app.predictions.historical_materialization import (
    HistoricalScoringMaterializationArtifactV1,
)
from scripts.materialize_scoring_training import (
    CliUsageError,
    _compact_summary,
    _selected_features,
)


def _args(feature_names: list[str] | None) -> argparse.Namespace:
    return argparse.Namespace(feature_names=feature_names)


def test_selected_features_defaults_to_complete_model_feature_schema() -> None:
    assert _selected_features(_args(None)) == MODEL_FEATURE_NAMES_V1
    assert len(MODEL_FEATURE_NAMES_V1) == 613


def test_selected_features_are_sorted_and_must_be_supported_unique_names() -> None:
    away = "away.lineup.season_to_date.hitting.ops"
    home = "home.lineup.season_to_date.hitting.ops"

    assert _selected_features(_args([home, away])) == (away, home)

    with pytest.raises(CliUsageError, match="duplicates"):
        _selected_features(_args([away, away]))

    with pytest.raises(CliUsageError, match="unsupported"):
        _selected_features(_args(["not.a.real.feature"]))

    with pytest.raises(CliUsageError, match="must not be empty"):
        _selected_features(_args([" "]))


def test_compact_summary_does_not_call_zero_row_features_fully_observed() -> None:
    class _ZeroRowArtifact:
        def summary_as_dict(self) -> dict[str, object]:
            return {
                "coverage": {
                    "materialized_game_count": 0,
                    "feature_coverage": [
                        {
                            "feature_name": "schedule.game_number",
                            "observed_game_count": 0,
                            "missing_game_count": 0,
                            "distinct_observed_value_count": 0,
                        }
                    ],
                }
            }

    summary = _compact_summary(
        cast(HistoricalScoringMaterializationArtifactV1, _ZeroRowArtifact())
    )
    coverage = cast(dict[str, object], summary["coverage"])

    assert coverage["fully_observed_feature_count"] == 0
    assert coverage["never_observed_feature_count"] == 1
    assert coverage["variable_observed_feature_count"] == 0
    assert coverage["constant_observed_feature_count"] == 0
