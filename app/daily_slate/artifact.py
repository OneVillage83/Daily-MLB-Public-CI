from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.daily_slate.contracts import DailySlateContractError, DailySlateV1
from app.redaction import redact_value


DAILY_SLATE_ARTIFACT_RELPATH = (
    "daily_slate/snapshots/{snapshot_checksum}/daily_slate_v1.json"
)


@dataclass(frozen=True, slots=True)
class DailySlateArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def daily_slate_artifact_relpath(slate: DailySlateV1) -> str:
    return DAILY_SLATE_ARTIFACT_RELPATH.format(snapshot_checksum=slate.checksum)


def write_daily_slate_artifact(
    slate: DailySlateV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> DailySlateArtifactV1:
    selected_relpath = (
        daily_slate_artifact_relpath(slate)
        if relpath is None
        else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = slate.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise DailySlateContractError(
            "DailySlateV1 artifact contains credential-bearing material"
        )
    content = slate.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return DailySlateArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
