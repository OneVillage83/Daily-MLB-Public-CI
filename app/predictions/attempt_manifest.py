from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path

from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    publish_manifest,
    verify_manifest,
)
from app.predictions.production import PredictionProviderPolicyV1

PREDICTIONS_ATTEMPT_MANIFEST_CONTRACT = "DSE_PREDICTIONS_ATTEMPT_MANIFEST_V1"


def create_predictions_attempt_manifest(
    *,
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
    completed_at: datetime,
    provider_policy: PredictionProviderPolicyV1,
    expected_game_ids: tuple[str, ...],
    present_input_checksums: tuple[str, ...],
    missing_game_ids: tuple[str, ...],
    input_inventory_checksum: str,
    secret_values: Iterable[str] = (),
) -> PreModelAttemptManifestV1:
    return PreModelAttemptManifestV1(
        contract_version=PREDICTIONS_ATTEMPT_MANIFEST_CONTRACT,
        phase_key="predictions",
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
        completed_at=completed_at,
        phase_input_evidence={
            "expected_game_ids": list(expected_game_ids),
            "input_inventory_checksum": input_inventory_checksum,
            "missing_game_ids": list(missing_game_ids),
            "present_input_checksums": list(present_input_checksums),
            "provider_policy": provider_policy.as_dict(),
        },
        secret_values=secret_values,
    )


def publish_predictions_attempt_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> PreModelArtifactV1:
    return publish_manifest(manifest, artifact_root, "predictions", secret_values=secret_values)


def verify_predictions_attempt_manifest(
    manifest: PreModelAttemptManifestV1,
    artifact: PreModelArtifactV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> None:
    verify_manifest(manifest, artifact, artifact_root, secret_values=secret_values)
