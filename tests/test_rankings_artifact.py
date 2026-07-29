from __future__ import annotations

import hashlib
from pathlib import Path

from app.rankings.artifact import rankings_artifact_relpath, write_rankings_artifact
from app.rankings.engine import rank_recommendations
from tests.test_rankings import _mixed_gate


def test_rankings_artifact_is_content_addressed(tmp_path: Path) -> None:
    gate, _ = _mixed_gate()
    rankings = rank_recommendations(gate)
    artifact = write_rankings_artifact(rankings, tmp_path)
    expected_relpath = rankings_artifact_relpath(rankings)
    destination = tmp_path / expected_relpath
    content = rankings.canonical_json_bytes()
    assert artifact.relpath == expected_relpath
    assert destination.read_bytes() == content
    assert artifact.checksum == hashlib.sha256(content).hexdigest()
    assert artifact.byte_count == len(content)


def test_rankings_artifact_is_idempotent(tmp_path: Path) -> None:
    gate, _ = _mixed_gate()
    rankings = rank_recommendations(gate)
    first = write_rankings_artifact(rankings, tmp_path)
    second = write_rankings_artifact(rankings, tmp_path)
    assert first == second
