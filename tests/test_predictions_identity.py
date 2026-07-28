from __future__ import annotations

from dataclasses import replace

import pytest

from app.model_feature_set.builder import build_model_feature_set
from app.predictions.contracts import PredictionsContractError
from app.predictions.runtime import predict_model_feature_set
from tests.test_model_feature_set import _packet_from_chain


def test_prediction_game_rejects_mismatched_canonical_identity() -> None:
    feature_set = build_model_feature_set(_packet_from_chain())
    prediction = predict_model_feature_set(feature_set).games[0]
    with pytest.raises(PredictionsContractError, match="edge_event_id"):
        replace(prediction, edge_event_id="edge:mlb:999999")
