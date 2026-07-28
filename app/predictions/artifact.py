from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.predictions.contracts import PredictionsContractError, PredictionsV1
from app.redaction import redact_value

PREDICTIONS_ARTIFACT_RELPATH = (
    "predictions/snapshots/{predictions_checksum}/predictions_v1.json"
)


@dataclass(frozen=True, slots=True)
class PredictionsArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def predictions_artifact_relpath(predictions: PredictionsV1) -> str:
    return PREDICTIONS_ARTIFACT_RELPATH.format(
        predictions_checksum=predictions.checksum
    )


def write_predictions_artifact(
    predictions: PredictionsV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> PredictionsArtifactV1:
    selected_relpath = (
        predictions_artifact_relpath(predictions) if relpath is None else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = predictions.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise PredictionsContractError(
            "Predictions artifact contains credential-bearing material"
        )
    content = predictions.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return PredictionsArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
