from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, NoReturn, Protocol

from app.redaction import redact_text
from app.run_controller.contracts import (
    CANONICAL_PIPELINE_PHASES,
    SUCCESSFUL_PHASE_STATUSES,
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelinePhaseV1,
    PipelineRunStatus,
    PipelineRunV1,
)
from app.run_controller.repository import (
    PipelineRepositoryInvariantError,
    PipelineRunNotFoundError,
    PipelineRunRepository,
)


PIPELINE_VERSION_V1 = "DSE_MANUAL_RUN_CONTROLLER_V1"
CONFIGURATION_VERSION_V1 = "DSE_DAILY_MLB_CONFIG_V1"
HANDLER_TERMINAL_STATUSES = frozenset(
    {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
    }
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("controller clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if started_at is None or completed_at is None:
        return None
    return max(
        0.0,
        (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)).total_seconds(),
    )


@dataclass(frozen=True, slots=True)
class PhaseExecutionContext:
    run_id: str
    requested_date: str
    as_of_time: str
    timezone: str
    phase_key: PipelinePhaseKey
    attempt_number: int
    retry: bool
    force_refresh: bool
    pipeline_version: str
    configuration_version: str
    configuration_fingerprint: str
    code_revision: str


@dataclass(frozen=True, slots=True)
class PhaseExecutionResult:
    status: PipelinePhaseStatus
    input_checksum: str | None = None
    output_checksum: str | None = None
    artifact_relpath: str | None = None
    warnings: Any | None = None
    continue_pipeline: bool = True

    def __post_init__(self) -> None:
        if self.status not in HANDLER_TERMINAL_STATUSES:
            raise ValueError(
                "phase handlers must return succeeded, succeeded_with_warnings, "
                "or degraded"
            )
        if (
            self.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
            and self.warnings is None
        ):
            raise ValueError("succeeded_with_warnings requires warning metadata")


class PhaseHandler(Protocol):
    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult: ...


class ManualRunControllerError(RuntimeError):
    pass


class ManualRunExecutionBlocked(ManualRunControllerError):
    def __init__(self, run_id: str, phase_key: PipelinePhaseKey) -> None:
        self.run_id = run_id
        self.phase_key = phase_key
        super().__init__(
            f"execution blocked at {phase_key.value}: no phase handler is registered"
        )


class ManualRunAwaitingHumanInput(ManualRunControllerError):
    def __init__(self, run_id: str, phase_key: PipelinePhaseKey) -> None:
        self.run_id = run_id
        self.phase_key = phase_key
        super().__init__(
            f"execution paused at {phase_key.value}: awaiting explicit human input"
        )


class ManualRunRecoveryRequired(ManualRunControllerError):
    def __init__(self, run_id: str, phase_key: PipelinePhaseKey) -> None:
        self.run_id = run_id
        self.phase_key = phase_key
        super().__init__(
            f"recovery required for active phase {phase_key.value}; "
            "automatic crash recovery is not enabled"
        )


class ManualRunExecutionConflict(ManualRunControllerError):
    pass


class ManualRunExecutionError(ManualRunControllerError):
    def __init__(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        safe_message: str,
    ) -> None:
        self.run_id = run_id
        self.phase_key = phase_key
        self.safe_message = safe_message
        super().__init__(f"phase {phase_key.value} failed: {safe_message}")


@dataclass(frozen=True, slots=True)
class ManualRunSummaryV1:
    run: PipelineRunV1
    phases: tuple[PipelinePhaseV1, ...]
    phase_status_counts: Mapping[str, int]
    completed_phase_count: int
    current_phase: PipelinePhaseKey | None
    next_actionable_phase: PipelinePhaseKey | None
    warning_phase_count: int
    degraded_phase_count: int
    reused_phase_count: int
    skipped_phase_count: int
    duration_seconds: float | None
    resumable: bool
    human_review_status: PipelinePhaseStatus

    def as_dict(self) -> dict[str, Any]:
        run = self.run
        return {
            "as_of_time": run.as_of_time,
            "code_revision": run.code_revision,
            "completed_at": run.completed_at,
            "completed_phase_count": self.completed_phase_count,
            "configuration_fingerprint": run.configuration_fingerprint,
            "configuration_version": run.configuration_version,
            "created_at": run.created_at,
            "current_phase": self.current_phase.value if self.current_phase else None,
            "database_schema_version": run.database_schema_version,
            "degraded_phase_count": self.degraded_phase_count,
            "duration_seconds": self.duration_seconds,
            "failed_phase": run.failure_phase.value if run.failure_phase else None,
            "final_summary": run.final_summary,
            "force_refresh": run.force_refresh,
            "human_review_status": self.human_review_status.value,
            "next_actionable_phase": (
                self.next_actionable_phase.value
                if self.next_actionable_phase
                else None
            ),
            "phase_status_counts": dict(self.phase_status_counts),
            "phases": [
                {
                    "artifact_relpath": phase.artifact_relpath,
                    "attempt_count": phase.attempt_count,
                    "completed_at": phase.completed_at,
                    "error": phase.error,
                    "input_checksum": phase.input_checksum,
                    "ordinal": phase.ordinal,
                    "output_checksum": phase.output_checksum,
                    "phase_key": phase.phase_key.value,
                    "reused_from_run_id": phase.reused_from_run_id,
                    "started_at": phase.started_at,
                    "status": phase.status.value,
                    "updated_at": phase.updated_at,
                    "warnings": phase.warnings,
                }
                for phase in self.phases
            ],
            "pipeline_version": run.pipeline_version,
            "requested_date": run.requested_date,
            "resumable": self.resumable,
            "reused_phase_count": self.reused_phase_count,
            "run_id": run.run_id,
            "run_type": run.run_type,
            "skipped_phase_count": self.skipped_phase_count,
            "sport": run.sport,
            "started_at": run.started_at,
            "status": run.status.value,
            "timezone": run.timezone,
            "total_phases": len(self.phases),
            "updated_at": run.updated_at,
            "warning_phase_count": self.warning_phase_count,
        }


class ManualRunController:
    def __init__(
        self,
        repository: PipelineRunRepository,
        *,
        timezone_name: str,
        configuration_metadata: Mapping[str, Any],
        handlers: Mapping[PipelinePhaseKey, PhaseHandler] | None = None,
        pipeline_version: str = PIPELINE_VERSION_V1,
        configuration_version: str = CONFIGURATION_VERSION_V1,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(configuration_metadata, Mapping):
            raise TypeError("configuration_metadata must be a mapping")
        normalized_handlers: dict[PipelinePhaseKey, PhaseHandler] = {}
        for phase_key, handler in (handlers or {}).items():
            key = (
                phase_key
                if isinstance(phase_key, PipelinePhaseKey)
                else PipelinePhaseKey(phase_key)
            )
            if not callable(handler):
                raise TypeError(f"handler for {key.value} must be callable")
            normalized_handlers[key] = handler
        self.repository = repository
        self.timezone_name = timezone_name
        self._configuration_metadata = copy.deepcopy(dict(configuration_metadata))
        self.handlers = MappingProxyType(normalized_handlers)
        self.pipeline_version = pipeline_version
        self.configuration_version = configuration_version
        self.clock = clock

    def start(
        self,
        requested_date: str,
        *,
        force_refresh: bool = False,
    ) -> ManualRunSummaryV1:
        now = self.clock()
        created = self.repository.create_pipeline_run(
            requested_date=requested_date,
            as_of_time=now,
            timezone_name=self.timezone_name,
            pipeline_version=self.pipeline_version,
            configuration_version=self.configuration_version,
            configuration_metadata=copy.deepcopy(self._configuration_metadata),
            force_refresh=force_refresh,
            created_at=_timestamp(now),
        )
        return self._summarize(created.run, created.phases)

    def show(self, run_id: str) -> ManualRunSummaryV1:
        run = self.repository.get_pipeline_run(run_id)
        if run is None:
            raise PipelineRunNotFoundError(f"pipeline run not found: {run_id}")
        phases = self.repository.get_pipeline_run_phases(run_id)
        return self._summarize(run, phases)

    def summarize(self, run_id: str) -> ManualRunSummaryV1:
        return self.show(run_id)

    def execute(self, run_id: str) -> ManualRunSummaryV1:
        summary = self.show(run_id)
        if summary.run.status in {
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.SUCCEEDED_WITH_WARNINGS,
            PipelineRunStatus.DEGRADED,
        }:
            raise ManualRunExecutionConflict(
                "terminal pipeline run cannot execute without explicit reopened work"
            )
        if summary.run.status is PipelineRunStatus.FAILED:
            raise ManualRunExecutionConflict("failed pipeline run must be resumed explicitly")
        return self._execute_loop(run_id)

    def resume(self, run_id: str) -> ManualRunSummaryV1:
        summary = self.show(run_id)
        if summary.run.status is PipelineRunStatus.PENDING:
            return self._execute_loop(run_id)
        if summary.run.status is PipelineRunStatus.RUNNING:
            active = self._running_phases(summary.phases)
            if active:
                raise ManualRunRecoveryRequired(run_id, active[0].phase_key)
            return self._execute_loop(run_id)
        if summary.run.status is PipelineRunStatus.FAILED:
            failed_key = summary.run.failure_phase
            if failed_key is None:
                raise PipelineRepositoryInvariantError(
                    "failed pipeline run has no failure phase"
                )
            failed_phase = next(
                (phase for phase in summary.phases if phase.phase_key is failed_key),
                None,
            )
            if failed_phase is None or failed_phase.status is not PipelinePhaseStatus.FAILED:
                raise PipelineRepositoryInvariantError(
                    "failed pipeline run does not identify a failed phase"
                )
            if failed_key not in self.handlers:
                raise ManualRunExecutionBlocked(run_id, failed_key)
            timestamp = _timestamp(self.clock())
            resumed = self.repository.resume_failed_pipeline_run_and_phase(
                run_id,
                failed_key,
                reason="manual controller run resume and phase retry",
                transitioned_at=timestamp,
            )
            retried = next(
                phase
                for phase in resumed.phases
                if phase.phase_key is failed_key
            )
            return self._execute_loop(
                run_id,
                owned_running_phase=retried,
                retry=True,
            )
        raise ManualRunExecutionConflict("terminal pipeline run cannot be resumed")

    def _execute_loop(
        self,
        run_id: str,
        *,
        owned_running_phase: PipelinePhaseV1 | None = None,
        retry: bool = False,
    ) -> ManualRunSummaryV1:
        while True:
            summary = self.show(run_id)
            running = self._running_phases(summary.phases)
            if owned_running_phase is not None:
                if (
                    len(running) != 1
                    or running[0].phase_key is not owned_running_phase.phase_key
                ):
                    raise PipelineRepositoryInvariantError(
                        "controller-owned running phase changed unexpectedly"
                    )
                phase = running[0]
                owned_running_phase = None
            else:
                if running:
                    raise ManualRunRecoveryRequired(run_id, running[0].phase_key)
                next_phase = self._first_incomplete(summary.phases)
                retry = False
                if next_phase is None:
                    return self._finalize_or_return(summary)
                if next_phase.status is PipelinePhaseStatus.FAILED:
                    raise ManualRunRecoveryRequired(run_id, next_phase.phase_key)
                if next_phase.status is not PipelinePhaseStatus.PENDING:
                    raise PipelineRepositoryInvariantError(
                        "first incomplete phase is not actionable"
                    )
                handler = self.handlers.get(next_phase.phase_key)
                if handler is None:
                    raise ManualRunExecutionBlocked(run_id, next_phase.phase_key)
                awaiting_input = getattr(handler, "awaiting_input", None)
                if callable(awaiting_input) and bool(awaiting_input(run_id)):
                    raise ManualRunAwaitingHumanInput(run_id, next_phase.phase_key)
                if summary.run.status is PipelineRunStatus.PENDING:
                    self.repository.transition_pipeline_run(
                        run_id,
                        PipelineRunStatus.RUNNING,
                        reason="first registered phase execution",
                        transitioned_at=_timestamp(self.clock()),
                    )
                phase = self.repository.transition_pipeline_phase(
                    run_id,
                    next_phase.phase_key,
                    PipelinePhaseStatus.RUNNING,
                    reason="manual controller phase execution",
                    transitioned_at=_timestamp(self.clock()),
                )

            result = self._invoke_handler(self.show(run_id).run, phase, retry=retry)
            try:
                completed = self.repository.transition_pipeline_phase(
                    run_id,
                    phase.phase_key,
                    result.status,
                    input_checksum=result.input_checksum,
                    output_checksum=result.output_checksum,
                    artifact_relpath=result.artifact_relpath,
                    warnings=result.warnings,
                    reason="manual controller handler result",
                    transitioned_at=_timestamp(self.clock()),
                )
            except Exception as exc:
                self._fail_phase(self.show(run_id).run, phase, exc)
            if not result.continue_pipeline:
                refreshed = self.show(run_id)
                if all(
                    item.status in SUCCESSFUL_PHASE_STATUSES
                    for item in refreshed.phases
                ):
                    return self._finalize_or_return(refreshed)
                return refreshed
            if completed.status not in SUCCESSFUL_PHASE_STATUSES:
                raise PipelineRepositoryInvariantError(
                    "handler did not leave a completed phase"
                )

    def _invoke_handler(
        self,
        run: PipelineRunV1,
        phase: PipelinePhaseV1,
        *,
        retry: bool,
    ) -> PhaseExecutionResult:
        handler = self.handlers.get(phase.phase_key)
        if handler is None:
            raise ManualRunExecutionBlocked(run.run_id, phase.phase_key)
        context = PhaseExecutionContext(
            run_id=run.run_id,
            requested_date=run.requested_date,
            as_of_time=run.as_of_time,
            timezone=run.timezone,
            phase_key=phase.phase_key,
            attempt_number=phase.attempt_count,
            retry=retry,
            force_refresh=run.force_refresh,
            pipeline_version=run.pipeline_version,
            configuration_version=run.configuration_version,
            configuration_fingerprint=run.configuration_fingerprint,
            code_revision=run.code_revision,
        )
        try:
            result = handler(context)
            if not isinstance(result, PhaseExecutionResult):
                raise TypeError("phase handler must return PhaseExecutionResult")
            return result
        except Exception as exc:
            self._fail_phase(run, phase, exc)

    def _fail_phase(
        self,
        run: PipelineRunV1,
        phase: PipelinePhaseV1,
        exc: Exception,
    ) -> NoReturn:
        safe_message = redact_text(str(exc), self.repository.secret_values)
        if not safe_message:
            safe_message = "phase handler failed"
        timestamp = _timestamp(self.clock())
        self.repository.fail_running_pipeline_phase_and_run(
            run.run_id,
            phase.phase_key,
            error={
                "error_type": type(exc).__name__,
                "message": safe_message,
            },
            error_message=safe_message,
            reason="manual controller handler failure",
            transitioned_at=timestamp,
        )
        raise ManualRunExecutionError(
            run.run_id,
            phase.phase_key,
            safe_message,
        ) from exc

    def _finalize_or_return(
        self,
        summary: ManualRunSummaryV1,
    ) -> ManualRunSummaryV1:
        if not all(
            phase.status in SUCCESSFUL_PHASE_STATUSES for phase in summary.phases
        ):
            return summary
        if summary.run.status is not PipelineRunStatus.RUNNING:
            raise PipelineRepositoryInvariantError(
                "completed phases require a running pipeline run before finalization"
            )
        statuses = {phase.status for phase in summary.phases}
        if PipelinePhaseStatus.DEGRADED in statuses:
            target = PipelineRunStatus.DEGRADED
        elif PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS in statuses:
            target = PipelineRunStatus.SUCCEEDED_WITH_WARNINGS
        else:
            target = PipelineRunStatus.SUCCEEDED
        completed_at = _timestamp(self.clock())
        final_summary = summary.as_dict()
        final_summary.update(
            {
                "completed_at": completed_at,
                "current_phase": None,
                "duration_seconds": _duration_seconds(
                    summary.run.started_at,
                    completed_at,
                ),
                "next_actionable_phase": None,
                "resumable": False,
                "status": target.value,
            }
        )
        self.repository.transition_pipeline_run(
            summary.run.run_id,
            target,
            final_summary=final_summary,
            reason="all canonical phases reached completed states",
            transitioned_at=completed_at,
        )
        return self.show(summary.run.run_id)

    @staticmethod
    def _running_phases(
        phases: tuple[PipelinePhaseV1, ...],
    ) -> tuple[PipelinePhaseV1, ...]:
        return tuple(
            phase for phase in phases if phase.status is PipelinePhaseStatus.RUNNING
        )

    @staticmethod
    def _first_incomplete(
        phases: tuple[PipelinePhaseV1, ...],
    ) -> PipelinePhaseV1 | None:
        return next(
            (
                phase
                for phase in phases
                if phase.status not in SUCCESSFUL_PHASE_STATUSES
            ),
            None,
        )

    @staticmethod
    def _summarize(
        run: PipelineRunV1,
        phases: tuple[PipelinePhaseV1, ...],
    ) -> ManualRunSummaryV1:
        if len(phases) != len(CANONICAL_PIPELINE_PHASES):
            raise PipelineRepositoryInvariantError(
                "pipeline run summary requires all canonical phases"
            )
        expected = tuple(item.key for item in CANONICAL_PIPELINE_PHASES)
        if tuple(phase.phase_key for phase in phases) != expected:
            raise PipelineRepositoryInvariantError(
                "pipeline phases are not in canonical order"
            )
        counts = Counter(phase.status.value for phase in phases)
        status_counts = MappingProxyType(
            {status.value: counts.get(status.value, 0) for status in PipelinePhaseStatus}
        )
        current = next(
            (
                phase.phase_key
                for phase in phases
                if phase.status
                in {PipelinePhaseStatus.RUNNING, PipelinePhaseStatus.FAILED}
            ),
            None,
        )
        next_phase = current or next(
            (
                phase.phase_key
                for phase in phases
                if phase.status is PipelinePhaseStatus.PENDING
            ),
            None,
        )
        completed = sum(
            phase.status in SUCCESSFUL_PHASE_STATUSES for phase in phases
        )
        active = any(
            phase.status is PipelinePhaseStatus.RUNNING for phase in phases
        )
        resumable = (
            run.status in {PipelineRunStatus.PENDING, PipelineRunStatus.FAILED}
            or (run.status is PipelineRunStatus.RUNNING and not active)
        )
        human_review = next(
            phase
            for phase in phases
            if phase.phase_key is PipelinePhaseKey.HUMAN_REVIEW
        )
        return ManualRunSummaryV1(
            run=run,
            phases=phases,
            phase_status_counts=status_counts,
            completed_phase_count=completed,
            current_phase=current,
            next_actionable_phase=next_phase,
            warning_phase_count=counts[PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS.value],
            degraded_phase_count=counts[PipelinePhaseStatus.DEGRADED.value],
            reused_phase_count=counts[PipelinePhaseStatus.REUSED.value],
            skipped_phase_count=counts[PipelinePhaseStatus.SKIPPED.value],
            duration_seconds=_duration_seconds(run.started_at, run.completed_at),
            resumable=resumable,
            human_review_status=human_review.status,
        )
