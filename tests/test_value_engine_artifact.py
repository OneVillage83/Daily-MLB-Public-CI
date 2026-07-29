from __future__ import annotations

import hashlib

from app.value_engine.artifact import (
    value_engine_artifact_relpath,
    write_value_engine_artifact,
)
from app.value_engine.engine import evaluate_value_engine
from tests.test_value_engine import _packet_predictions


def test_value_engine_artifact_is_content_addressed_and_exact(tmp_path) -> None:
    packet, predictions = _packet_predictions()
    value_engine = evaluate_value_engine(
        predictions=predictions,
        matchup_packet=packet,
    )
    artifact = write_value_engine_artifact(value_engine, tmp_path)
    expected_relpath = value_engine_artifact_relpath(value_engine)
    destination = tmp_path / expected_relpath
    assert artifact.relpath == expected_relpath
    assert destination.read_bytes() == value_engine.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(
        value_engine.canonical_json_bytes()
    ).hexdigest()
    assert artifact.byte_count == len(value_engine.canonical_json_bytes())


def test_value_engine_artifact_is_idempotent(tmp_path) -> None:
    packet, predictions = _packet_predictions()
    value_engine = evaluate_value_engine(
        predictions=predictions,
        matchup_packet=packet,
    )
    first = write_value_engine_artifact(value_engine, tmp_path)
    second = write_value_engine_artifact(value_engine, tmp_path)
    assert first == second
