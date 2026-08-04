from __future__ import annotations

from pathlib import Path

from app.pre_model_evidence import PreModelArtifactV1, publish_canonical_bytes, verify_canonical_bytes
from app.predictions.production import PredictionsContractError, PredictionsV1
from app.redaction import redact_value


def predictions_artifact_relpath(snapshot: PredictionsV1) -> str:
    return f"predictions/snapshots/{snapshot.checksum}/predictions_v1.json"


def write_predictions_artifact(
    snapshot: PredictionsV1,
    artifact_root: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> PreModelArtifactV1:
    if redact_value(snapshot.as_dict(), secret_values) != snapshot.as_dict():
        raise PredictionsContractError("Predictions artifact contains credential-bearing material")
    return publish_canonical_bytes(
        artifact_root,
        predictions_artifact_relpath(snapshot),
        snapshot.canonical_json_bytes(),
    )


def verify_predictions_artifact(
    snapshot: PredictionsV1,
    artifact: PreModelArtifactV1,
    artifact_root: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> None:
    if artifact.relpath != predictions_artifact_relpath(snapshot):
        raise PredictionsContractError("Predictions artifact path identity mismatch")
    verify_canonical_bytes(artifact_root, artifact, snapshot.canonical_json_bytes())
    if redact_value(snapshot.as_dict(), secret_values) != snapshot.as_dict():
        raise PredictionsContractError("Predictions artifact contains credential-bearing material")
