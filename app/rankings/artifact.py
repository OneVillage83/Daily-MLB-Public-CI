from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.rankings.contracts import RankingsContractError, RankingsV1
from app.redaction import redact_value

RANKINGS_ARTIFACT_RELPATH = (
    "rankings/snapshots/{rankings_checksum}/rankings_v1.json"
)


@dataclass(frozen=True, slots=True)
class RankingsArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def rankings_artifact_relpath(rankings: RankingsV1) -> str:
    return RANKINGS_ARTIFACT_RELPATH.format(rankings_checksum=rankings.checksum)


def write_rankings_artifact(
    rankings: RankingsV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> RankingsArtifactV1:
    selected = rankings_artifact_relpath(rankings) if relpath is None else relpath
    safe_relpath = validate_artifact_relpath(selected)
    payload = rankings.as_dict()
    if redact_value(payload, secret_values, preserve_field_names=("key",)) != payload:
        raise RankingsContractError("Rankings artifact contains credentials")
    content = rankings.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return RankingsArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
