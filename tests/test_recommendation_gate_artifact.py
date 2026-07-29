from __future__ import annotations

import hashlib
from pathlib import Path

from app.recommendation_gate.artifact import (
    recommendation_gate_artifact_relpath,
    write_recommendation_gate_artifact,
)
from app.recommendation_gate.engine import evaluate_recommendation_gate
from tests.test_recommendation_gate import _eligible_value_engine


def test_recommendation_gate_artifact_is_content_addressed(
    tmp_path: Path,
) -> None:
    recommendation_gate = evaluate_recommendation_gate(_eligible_value_engine())
    artifact = write_recommendation_gate_artifact(recommendation_gate, tmp_path)
    expected_relpath = recommendation_gate_artifact_relpath(recommendation_gate)
    destination = tmp_path / expected_relpath
    content = recommendation_gate.canonical_json_bytes()
    assert artifact.relpath == expected_relpath
    assert destination.read_bytes() == content
    assert artifact.checksum == hashlib.sha256(content).hexdigest()
    assert artifact.byte_count == len(content)


def test_recommendation_gate_artifact_is_idempotent(tmp_path: Path) -> None:
    recommendation_gate = evaluate_recommendation_gate(_eligible_value_engine())
    first = write_recommendation_gate_artifact(recommendation_gate, tmp_path)
    second = write_recommendation_gate_artifact(recommendation_gate, tmp_path)
    assert first == second
