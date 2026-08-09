from __future__ import annotations

import argparse

import pytest

from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1
from scripts.materialize_scoring_training import CliUsageError, _selected_features


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
