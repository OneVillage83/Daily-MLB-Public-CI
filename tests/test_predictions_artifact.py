from __future__ import annotations

import hashlib

from app.model_feature_set.builder import build_model_feature_set
from app.predictions.artifact import (
    predictions_artifact_relpath,
    write_predictions_artifact,
)
from app.predictions.runtime import predict_model_feature_set
from tests.test_model_feature_set import _packet_from_chain


def test_predictions_artifact_is_content_addressed_and_exact(tmp_path) -> None:
    feature_set = build_model_feature_set(_packet_from_chain())
    predictions = predict_model_feature_set(feature_set)
    expected_relpath = predictions_artifact_relpath(predictions)
    artifact = write_predictions_artifact(predictions, tmp_path)
    destination = tmp_path / expected_relpath
    assert artifact.relpath == expected_relpath
    assert destination.read_bytes() == predictions.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(
        predictions.canonical_json_bytes()
    ).hexdigest()
    assert artifact.byte_count == len(predictions.canonical_json_bytes())


def test_predictions_artifact_same_content_is_idempotent(tmp_path) -> None:
    feature_set = build_model_feature_set(_packet_from_chain())
    predictions = predict_model_feature_set(feature_set)
    first = write_predictions_artifact(predictions, tmp_path)
    second = write_predictions_artifact(predictions, tmp_path)
    assert first == second
