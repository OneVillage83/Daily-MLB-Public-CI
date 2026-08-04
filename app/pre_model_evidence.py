from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, field
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


_MANIFEST_PROFILES: dict[str, tuple[str, frozenset[str], tuple[str, ...]]] = {
    "data_quality": (
        "DSE_DATA_QUALITY_ATTEMPT_MANIFEST_V1",
        frozenset({"assembled", "input_failed", "assessment_failed", "persistence_failed"}),
        ("daily_slate", "game_state", "baseball_intelligence_assembly", "odds_weather"),
    ),
    "matchup_packet": (
        "DSE_MATCHUP_PACKET_ATTEMPT_MANIFEST_V1",
        frozenset({"assembled", "input_failed", "assembly_failed", "persistence_failed"}),
        ("daily_slate", "game_state", "baseball_intelligence_assembly", "odds_weather", "data_quality"),
    ),
    "model_feature_set": (
        "DSE_MODEL_FEATURE_SET_ATTEMPT_MANIFEST_V1",
        frozenset({"assembled", "input_failed", "transformation_failed", "persistence_failed"}),
        ("data_quality", "matchup_packet"),
    ),
}


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
    phase_input_evidence: Mapping[str, object] = field(default_factory=dict)
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        profile = _MANIFEST_PROFILES.get(self.phase_key)
        if profile is None:
            raise PreModelEvidenceError("manifest phase is unsupported")
        contract, outcomes, upstream_phases = profile
        if self.contract_version != contract:
            raise PreModelEvidenceError("manifest contract does not match phase")
        validate_run_id(self.run_id)
        if isinstance(self.phase_attempt, bool) or not isinstance(self.phase_attempt, int) or self.phase_attempt < 1:
            raise PreModelEvidenceError("phase_attempt must be positive")
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", aware_utc(self.observed_at, "observed_at"))
        object.__setattr__(self, "created_at", aware_utc(self.created_at, "created_at"))
        object.__setattr__(self, "completed_at", aware_utc(self.completed_at, "completed_at"))
        if self.completed_at < self.created_at:
            raise PreModelEvidenceError("completed_at cannot precede created_at")
        require_checksum(self.phase_input_checksum, "phase_input_checksum")
        if self.snapshot_checksum is not None:
            require_checksum(self.snapshot_checksum, "snapshot_checksum")
        upstream = tuple(self.upstream)
        if tuple(value.phase_key for value in upstream) != upstream_phases:
            raise PreModelEvidenceError("manifest upstream phases are not exact and ordered")
        object.__setattr__(self, "upstream", upstream)
        if self.outcome not in outcomes:
            raise PreModelEvidenceError("manifest outcome is unsupported for phase")
        if (self.outcome == "assembled") != (self.snapshot_checksum is not None):
            raise PreModelEvidenceError("manifest outcome/snapshot checksum identity is invalid")
        try:
            warnings = tuple(
                json.loads(canonical_json_bytes(dict(value))) for value in self.warnings
            )
        except (TypeError, ValueError) as exc:
            raise PreModelEvidenceError("manifest warnings are not canonical JSON objects") from exc
        object.__setattr__(self, "warnings", warnings)
        try:
            input_evidence = json.loads(
                canonical_json_bytes(dict(self.phase_input_evidence))
            )
        except (TypeError, ValueError) as exc:
            raise PreModelEvidenceError(
                "manifest phase_input_evidence is not canonical JSON"
            ) from exc
        if not isinstance(input_evidence, dict):
            raise PreModelEvidenceError("manifest phase_input_evidence must be an object")
        object.__setattr__(self, "phase_input_evidence", input_evidence)
        self._validate_phase_input_evidence(input_evidence)
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

    def _validate_phase_input_evidence(self, value: dict[str, object]) -> None:
        if self.phase_key == "data_quality":
            if set(value) != {"policy"} or not isinstance(value["policy"], dict):
                raise PreModelEvidenceError("Data Quality manifest requires exact policy evidence")
            policy = value["policy"]
            if set(policy) != {
                "checksum",
                "network_enabled",
                "policy_version",
                "supported_markets",
            }:
                raise PreModelEvidenceError("Data Quality manifest policy is incomplete")
            identity = {key: item for key, item in policy.items() if key != "checksum"}
            if policy["checksum"] != canonical_sha256(identity):
                raise PreModelEvidenceError("Data Quality manifest policy checksum mismatch")
        elif self.phase_key == "matchup_packet":
            if set(value) != {"assembly_policy_version"} or not isinstance(
                value["assembly_policy_version"], str
            ):
                raise PreModelEvidenceError(
                    "Matchup Packet manifest requires its assembly policy"
                )
        elif self.phase_key == "model_feature_set":
            if set(value) != {
                "inventory_validation_state",
                "selected_feature_inventory",
                "selected_feature_inventory_checksum",
            }:
                raise PreModelEvidenceError(
                    "Model Feature Set manifest inventory evidence is incomplete"
                )
            inventory = value["selected_feature_inventory"]
            state = value["inventory_validation_state"]
            if not isinstance(inventory, list) or state not in {"unverified", "validated"}:
                raise PreModelEvidenceError(
                    "Model Feature Set manifest inventory state is invalid"
                )
            if value["selected_feature_inventory_checksum"] != canonical_sha256(inventory):
                raise PreModelEvidenceError(
                    "Model Feature Set manifest inventory checksum mismatch"
                )
            if (self.outcome == "input_failed") != (state == "unverified"):
                raise PreModelEvidenceError(
                    "Model Feature Set manifest inventory state disagrees with outcome"
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
            "phase_input_evidence": dict(self.phase_input_evidence),
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
        observed = _read_owned_bytes(destination)
        if observed != content:
            raise PreModelEvidenceError(
                "immutable artifact conflicts with canonical bytes"
            )
    except Exception:
        if created:
            try:
                retained = _read_owned_bytes(destination)
            except (OSError, PreModelEvidenceError):
                retained = b""
            if (
                len(retained) == len(content)
                and hashlib.sha256(retained).hexdigest() == checksum
            ):
                try:
                    if _safe_file_identity(destination).st_nlink == 1:
                        destination.unlink(missing_ok=True)
                except (OSError, PreModelEvidenceError):
                    pass
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
        observed = _read_owned_bytes(path)
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
        observed = _read_owned_bytes(path)
    except (OSError, PreModelEvidenceError):
        return
    if (
        len(observed) == artifact.byte_count
        and hashlib.sha256(observed).hexdigest() == artifact.checksum
    ):
        try:
            if _safe_file_identity(path).st_nlink == 1:
                path.unlink(missing_ok=True)
        except (OSError, PreModelEvidenceError):
            return


def manifest_relpath(phase_directory: str, run_id: str, phase_attempt: int) -> str:
    validate_run_id(run_id)
    if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
        raise PreModelEvidenceError("phase_attempt must be positive")
    return f"{phase_directory}/attempts/{run_id}/attempt_{phase_attempt:04d}.json"


def publish_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact_root: Path,
    phase_directory: str,
    *,
    secret_values: Iterable[str] = (),
) -> PreModelArtifactV1:
    _validate_manifest_secrets(manifest, secret_values)
    return publish_canonical_bytes(
        artifact_root,
        manifest_relpath(phase_directory, manifest.run_id, manifest.phase_attempt),
        manifest.canonical_json_bytes(),
    )


def verify_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact: PreModelArtifactV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> None:
    _validate_manifest_secrets(manifest, secret_values)
    expected = manifest.canonical_json_bytes()
    verify_canonical_bytes(artifact_root, artifact, expected)
    path = resolve_contained_path(artifact_root, artifact.relpath)
    retained = _read_owned_bytes(path)
    try:
        parsed = json.loads(retained.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreModelEvidenceError("retained manifest is not canonical UTF-8 JSON") from exc
    if not isinstance(parsed, dict) or canonical_json_bytes(parsed) != retained or parsed != manifest.as_dict():
        raise PreModelEvidenceError("retained manifest is not exact canonical evidence")
    configured = tuple(str(value) for value in secret_values if str(value))
    if redact_value(parsed, configured, preserve_field_names=("bookmaker_key", "market_key")) != parsed:
        raise PreModelEvidenceError("retained manifest contains credential-bearing material")


def _validate_manifest_secrets(
    manifest: PreModelAttemptManifestV1,
    secret_values: Iterable[str],
) -> None:
    payload = manifest.as_dict()
    configured = tuple(str(value) for value in secret_values if str(value))
    if redact_value(payload, configured, preserve_field_names=("bookmaker_key", "market_key")) != payload:
        raise PreModelEvidenceError("attempt manifest contains credential-bearing material")


def _safe_file_identity(path: Path) -> os.stat_result:
    link_metadata = path.lstat()
    if stat.S_ISLNK(link_metadata.st_mode):
        raise PreModelEvidenceError("immutable artifact cannot be a symbolic link")
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise PreModelEvidenceError("immutable artifact is not a regular file")
    if hasattr(metadata, "st_nlink") and metadata.st_nlink != 1:
        raise PreModelEvidenceError("immutable artifact must have exactly one hard link")
    return metadata


def _read_owned_bytes(path: Path) -> bytes:
    before = _safe_file_identity(path)
    observed = path.read_bytes()
    after = _safe_file_identity(path)
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if any(getattr(before, name, None) != getattr(after, name, None) for name in identity_fields):
        raise PreModelEvidenceError("immutable artifact changed while being verified")
    if len(observed) != after.st_size:
        raise PreModelEvidenceError("immutable artifact byte count changed while being verified")
    return observed


def row_checksum(payload: Mapping[str, object]) -> str:
    return canonical_sha256(dict(payload))
