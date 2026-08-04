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

MATCHUP_PACKET_ATTEMPT_MANIFEST_CONTRACT = "DSE_MATCHUP_PACKET_ATTEMPT_MANIFEST_V1"
MatchupPacketAttemptManifestV1: TypeAlias = PreModelAttemptManifestV1
MatchupPacketAttemptManifestArtifactV1: TypeAlias = PreModelArtifactV1


def create_matchup_packet_attempt_manifest(
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
    assembly_policy_version: str,
    secret_values: Iterable[str] = (),
) -> MatchupPacketAttemptManifestV1:
    return PreModelAttemptManifestV1(
        contract_version=MATCHUP_PACKET_ATTEMPT_MANIFEST_CONTRACT,
        phase_key="matchup_packet",
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
            "assembly_policy_version": assembly_policy_version,
        },
        secret_values=secret_values,
    )


def publish_matchup_packet_attempt_manifest(
    manifest: MatchupPacketAttemptManifestV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> MatchupPacketAttemptManifestArtifactV1:
    return publish_manifest(
        manifest, artifact_root, "matchup_packet", secret_values=secret_values
    )


def verify_matchup_packet_attempt_manifest(
    manifest: MatchupPacketAttemptManifestV1,
    artifact: MatchupPacketAttemptManifestArtifactV1,
    artifact_root: Path,
    *,
    secret_values: Iterable[str] = (),
) -> None:
    verify_manifest(manifest, artifact, artifact_root, secret_values=secret_values)
