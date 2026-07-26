from __future__ import annotations

from typing import Any

import pytest

from app.run_state import FailureStage, InvalidRunTransition, RunStatus, validate_transition


def test_run_status_contract_is_exact() -> None:
    assert {member.name: member.value for member in RunStatus} == {
        "QUEUED": "queued",
        "RUNNING": "running",
        "COMPLETED": "completed",
        "COMPLETED_WITH_WARNINGS": "completed_with_warnings",
        "FAILED": "failed",
    }


def test_failure_stage_contract_is_exact() -> None:
    assert {member.name: member.value for member in FailureStage} == {
        "BEFORE_WORKER_START": "before_worker_start",
        "STARTUP_RECONCILIATION": "startup_reconciliation",
        "WORKER_EXECUTION": "worker_execution",
        "COLLECTOR": "collector",
        "PERSISTENCE": "persistence",
        "ARTIFACT_GENERATION": "artifact_generation",
    }


@pytest.mark.parametrize(
    ("current", "target", "failure_stage"),
    [
        (RunStatus.QUEUED, RunStatus.RUNNING, None),
        (RunStatus.QUEUED, RunStatus.FAILED, FailureStage.BEFORE_WORKER_START),
        (RunStatus.RUNNING, RunStatus.COMPLETED, None),
        (RunStatus.RUNNING, RunStatus.COMPLETED_WITH_WARNINGS, None),
        (RunStatus.RUNNING, RunStatus.FAILED, FailureStage.WORKER_EXECUTION),
    ],
)
def test_validate_transition_accepts_only_approved_edges(
    current: RunStatus,
    target: RunStatus,
    failure_stage: FailureStage | None,
) -> None:
    validate_transition(current, target, failure_stage)


_ALLOWED_EDGES = {
    (RunStatus.QUEUED, RunStatus.RUNNING),
    (RunStatus.QUEUED, RunStatus.FAILED),
    (RunStatus.RUNNING, RunStatus.COMPLETED),
    (RunStatus.RUNNING, RunStatus.COMPLETED_WITH_WARNINGS),
    (RunStatus.RUNNING, RunStatus.FAILED),
}


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in RunStatus
        for target in RunStatus
        if (current, target) not in _ALLOWED_EDGES
    ],
)
def test_validate_transition_rejects_every_other_edge(current: RunStatus, target: RunStatus) -> None:
    stage = FailureStage.WORKER_EXECUTION if target is RunStatus.FAILED else None
    with pytest.raises(InvalidRunTransition, match="not allowed"):
        validate_transition(current, target, stage)


def test_failed_transition_requires_failure_stage() -> None:
    with pytest.raises(InvalidRunTransition, match="require a failure stage") as captured:
        validate_transition(RunStatus.RUNNING, RunStatus.FAILED)

    assert captured.value.current is RunStatus.RUNNING
    assert captured.value.target is RunStatus.FAILED
    assert captured.value.failure_stage is None


def test_nonfailed_transition_rejects_failure_stage() -> None:
    with pytest.raises(InvalidRunTransition, match="only for failed"):
        validate_transition(RunStatus.QUEUED, RunStatus.RUNNING, FailureStage.COLLECTOR)


@pytest.mark.parametrize(
    ("current", "target", "stage"),
    [
        ("queued", RunStatus.RUNNING, None),
        (RunStatus.QUEUED, "running", None),
        (RunStatus.QUEUED, RunStatus.FAILED, "collector"),
    ],
)
def test_validate_transition_requires_typed_enums(current: Any, target: Any, stage: Any) -> None:
    with pytest.raises(TypeError):
        validate_transition(current, target, stage)
