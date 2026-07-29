from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.daily_slate.acquisition import (
    DailySlateNormalizationResultV1,
    MlbScheduleEvidenceV1,
    acquire_mlb_schedule,
    build_mlb_schedule_request,
    normalize_mlb_schedule,
    verified_mlb_player_identity_resolver,
)
from app.daily_slate.artifact import write_daily_slate_artifact
from app.daily_slate.repository import DailySlateRepository
from app.database import Database
from app.identifiers import validate_run_id
from app.redaction import redact_text, redact_value
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.stats.contracts import (
    RawArtifact,
    StatsRequest,
    StatsResponse,
    StatsTransport,
    StatsTransportError,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import HttpStatsTransport

DAILY_SLATE_RAW_LINK_CONTRACT = "DSE_DAILY_SLATE_RAW_LINK_V1"
DAILY_SLATE_RAW_PROVIDER_DIRECTORY = "provider_raw"
_RAW_EVIDENCE_STATES = frozenset(
    {"normalized", "acquisition_failed", "normalization_failed"}
)


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


def _capture_payload(
    capture: RawArtifact,
    *,
    artifact_root: Path,
    ordinal: int,
) -> dict[str, object]:
    return {
        "capture_id": capture.capture_id,
        "checksum_sha256": capture.checksum_sha256,
        "content_type": capture.content_type,
        "metadata_relpath": _relative_raw_path(
            capture.metadata_path,
            artifact_root,
            "raw artifact metadata",
        ),
        "ordinal": ordinal,
        "raw_artifact_relpath": _relative_raw_path(
            capture.path,
            artifact_root,
            "raw artifact",
        ),
        "retrieved_at": capture.retrieved_at.isoformat(),
        "size_bytes": capture.size_bytes,
    }


def write_daily_slate_raw_link(
    *,
    request: StatsRequest,
    captures: Iterable[RawArtifact],
    http_attempts: int,
    http_status: int | None,
    response_headers: Mapping[str, str],
    evidence_state: str,
    artifact_root: Path,
    run_id: str,
    requested_date: str,
    phase_attempt: int,
    recorded_at: datetime,
    error: Exception | None = None,
    secret_values: Iterable[str] = (),
) -> str:
    relpath = daily_slate_raw_link_relpath(run_id, phase_attempt)
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    if evidence_state not in _RAW_EVIDENCE_STATES:
        raise ValueError("unsupported DailySlate raw evidence state")
    if isinstance(http_attempts, bool) or http_attempts < 0:
        raise ValueError("http_attempts must be a non-negative integer")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware")

    retained = tuple(captures)
    if any(capture.provider is not request.provider for capture in retained):
        raise ValueError("raw capture provider does not match DailySlate request")
    capture_payloads = [
        _capture_payload(capture, artifact_root=root, ordinal=index)
        for index, capture in enumerate(retained, start=1)
    ]
    configured_secrets = tuple(str(value) for value in secret_values if str(value))
    error_payload = (
        None
        if error is None
        else {
            "error_type": type(error).__name__,
            "message": redact_text(error, configured_secrets),
        }
    )
    payload: dict[str, Any] = {
        "captures": capture_payloads,
        "contract_version": DAILY_SLATE_RAW_LINK_CONTRACT,
        "endpoint_category": request.endpoint_category,
        "error": error_payload,
        "evidence_state": evidence_state,
        "final_raw_checksum_sha256": (
            retained[-1].checksum_sha256 if retained else None
        ),
        "http_attempts": http_attempts,
        "http_status": http_status,
        "observed_at": retained[-1].retrieved_at.isoformat() if retained else None,
        "phase_attempt": phase_attempt,
        "provider": request.provider.value,
        "recorded_at": recorded_at.astimezone(timezone.utc).isoformat(),
        "request": {
            "method": "GET",
            "params": dict(request.params),
            "url": request.url,
        },
        "requested_date": requested_date,
        "response_headers": dict(response_headers),
        "run_id": run_id,
    }
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
    created = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise
    return relpath


class _RecordingStatsTransport:
    def __init__(self, delegate: StatsTransport) -> None:
        self.delegate = delegate
        self.request: StatsRequest | None = None
        self.response: StatsResponse | None = None
        self.transport_error: StatsTransportError | None = None

    def fetch(self, request: StatsRequest) -> StatsResponse:
        self.request = request
        try:
            response = self.delegate.fetch(request)
        except StatsTransportError as exc:
            self.transport_error = exc
            raise
        self.response = response
        return response

    @property
    def captures(self) -> tuple[RawArtifact, ...]:
        if self.response is not None:
            return self.response.attempt_captures or (self.response.capture,)
        if self.transport_error is not None:
            return self.transport_error.captures
        return ()

    @property
    def attempts(self) -> int:
        if self.response is not None:
            return self.response.attempts
        if self.transport_error is not None:
            return self.transport_error.attempts
        return 0

    @property
    def status_code(self) -> int | None:
        if self.response is not None:
            return self.response.status_code
        if self.transport_error is not None:
            return self.transport_error.status_code
        return None

    @property
    def response_headers(self) -> Mapping[str, str]:
        return {} if self.response is None else self.response.headers


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

    def _write_recorded_raw_link(
        self,
        context: PhaseExecutionContext,
        recording: _RecordingStatsTransport,
        *,
        evidence_state: str,
        error: Exception | None = None,
    ) -> str:
        request = recording.request or build_mlb_schedule_request(
            context.requested_date,
            timeout_seconds=self.request_timeout_seconds,
            max_attempts=self.max_attempts,
        )
        return write_daily_slate_raw_link(
            request=request,
            captures=recording.captures,
            http_attempts=recording.attempts,
            http_status=recording.status_code,
            response_headers=recording.response_headers,
            evidence_state=evidence_state,
            artifact_root=self.artifact_root,
            run_id=context.run_id,
            requested_date=context.requested_date,
            phase_attempt=context.attempt_number,
            recorded_at=self.clock(),
            error=error,
            secret_values=self.secret_values,
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        if context.phase_key is not PipelinePhaseKey.DAILY_SLATE:
            raise ValueError("DailySlatePhaseHandler may only execute DAILY_SLATE")
        if context.attempt_number < 1:
            raise ValueError("DAILY_SLATE phase attempt must be positive")

        recording = _RecordingStatsTransport(self.transport)
        try:
            evidence = acquire_mlb_schedule(
                transport=recording,
                raw_store=self.raw_store,
                requested_date=context.requested_date,
                timeout_seconds=self.request_timeout_seconds,
                max_attempts=self.max_attempts,
            )
        except Exception as exc:
            self._write_recorded_raw_link(
                context,
                recording,
                evidence_state="acquisition_failed",
                error=exc,
            )
            raise

        try:
            normalized = normalize_mlb_schedule(
                evidence.payload,
                requested_date=context.requested_date,
                as_of_time=context.as_of_time,
                observed_at=evidence.observed_at,
                upstream_checksum=evidence.raw_checksum,
                player_identity_resolver=self.player_identity_resolver,
            )
        except Exception as exc:
            self._write_recorded_raw_link(
                context,
                recording,
                evidence_state="normalization_failed",
                error=exc,
            )
            raise

        self._write_recorded_raw_link(
            context,
            recording,
            evidence_state="normalized",
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
