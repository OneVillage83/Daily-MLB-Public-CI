from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.redaction import redact_value
from app.value_engine.contracts import ValueEngineContractError, ValueEngineV1

VALUE_ENGINE_ARTIFACT_RELPATH = (
    "value_engine/snapshots/{value_engine_checksum}/value_engine_v1.json"
)


@dataclass(frozen=True, slots=True)
class ValueEngineArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def value_engine_artifact_relpath(value_engine: ValueEngineV1) -> str:
    return VALUE_ENGINE_ARTIFACT_RELPATH.format(
        value_engine_checksum=value_engine.checksum
    )


def write_value_engine_artifact(
    value_engine: ValueEngineV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> ValueEngineArtifactV1:
    selected = value_engine_artifact_relpath(value_engine) if relpath is None else relpath
    safe_relpath = validate_artifact_relpath(selected)
    payload = value_engine.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise ValueEngineContractError("Value Engine artifact contains credentials")
    content = value_engine.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return ValueEngineArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
