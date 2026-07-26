from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"


class FailureStage(StrEnum):
    BEFORE_WORKER_START = "before_worker_start"
    STARTUP_RECONCILIATION = "startup_reconciliation"
    WORKER_EXECUTION = "worker_execution"
    COLLECTOR = "collector"
    PERSISTENCE = "persistence"
    ARTIFACT_GENERATION = "artifact_generation"


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.FAILED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
            RunStatus.FAILED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.COMPLETED_WITH_WARNINGS: frozenset(),
    RunStatus.FAILED: frozenset(),
}


class InvalidRunTransition(ValueError):
    def __init__(
        self,
        current: RunStatus,
        target: RunStatus,
        failure_stage: FailureStage | None,
        reason: str,
    ) -> None:
        self.current = current
        self.target = target
        self.failure_stage = failure_stage
        self.reason = reason
        super().__init__(f"invalid run transition {current.value} -> {target.value}: {reason}")


def validate_transition(
    current: RunStatus,
    target: RunStatus,
    failure_stage: FailureStage | None = None,
) -> None:
    if not isinstance(current, RunStatus):
        raise TypeError("current must be a RunStatus")
    if not isinstance(target, RunStatus):
        raise TypeError("target must be a RunStatus")
    if failure_stage is not None and not isinstance(failure_stage, FailureStage):
        raise TypeError("failure_stage must be a FailureStage or None")

    if target is RunStatus.FAILED and failure_stage is None:
        raise InvalidRunTransition(current, target, failure_stage, "failed transitions require a failure stage")
    if target is not RunStatus.FAILED and failure_stage is not None:
        raise InvalidRunTransition(current, target, failure_stage, "failure stage is valid only for failed transitions")
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidRunTransition(current, target, failure_stage, "state transition is not allowed")
