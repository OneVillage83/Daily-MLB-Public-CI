from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.model_feature_set.contracts import ModelFeatureSetContractError, ModelFeatureSetV1
from app.redaction import redact_value

MODEL_FEATURE_SET_ARTIFACT_RELPATH = (
    "model_feature_set/snapshots/{feature_set_checksum}/model_feature_set_v1.json"
)


@dataclass(frozen=True, slots=True)
class ModelFeatureSetArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def model_feature_set_artifact_relpath(feature_set: ModelFeatureSetV1) -> str:
    return MODEL_FEATURE_SET_ARTIFACT_RELPATH.format(
        feature_set_checksum=feature_set.checksum
    )


def write_model_feature_set_artifact(
    feature_set: ModelFeatureSetV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> ModelFeatureSetArtifactV1:
    selected_relpath = (
        model_feature_set_artifact_relpath(feature_set) if relpath is None else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = feature_set.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise ModelFeatureSetContractError(
            "ModelFeatureSet artifact contains credential-bearing material"
        )
    content = feature_set.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return ModelFeatureSetArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
