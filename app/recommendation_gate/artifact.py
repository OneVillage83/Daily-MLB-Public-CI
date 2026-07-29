from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.recommendation_gate.contracts import (
    RecommendationGateContractError,
    RecommendationGateV1,
)
from app.redaction import redact_value

RECOMMENDATION_GATE_ARTIFACT_RELPATH = (
    "recommendation_gate/snapshots/{recommendation_gate_checksum}/"
    "recommendation_gate_v1.json"
)


@dataclass(frozen=True, slots=True)
class RecommendationGateArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def recommendation_gate_artifact_relpath(
    recommendation_gate: RecommendationGateV1,
) -> str:
    return RECOMMENDATION_GATE_ARTIFACT_RELPATH.format(
        recommendation_gate_checksum=recommendation_gate.checksum
    )


def write_recommendation_gate_artifact(
    recommendation_gate: RecommendationGateV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> RecommendationGateArtifactV1:
    selected = (
        recommendation_gate_artifact_relpath(recommendation_gate)
        if relpath is None
        else relpath
    )
    safe_relpath = validate_artifact_relpath(selected)
    payload = recommendation_gate.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise RecommendationGateContractError(
            "Recommendation Gate artifact contains credentials"
        )
    content = recommendation_gate.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return RecommendationGateArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
