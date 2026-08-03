from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.exporter import atomic_create_bytes
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_value


class PreModelEvidenceError(RuntimeError):
    pass


def aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PreModelEvidenceError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def require_checksum(value: str, field: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise PreModelEvidenceError(
            f"{field} must be 64 lowercase hexadecimal characters"
        )
    return value


def canonical_text(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def json_object(value: str, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PreModelEvidenceError(f"{field} is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise PreModelEvidenceError(f"{field} must be a JSON object")
    return payload


def json_array(value: str, field: str) -> list[Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise PreModelEvidenceError(f"{field} is not valid JSON") from exc
    if not isinstance(payload, list):
        raise PreModelEvidenceError(f"{field} must be a JSON array")
    return payload


@dataclass(frozen=True, slots=True)
class PreModelUpstreamIdentityV1:
    phase_key: str
    snapshot_id: str
    checksum: str

    def __post_init__(self) -> None:
        if not self.phase_key or self.phase_key != self.phase_key.strip():
            raise PreModelEvidenceError("upstream phase_key must be nonblank")
        if not self.snapshot_id or self.snapshot_id != self.snapshot_id.strip():
            raise PreModelEvidenceError("upstream snapshot_id must be nonblank")
        require_checksum(self.checksum, "upstream checksum")

    def as_dict(self) -> dict[str, str]:
        return {
            "checksum": self.checksum,
            "phase_key": self.phase_key,
            "snapshot_id": self.snapshot_id,
        }


@dataclass(frozen=True, slots=True)
class PreModelAttemptManifestV1:
    contract_version: str
    phase_key: str
    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    upstream: tuple[PreModelUpstreamIdentityV1, ...]
    outcome: str
    snapshot_checksum: str | None
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime
    completed_at: datetime
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        if not self.contract_version or not self.phase_key:
            raise PreModelEvidenceError("manifest contract and phase are required")
        validate_run_id(self.run_id)
        if self.phase_attempt < 1:
            raise PreModelEvidenceError("phase_attempt must be positive")
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", aware_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "created_at", aware_utc(self.created_at, "created_at"))
        object.__setattr__(self, "completed_at", aware_utc(self.completed_at, "completed_at"))
        require_checksum(self.phase_input_checksum, "phase_input_checksum")
        if self.snapshot_checksum is not None:
            require_checksum(self.snapshot_checksum, "snapshot_checksum")
        upstream = tuple(self.upstream)
        if len({value.phase_key for value in upstream}) != len(upstream):
            raise PreModelEvidenceError("upstream phases must be unique")
        object.__setattr__(self, "upstream", upstream)
        warnings = tuple(dict(value) for value in self.warnings)
        object.__setattr__(self, "warnings", warnings)
        payload = self.as_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(
            payload,
            configured,
            preserve_field_names=("bookmaker_key", "market_key"),
        ) != payload:
            raise PreModelEvidenceError(
                "attempt manifest contains credential-bearing material"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "outcome": self.outcome,
            "phase_attempt": self.phase_attempt,
            "phase_input_checksum": self.phase_input_checksum,
            "phase_key": self.phase_key,
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "snapshot_checksum": self.snapshot_checksum,
            "upstream": [value.as_dict() for value in self.upstream],
            "warnings": [dict(value) for value in self.warnings],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class PreModelArtifactV1:
    relpath: str
    checksum: str
    byte_count: int
    created: bool = False


def publish_canonical_bytes(
    artifact_root: Path,
    relpath: str,
    content: bytes,
) -> PreModelArtifactV1:
    safe_relpath = validate_artifact_relpath(relpath)
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256(content).hexdigest()
    created = False
    try:
        created = atomic_create_bytes(destination, content)
        observed = destination.read_bytes()
        if observed != content:
            raise PreModelEvidenceError(
                "immutable artifact conflicts with canonical bytes"
            )
    except Exception:
        if created:
            try:
                retained = destination.read_bytes()
            except OSError:
                retained = b""
            if (
                len(retained) == len(content)
                and hashlib.sha256(retained).hexdigest() == checksum
            ):
                destination.unlink(missing_ok=True)
        raise
    return PreModelArtifactV1(
        relpath=safe_relpath,
        checksum=checksum,
        byte_count=len(content),
        created=created,
    )


def verify_canonical_bytes(
    artifact_root: Path,
    artifact: PreModelArtifactV1,
    expected: bytes,
) -> None:
    path = resolve_contained_path(artifact_root, artifact.relpath)
    try:
        observed = path.read_bytes()
    except OSError as exc:
        raise PreModelEvidenceError("immutable artifact is missing or unreadable") from exc
    if (
        len(observed) != artifact.byte_count
        or hashlib.sha256(observed).hexdigest() != artifact.checksum
        or observed != expected
    ):
        raise PreModelEvidenceError("immutable artifact byte evidence does not match")


def cleanup_owned_artifact(artifact_root: Path, artifact: PreModelArtifactV1) -> None:
    if not artifact.created:
        return
    path = resolve_contained_path(artifact_root, artifact.relpath)
    try:
        observed = path.read_bytes()
    except OSError:
        return
    if (
        len(observed) == artifact.byte_count
        and hashlib.sha256(observed).hexdigest() == artifact.checksum
    ):
        path.unlink(missing_ok=True)


def manifest_relpath(phase_directory: str, run_id: str, phase_attempt: int) -> str:
    validate_run_id(run_id)
    if phase_attempt < 1:
        raise PreModelEvidenceError("phase_attempt must be positive")
    return f"{phase_directory}/attempts/{run_id}/attempt_{phase_attempt:04d}.json"


def publish_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact_root: Path,
    phase_directory: str,
) -> PreModelArtifactV1:
    return publish_canonical_bytes(
        artifact_root,
        manifest_relpath(phase_directory, manifest.run_id, manifest.phase_attempt),
        manifest.canonical_json_bytes(),
    )


def verify_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact: PreModelArtifactV1,
    artifact_root: Path,
) -> None:
    verify_canonical_bytes(artifact_root, artifact, manifest.canonical_json_bytes())


def row_checksum(payload: Mapping[str, object]) -> str:
    return canonical_sha256(dict(payload))
