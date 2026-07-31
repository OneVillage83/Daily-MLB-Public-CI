"""Immutable, credential-free Phase 3 attempt manifests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.artifacts import UnsafeArtifactPath, resolve_contained_path, validate_artifact_relpath
from app.daily_slate.contracts import canonical_json_bytes
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_value
from app.exporter import atomic_create_bytes


BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_CONTRACT = (
    "DSE_BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_V1"
)


class BaseballIntelligenceAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    SELECTION_FAILED = "selection_failed"
    ASSEMBLY_FAILED = "assembly_failed"


class BaseballIntelligenceAttemptManifestError(RuntimeError):
    """Raised when retained Phase 3 attempt evidence is unsafe or inconsistent."""


def _checksum(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 checksum")
    return value


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def baseball_intelligence_attempt_manifest_relpath(
    run_id: str, phase_attempt: int
) -> str:
    safe_run_id = validate_run_id(run_id)
    if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
        raise ValueError("phase_attempt must be a positive integer")
    return validate_artifact_relpath(
        f"baseball_intelligence/attempts/{safe_run_id}/attempt_{phase_attempt:04d}.json"
    )


def _warnings(values: Iterable[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    original = tuple(values)
    normalized = tuple(
        dict(item)
        for item in original
        if isinstance(item, Mapping)
    )
    if len(normalized) != len(original):
        raise ValueError("attempt warnings must be mappings")
    return tuple(
        sorted(
            normalized,
            key=lambda item: canonical_json_bytes(item),
        )
    )


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceAttemptManifestV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    upstream_daily_slate_snapshot_id: str
    upstream_daily_slate_checksum: str
    upstream_game_state_snapshot_id: str
    upstream_game_state_checksum: str
    selection_observed_at: datetime
    outcome: BaseballIntelligenceAttemptOutcome | str
    assembly_checksum: str | None
    candidate_canonical_player_ids: tuple[str, ...]
    candidate_feature_snapshot_ids: tuple[str, ...]
    candidate_stats_run_ids: tuple[str, ...]
    candidate_feature_checksums: tuple[str, ...]
    candidate_inventory_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime
    contract_version: str = BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_CONTRACT

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", validate_run_id(self.run_id))
        if isinstance(self.phase_attempt, bool) or not isinstance(self.phase_attempt, int) or self.phase_attempt < 1:
            raise ValueError("phase_attempt must be a positive integer")
        parse_requested_date(self.requested_date)
        for field in (
            "upstream_daily_slate_snapshot_id",
            "upstream_game_state_snapshot_id",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be a non-empty trimmed string")
        for field in (
            "upstream_daily_slate_checksum",
            "upstream_game_state_checksum",
        ):
            object.__setattr__(self, field, _checksum(getattr(self, field), field))
        object.__setattr__(self, "selection_observed_at", _aware(self.selection_observed_at, "selection_observed_at"))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        try:
            selected_outcome = BaseballIntelligenceAttemptOutcome(self.outcome)
        except ValueError as exc:
            raise ValueError("unsupported Baseball Intelligence attempt outcome") from exc
        object.__setattr__(self, "outcome", selected_outcome)
        if selected_outcome is BaseballIntelligenceAttemptOutcome.ASSEMBLED:
            if self.assembly_checksum is None:
                raise ValueError("assembled attempt requires assembly_checksum")
            object.__setattr__(self, "assembly_checksum", _checksum(self.assembly_checksum, "assembly_checksum"))
        elif self.assembly_checksum is not None:
            raise ValueError("failed attempt must not contain assembly_checksum")
        for field in (
            "candidate_canonical_player_ids",
            "candidate_stats_run_ids",
        ):
            raw_values = getattr(self, field)
            values = tuple(sorted({str(value) for value in raw_values}))
            if any(not value or value != value.strip() for value in values):
                raise ValueError(f"{field} must contain non-empty trimmed strings")
            object.__setattr__(self, field, values)
        snapshot_ids = tuple(str(value) for value in self.candidate_feature_snapshot_ids)
        if (
            any(not value or value != value.strip() for value in snapshot_ids)
            or len(snapshot_ids) != len(set(snapshot_ids))
        ):
            raise ValueError(
                "candidate_feature_snapshot_ids must contain unique non-empty trimmed strings"
            )
        object.__setattr__(self, "candidate_feature_snapshot_ids", snapshot_ids)
        object.__setattr__(
            self,
            "candidate_feature_checksums",
            tuple(
                sorted(
                    {
                        _checksum(value, "candidate_feature_checksums")
                        for value in self.candidate_feature_checksums
                    }
                )
            ),
        )
        object.__setattr__(
            self,
            "candidate_inventory_checksum",
            _checksum(self.candidate_inventory_checksum, "candidate_inventory_checksum"),
        )
        object.__setattr__(self, "warnings", _warnings(self.warnings))
        if self.contract_version != BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_CONTRACT:
            raise ValueError("invalid Baseball Intelligence attempt manifest contract_version")
        if redact_value(self.as_dict()) != self.as_dict():
            raise ValueError("Baseball Intelligence attempt manifest contains credential-bearing material")

    def as_dict(self) -> dict[str, object]:
        return {
            "assembly_checksum": self.assembly_checksum,
            "candidate_canonical_player_ids": list(
                self.candidate_canonical_player_ids
            ),
            "candidate_feature_checksums": list(self.candidate_feature_checksums),
            "candidate_feature_snapshot_ids": list(self.candidate_feature_snapshot_ids),
            "candidate_inventory_checksum": self.candidate_inventory_checksum,
            "candidate_stats_run_ids": list(self.candidate_stats_run_ids),
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "outcome": BaseballIntelligenceAttemptOutcome(self.outcome).value,
            "phase_attempt": self.phase_attempt,
            "phase_key": "baseball_intelligence_assembly",
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "selection_observed_at": self.selection_observed_at.isoformat(),
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_daily_slate_snapshot_id": self.upstream_daily_slate_snapshot_id,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
            "upstream_game_state_snapshot_id": self.upstream_game_state_snapshot_id,
            "warnings": [dict(item) for item in self.warnings],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceAttemptManifestArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def write_baseball_intelligence_attempt_manifest(
    manifest: BaseballIntelligenceAttemptManifestV1,
    artifact_root: Path,
) -> BaseballIntelligenceAttemptManifestArtifactV1:
    """Create immutable attempt evidence, or verify an exact idempotent replay."""

    artifact, _ = publish_baseball_intelligence_attempt_manifest(
        manifest,
        artifact_root,
    )
    return artifact


def publish_baseball_intelligence_attempt_manifest(
    manifest: BaseballIntelligenceAttemptManifestV1,
    artifact_root: Path,
) -> tuple[BaseballIntelligenceAttemptManifestArtifactV1, bool]:
    """Atomically publish immutable attempt evidence and report file ownership."""

    relpath = baseball_intelligence_attempt_manifest_relpath(
        manifest.run_id, manifest.phase_attempt
    )
    content = manifest.canonical_json_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    try:
        destination = resolve_contained_path(artifact_root, relpath)
    except UnsafeArtifactPath as exc:
        raise BaseballIntelligenceAttemptManifestError("attempt manifest path is unsafe") from exc
    try:
        created = atomic_create_bytes(destination, content)
    except (OSError, UnsafeArtifactPath) as exc:
        raise BaseballIntelligenceAttemptManifestError(
            "attempt manifest could not be atomically published"
        ) from exc
    if not created:
        existing = verify_baseball_intelligence_attempt_manifest(
            artifact_root=artifact_root,
            relpath=relpath,
            expected=manifest,
        )
        if existing.checksum != checksum or existing.byte_count != len(content):
            raise BaseballIntelligenceAttemptManifestError(
                "immutable attempt manifest conflicts with requested evidence"
            )
        return existing, False
    try:
        artifact = verify_baseball_intelligence_attempt_manifest(
            artifact_root=artifact_root,
            relpath=relpath,
            expected=manifest,
        )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return artifact, True


def verify_baseball_intelligence_attempt_manifest(
    *,
    artifact_root: Path,
    relpath: str,
    expected: BaseballIntelligenceAttemptManifestV1,
) -> BaseballIntelligenceAttemptManifestArtifactV1:
    try:
        safe_relpath = validate_artifact_relpath(relpath)
        if safe_relpath != baseball_intelligence_attempt_manifest_relpath(expected.run_id, expected.phase_attempt):
            raise BaseballIntelligenceAttemptManifestError(
                "attempt manifest path does not match run and attempt identity"
            )
        content = resolve_contained_path(artifact_root, safe_relpath).read_bytes()
    except (FileNotFoundError, UnsafeArtifactPath) as exc:
        raise BaseballIntelligenceAttemptManifestError("attempt manifest is missing or unsafe") from exc
    if content != expected.canonical_json_bytes():
        raise BaseballIntelligenceAttemptManifestError(
            "attempt manifest bytes do not match canonical attempt evidence"
        )
    try:
        parsed: Any = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaseballIntelligenceAttemptManifestError("attempt manifest is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, Mapping) or canonical_json_bytes(dict(parsed)) != content:
        raise BaseballIntelligenceAttemptManifestError("attempt manifest bytes are not canonical JSON")
    if redact_value(parsed) != parsed:
        raise BaseballIntelligenceAttemptManifestError(
            "attempt manifest contains credential-bearing material"
        )
    return BaseballIntelligenceAttemptManifestArtifactV1(
        safe_relpath,
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
