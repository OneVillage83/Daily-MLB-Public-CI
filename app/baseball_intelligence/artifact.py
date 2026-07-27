from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.baseball_intelligence.contracts import (
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceContractError,
)
from app.redaction import redact_value

BASEBALL_INTELLIGENCE_ARTIFACT_RELPATH = (
    "baseball_intelligence/snapshots/{assembly_checksum}/"
    "baseball_intelligence_assembly_v1.json"
)


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def baseball_intelligence_artifact_relpath(
    assembly: BaseballIntelligenceAssemblyV1,
) -> str:
    return BASEBALL_INTELLIGENCE_ARTIFACT_RELPATH.format(
        assembly_checksum=assembly.checksum
    )


def write_baseball_intelligence_artifact(
    assembly: BaseballIntelligenceAssemblyV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> BaseballIntelligenceArtifactV1:
    selected_relpath = (
        baseball_intelligence_artifact_relpath(assembly)
        if relpath is None
        else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = assembly.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise BaseballIntelligenceContractError(
            "Baseball Intelligence artifact contains credential-bearing material"
        )
    content = assembly.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return BaseballIntelligenceArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
