from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.daily_slate.acquisition import (
    DailySlateNormalizationResultV1,
    MlbScheduleEvidenceV1,
    acquire_mlb_schedule,
    normalize_mlb_schedule,
    verified_mlb_player_identity_resolver,
)
from app.daily_slate.artifact import write_daily_slate_artifact
from app.daily_slate.repository import DailySlateRepository
from app.database import Database
from app.identifiers import validate_run_id
from app.redaction import redact_value
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.stats.contracts import StatsTransport
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import HttpStatsTransport

DAILY_SLATE_RAW_LINK_CONTRACT = "DSE_DAILY_SLATE_RAW_LINK_V1"
DAILY_SLATE_RAW_PROVIDER_DIRECTORY = "provider_raw"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def daily_slate_raw_link_relpath(run_id: str, phase_attempt: int) -> str:
    safe_run_id = validate_run_id(run_id)
    if isinstance(phase_attempt, bool) or phase_attempt < 1:
        raise ValueError("phase_attempt must be a positive integer")
    return validate_artifact_relpath(
        f"daily_slate/raw_links/{safe_run_id}/attempt_{phase_attempt:04d}.json"
    )


def _relative_raw_path(path: Path, artifact_root: Path, field_name: str) -> str:
    resolved_root = artifact_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must remain inside the artifact root") from exc
    return validate_artifact_relpath(relative.as_posix())


def write_daily_slate_raw_link(
    *,
    evidence: MlbScheduleEvidenceV1,
    artifact_root: Path,
    run_id: str,
    requested_date: str,
    phase_attempt: int,
    secret_values: Iterable[str] = (),
) -> str:
    relpath = daily_slate_raw_link_relpath(run_id, phase_attempt)
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    raw_relpath = _relative_raw_path(
        evidence.response.capture.path,
        root,
        "raw artifact",
    )
    raw_metadata_relpath = _relative_raw_path(
        evidence.response.capture.metadata_path,
        root,
        "raw artifact metadata",
    )
    payload: dict[str, Any] = {
        "contract_version": DAILY_SLATE_RAW_LINK_CONTRACT,
        "endpoint_category": evidence.request.endpoint_category,
        "http_attempts": evidence.response.attempts,
        "http_status": evidence.response.status_code,
        "observed_at": evidence.observed_at.isoformat(),
        "phase_attempt": phase_attempt,
        "provider": evidence.response.capture.provider.value,
        "raw_artifact_relpath": raw_relpath,
        "raw_checksum_sha256": evidence.raw_checksum,
        "raw_metadata_relpath": raw_metadata_relpath,
        "raw_size_bytes": evidence.response.capture.size_bytes,
        "request": {
            "method": "GET",
            "params": dict(evidence.request.params),
            "url": evidence.request.url,
        },
        "requested_date": requested_date,
        "response_headers": dict(evidence.response.headers),
        "run_id": run_id,
    }
    configured_secrets = tuple(str(value) for value in secret_values if str(value))
    if redact_value(payload, configured_secrets) != payload:
        raise ValueError("DailySlate raw-link manifest contains credential-bearing material")
    content = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    destination = resolve_contained_path(root, relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise
    return relpath


class DailySlatePhaseHandler:
    """Production handler for the Manual Run Controller DAILY_SLATE phase."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        request_timeout_seconds: float,
        user_agent: str,
        secret_values: Iterable[str] = (),
        max_attempts: int = 3,
        transport: StatsTransport | None = None,
        raw_store: RawArtifactStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if (transport is None) != (raw_store is None):
            raise ValueError("transport and raw_store must be supplied together")
        normalized_user_agent = user_agent.strip()
        if not normalized_user_agent:
            raise ValueError("DailySlate MLB user_agent must not be blank")

        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.request_timeout_seconds = request_timeout_seconds
        self.max_attempts = max_attempts
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        if raw_store is None:
            raw_store = RawArtifactStore(
                self.artifact_root / DAILY_SLATE_RAW_PROVIDER_DIRECTORY
            )
        if transport is None:
            transport = HttpStatsTransport(
                raw_store,
                user_agent=normalized_user_agent,
                clock=clock,
            )
        self.raw_store = raw_store
        self.transport = transport
        self.repository = DailySlateRepository(
            database,
            secret_values=self.secret_values,
            clock=clock,
        )
        self.player_identity_resolver = verified_mlb_player_identity_resolver(database)

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        if context.phase_key is not PipelinePhaseKey.DAILY_SLATE:
            raise ValueError("DailySlatePhaseHandler may only execute DAILY_SLATE")
        if context.attempt_number < 1:
            raise ValueError("DAILY_SLATE phase attempt must be positive")

        evidence = acquire_mlb_schedule(
            transport=self.transport,
            raw_store=self.raw_store,
            requested_date=context.requested_date,
            timeout_seconds=self.request_timeout_seconds,
            max_attempts=self.max_attempts,
        )
        write_daily_slate_raw_link(
            evidence=evidence,
            artifact_root=self.artifact_root,
            run_id=context.run_id,
            requested_date=context.requested_date,
            phase_attempt=context.attempt_number,
            secret_values=self.secret_values,
        )
        normalized = normalize_mlb_schedule(
            evidence.payload,
            requested_date=context.requested_date,
            as_of_time=context.as_of_time,
            observed_at=evidence.observed_at,
            upstream_checksum=evidence.raw_checksum,
            player_identity_resolver=self.player_identity_resolver,
        )
        return self._persist_result(context, evidence, normalized)

    def _persist_result(
        self,
        context: PhaseExecutionContext,
        evidence: MlbScheduleEvidenceV1,
        normalized: DailySlateNormalizationResultV1,
    ) -> PhaseExecutionResult:
        artifact = write_daily_slate_artifact(
            normalized.slate,
            self.artifact_root,
            secret_values=self.secret_values,
        )
        persisted = self.repository.persist_daily_slate(
            run_id=context.run_id,
            phase_attempt=context.attempt_number,
            slate=normalized.slate,
            artifact=artifact,
        )
        if persisted.slate != normalized.slate:
            raise RuntimeError("persisted DailySlateV1 does not match normalized evidence")
        if persisted.artifact_relpath != artifact.relpath:
            raise RuntimeError("persisted DailySlateV1 artifact path does not match writer")
        if persisted.artifact_checksum != artifact.checksum:
            raise RuntimeError("persisted DailySlateV1 artifact checksum does not match writer")

        warnings = normalized.warning_payload()
        status = (
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
            if warnings
            else PipelinePhaseStatus.SUCCEEDED
        )
        return PhaseExecutionResult(
            status=status,
            input_checksum=evidence.raw_checksum,
            output_checksum=normalized.slate.checksum,
            artifact_relpath=artifact.relpath,
            warnings=warnings or None,
        )
