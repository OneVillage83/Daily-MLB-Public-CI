from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    publish_canonical_bytes,
    publish_manifest,
    verify_canonical_bytes,
    verify_manifest,
)
from app.redaction import redact_value


class DecisionEvidenceError(RuntimeError):
    pass


def create_decision_manifest(
    *,
    contract_version: str,
    phase_key: str,
    run_id: str,
    phase_attempt: int,
    requested_date: str,
    as_of_time: datetime,
    observed_at: datetime,
    phase_input_checksum: str,
    upstream: tuple[PreModelUpstreamIdentityV1, ...],
    outcome: str,
    snapshot_checksum: str | None,
    warnings: tuple[Mapping[str, object], ...],
    created_at: datetime,
    phase_input_evidence: Mapping[str, object],
    secret_values: Iterable[str] = (),
) -> PreModelAttemptManifestV1:
    return PreModelAttemptManifestV1(
        contract_version=contract_version,
        phase_key=phase_key,
        run_id=run_id,
        phase_attempt=phase_attempt,
        requested_date=requested_date,
        as_of_time=as_of_time,
        observed_at=observed_at,
        phase_input_checksum=phase_input_checksum,
        upstream=upstream,
        outcome=outcome,
        snapshot_checksum=snapshot_checksum,
        warnings=warnings,
        created_at=created_at,
        completed_at=created_at,
        phase_input_evidence=phase_input_evidence,
        secret_values=secret_values,
    )


def publish_decision_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact_root: Path,
    phase_directory: str,
    *,
    secret_values: Iterable[str] = (),
) -> PreModelArtifactV1:
    return publish_manifest(manifest, artifact_root, phase_directory, secret_values=secret_values)


def verify_decision_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact: PreModelArtifactV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> None:
    verify_manifest(manifest, artifact, artifact_root, secret_values=secret_values)


def publish_snapshot(
    *,
    artifact_root: Path,
    relpath: str,
    payload: Mapping[str, object],
    content: bytes,
    secret_values: Iterable[str] = (),
) -> PreModelArtifactV1:
    configured = tuple(str(value) for value in secret_values if str(value))
    if redact_value(payload, configured, preserve_field_names=("bookmaker_key", "market_key")) != payload:
        raise DecisionEvidenceError("snapshot artifact contains credential-bearing material")
    return publish_canonical_bytes(artifact_root, relpath, content)


def verify_snapshot(
    *,
    artifact_root: Path,
    artifact: PreModelArtifactV1,
    expected_relpath: str,
    payload: Mapping[str, object],
    content: bytes,
    secret_values: Iterable[str] = (),
) -> None:
    if artifact.relpath != expected_relpath:
        raise DecisionEvidenceError("snapshot artifact path identity mismatch")
    verify_canonical_bytes(artifact_root, artifact, content)
    configured = tuple(str(value) for value in secret_values if str(value))
    if redact_value(payload, configured, preserve_field_names=("bookmaker_key", "market_key")) != payload:
        raise DecisionEvidenceError("snapshot artifact contains credential-bearing material")
