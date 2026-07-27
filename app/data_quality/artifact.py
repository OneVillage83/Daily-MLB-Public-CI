from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.data_quality.contracts import DataQualityContractError, DataQualityV1
from app.redaction import redact_value

DATA_QUALITY_ARTIFACT_RELPATH = (
    "data_quality/snapshots/{snapshot_checksum}/data_quality_v1.json"
)


@dataclass(frozen=True, slots=True)
class DataQualityArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


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
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = snapshot.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise DataQualityContractError(
            "Data Quality artifact contains credential-bearing material"
        )
    content = snapshot.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return DataQualityArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
