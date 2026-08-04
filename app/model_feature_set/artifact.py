from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.model_feature_set.contracts import ModelFeatureSetContractError, ModelFeatureSetV1
from app.pre_model_evidence import PreModelArtifactV1, publish_canonical_bytes, verify_canonical_bytes
from app.redaction import redact_value

MODEL_FEATURE_SET_ARTIFACT_RELPATH = (
    "model_feature_set/snapshots/{feature_set_checksum}/model_feature_set_v1.json"
)


@dataclass(frozen=True, slots=True)
class ModelFeatureSetArtifactV1:
    relpath: str
    checksum: str
    byte_count: int
    created: bool = field(default=False, compare=False, repr=False)


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
    payload = feature_set.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise ModelFeatureSetContractError(
            "ModelFeatureSet artifact contains credential-bearing material"
        )
    artifact = publish_canonical_bytes(
        artifact_root, selected_relpath, feature_set.canonical_json_bytes()
    )
    return ModelFeatureSetArtifactV1(
        artifact.relpath, artifact.checksum, artifact.byte_count, artifact.created
    )


def verify_model_feature_set_artifact(
    feature_set: ModelFeatureSetV1,
    artifact: ModelFeatureSetArtifactV1,
    artifact_root: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> None:
    if artifact.relpath != model_feature_set_artifact_relpath(feature_set):
        raise ModelFeatureSetContractError("Model Feature Set artifact path identity mismatch")
    verify_canonical_bytes(
        artifact_root,
        PreModelArtifactV1(artifact.relpath, artifact.checksum, artifact.byte_count),
        feature_set.canonical_json_bytes(),
    )
    if redact_value(
        feature_set.as_dict(), secret_values, preserve_field_names=("key",)
    ) != feature_set.as_dict():
        raise ModelFeatureSetContractError(
            "ModelFeatureSet artifact contains credential-bearing material"
        )
