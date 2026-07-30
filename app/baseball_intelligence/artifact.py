from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.baseball_intelligence.contracts import (
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceContractError,
)
from app.redaction import redact_value
from app.exporter import write_bytes

BASEBALL_INTELLIGENCE_ARTIFACT_RELPATH = (
    "baseball_intelligence/snapshots/{assembly_checksum}/"
    "baseball_intelligence_assembly_v1.json"
)


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


class BaseballIntelligenceArtifactIntegrityError(RuntimeError):
    """Raised when retained BIA artifact bytes fail offline verification."""


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
    # The shared writer uses a short same-directory tempfile then os.replace.
    # That preserves atomic replacement without extending the content-addressed
    # destination filename beyond Windows' long-path limits.
    write_bytes(destination, content)
    return BaseballIntelligenceArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )


def verify_baseball_intelligence_artifact(
    assembly: BaseballIntelligenceAssemblyV1,
    artifact: BaseballIntelligenceArtifactV1,
    artifact_root: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> Path:
    """Verify the exact content-addressed BIA artifact without network access."""

    safe_relpath = validate_artifact_relpath(artifact.relpath)
    if safe_relpath != baseball_intelligence_artifact_relpath(assembly):
        raise BaseballIntelligenceArtifactIntegrityError(
            "Baseball Intelligence artifact path does not match its semantic checksum"
        )
    payload = assembly.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise BaseballIntelligenceArtifactIntegrityError(
            "Baseball Intelligence artifact contains credential-bearing material"
        )
    try:
        destination = resolve_contained_path(artifact_root, safe_relpath)
        content = destination.read_bytes()
    except (FileNotFoundError, ValueError) as exc:
        raise BaseballIntelligenceArtifactIntegrityError(
            "Baseball Intelligence artifact is missing or unsafe"
        ) from exc
    expected = assembly.canonical_json_bytes()
    if (
        content != expected
        or hashlib.sha256(content).hexdigest() != artifact.checksum
        or len(content) != artifact.byte_count
    ):
        raise BaseballIntelligenceArtifactIntegrityError(
            "Baseball Intelligence artifact bytes, checksum, or byte count do not match"
        )
    return destination
