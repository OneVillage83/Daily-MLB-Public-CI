from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.data_quality.contracts import DataQualityContractError, DataQualityV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    publish_canonical_bytes,
    verify_canonical_bytes,
)
from app.redaction import redact_value

DATA_QUALITY_ARTIFACT_RELPATH = (
    "data_quality/snapshots/{snapshot_checksum}/data_quality_v1.json"
)


@dataclass(frozen=True, slots=True)
class DataQualityArtifactV1:
    relpath: str
    checksum: str
    byte_count: int
    created: bool = field(default=False, compare=False, repr=False)


def data_quality_artifact_relpath(snapshot: DataQualityV1) -> str:
    return DATA_QUALITY_ARTIFACT_RELPATH.format(snapshot_checksum=snapshot.checksum)


def write_data_quality_artifact(
    snapshot: DataQualityV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> DataQualityArtifactV1:
    selected_relpath = (
        data_quality_artifact_relpath(snapshot) if relpath is None else relpath
    )
    payload = snapshot.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise DataQualityContractError(
            "Data Quality artifact contains credential-bearing material"
        )
    artifact = publish_canonical_bytes(
        artifact_root, selected_relpath, snapshot.canonical_json_bytes()
    )
    return DataQualityArtifactV1(
        relpath=artifact.relpath,
        checksum=artifact.checksum,
        byte_count=artifact.byte_count,
        created=artifact.created,
    )


def verify_data_quality_artifact(
    snapshot: DataQualityV1,
    artifact: DataQualityArtifactV1,
    artifact_root: Path,
) -> None:
    if artifact.relpath != data_quality_artifact_relpath(snapshot):
        raise DataQualityContractError("Data Quality artifact path identity mismatch")
    verify_canonical_bytes(
        artifact_root,
        PreModelArtifactV1(
            artifact.relpath, artifact.checksum, artifact.byte_count, artifact.created
        ),
        snapshot.canonical_json_bytes(),
    )
