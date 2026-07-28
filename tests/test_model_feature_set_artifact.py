from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.model_feature_set.artifact import (
    model_feature_set_artifact_relpath,
    write_model_feature_set_artifact,
)
from app.model_feature_set.contracts import ModelFeatureSetV1


def _empty_feature_set() -> ModelFeatureSetV1:
    return ModelFeatureSetV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 27, 14, 30, tzinfo=timezone.utc),
        upstream_matchup_packet_checksum="a" * 64,
        games=(),
    )


def test_artifact_is_content_addressed_and_exact_canonical_bytes(tmp_path: Path) -> None:
    feature_set = _empty_feature_set()
    artifact = write_model_feature_set_artifact(feature_set, tmp_path)

    assert artifact.relpath == model_feature_set_artifact_relpath(feature_set)
    assert artifact.relpath == (
        f"model_feature_set/snapshots/{feature_set.checksum}/model_feature_set_v1.json"
    )
    path = tmp_path / artifact.relpath
    assert path.read_bytes() == feature_set.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(feature_set.canonical_json_bytes())


def test_rewriting_identical_feature_set_is_idempotent(tmp_path: Path) -> None:
    feature_set = _empty_feature_set()
    first = write_model_feature_set_artifact(feature_set, tmp_path)
    second = write_model_feature_set_artifact(feature_set, tmp_path)

    assert first == second
    assert (tmp_path / first.relpath).read_bytes() == feature_set.canonical_json_bytes()


def test_artifact_relpath_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_model_feature_set_artifact(
            _empty_feature_set(),
            tmp_path,
            relpath="../escape.json",
        )


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential-bearing"):
        write_model_feature_set_artifact(
            _empty_feature_set(),
            tmp_path,
            secret_values=("2026",),
        )
