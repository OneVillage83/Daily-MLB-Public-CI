from __future__ import annotations

import pytest

from app.data_quality.contracts import DataQualityDisposition
from app.model_feature_set.contracts import (
    ModelFeatureGameV1,
    ModelFeatureSetContractError,
)
from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1


def _values() -> tuple[float | None, ...]:
    return tuple(None for _ in MODEL_FEATURE_NAMES_V1)


def test_model_feature_game_requires_edge_id_derived_from_source_game_id() -> None:
    with pytest.raises(ModelFeatureSetContractError, match="edge_event_id identity mismatch"):
        ModelFeatureGameV1(
            edge_event_id="edge:mlb:999999",
            daily_mlb_game_id="game:mlb:900001",
            source_game_id="900001",
            away_team_id="SF",
            home_team_id="LAD",
            upstream_matchup_packet_game_checksum="a" * 64,
            quality_disposition=DataQualityDisposition.READY,
            quality_issue_codes=(),
            market_reference_checksum=None,
            feature_values=_values(),
        )


def test_model_feature_game_requires_daily_id_derived_from_source_game_id() -> None:
    with pytest.raises(ModelFeatureSetContractError, match="daily_mlb_game_id identity mismatch"):
        ModelFeatureGameV1(
            edge_event_id="edge:mlb:900001",
            daily_mlb_game_id="game:mlb:999999",
            source_game_id="900001",
            away_team_id="SF",
            home_team_id="LAD",
            upstream_matchup_packet_game_checksum="a" * 64,
            quality_disposition=DataQualityDisposition.READY,
            quality_issue_codes=(),
            market_reference_checksum=None,
            feature_values=_values(),
        )


def test_model_feature_game_rejects_noncanonical_source_game_id() -> None:
    with pytest.raises(ValueError, match="positive decimal MLB game identifier"):
        ModelFeatureGameV1(
            edge_event_id="edge:mlb:not-a-game",
            daily_mlb_game_id="game:mlb:not-a-game",
            source_game_id="not-a-game",
            away_team_id="SF",
            home_team_id="LAD",
            upstream_matchup_packet_game_checksum="a" * 64,
            quality_disposition=DataQualityDisposition.READY,
            quality_issue_codes=(),
            market_reference_checksum=None,
            feature_values=_values(),
        )
