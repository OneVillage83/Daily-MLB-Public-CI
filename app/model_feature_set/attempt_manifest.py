from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    publish_manifest,
    verify_manifest,
)
from app.model_feature_set.contracts import ModelFeatureSourceV1

MODEL_FEATURE_SET_ATTEMPT_MANIFEST_CONTRACT = "DSE_MODEL_FEATURE_SET_ATTEMPT_MANIFEST_V1"
ModelFeatureSetAttemptManifestV1: TypeAlias = PreModelAttemptManifestV1
ModelFeatureSetAttemptManifestArtifactV1: TypeAlias = PreModelArtifactV1


def create_model_feature_set_attempt_manifest(
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
    selected_feature_inventory: tuple[ModelFeatureSourceV1, ...],
    selected_feature_inventory_checksum: str,
    inventory_validation_state: str,
    secret_values: Iterable[str] = (),
) -> ModelFeatureSetAttemptManifestV1:
    return PreModelAttemptManifestV1(
        contract_version=MODEL_FEATURE_SET_ATTEMPT_MANIFEST_CONTRACT,
        phase_key="model_feature_set",
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
            "inventory_validation_state": inventory_validation_state,
            "selected_feature_inventory": [
                value.as_dict() for value in selected_feature_inventory
            ],
            "selected_feature_inventory_checksum": selected_feature_inventory_checksum,
        },
        secret_values=secret_values,
    )


def publish_model_feature_set_attempt_manifest(
    manifest: ModelFeatureSetAttemptManifestV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> ModelFeatureSetAttemptManifestArtifactV1:
    return publish_manifest(
        manifest, artifact_root, "model_feature_set", secret_values=secret_values
    )


def verify_model_feature_set_attempt_manifest(
    manifest: ModelFeatureSetAttemptManifestV1,
    artifact: ModelFeatureSetAttemptManifestArtifactV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> None:
    verify_manifest(manifest, artifact, artifact_root, secret_values=secret_values)
