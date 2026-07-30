from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests

from app.database import Database
from app.migrations import CURRENT_SCHEMA_VERSION
from app.run_controller.contracts import (
    CANONICAL_PIPELINE_PHASES,
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import (
    DuplicatePipelineRunError,
    PipelineRunRepository,
)
from app.run_controller.service import (
    ManualRunController,
    ManualRunExecutionBlocked,
    ManualRunExecutionError,
    ManualRunRecoveryRequired,
    PhaseExecutionContext,
    PhaseExecutionResult,
)


NOW = datetime(2026, 7, 26, 15, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _clock() -> datetime:
    return NOW


def _build(
    tmp_path: Path,
    *,
    handlers: dict[PipelinePhaseKey, Any] | None = None,
    secret_values: tuple[str, ...] = (),
    configuration_metadata: dict[str, object] | None = None,
) -> tuple[ManualRunController, PipelineRunRepository]:
    repository = PipelineRunRepository(
        Database(tmp_path / "pipeline.db"),
        secret_values=secret_values,
        repository_root=tmp_path / "not-a-git-repository",
    )
    controller = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata=configuration_metadata
        or {
            "execution": {"mode": "manual", "network_enabled": False},
            "odds": {"enabled": False, "regions": "us"},
        },
        handlers=handlers,
        clock=_clock,
    )
    return controller, repository


def _success_handler(
    calls: list[PipelinePhaseKey] | None = None,
    *,
    status: PipelinePhaseStatus = PipelinePhaseStatus.SUCCEEDED,
    warnings: object | None = None,
) -> Any:
    def handler(context: PhaseExecutionContext) -> PhaseExecutionResult:
        if calls is not None:
            calls.append(context.phase_key)
        return PhaseExecutionResult(
            status=status,
            input_checksum=HASH_A,
            output_checksum=HASH_B,
            artifact_relpath=f"2026-07-26/run/{context.phase_key.value}.json",
            warnings=warnings,
        )

    return handler


def _all_handlers(
    *,
    overrides: dict[PipelinePhaseKey, PhaseExecutionResult] | None = None,
    calls: list[PipelinePhaseKey] | None = None,
) -> dict[PipelinePhaseKey, Any]:
    results = overrides or {}

    def handler(context: PhaseExecutionContext) -> PhaseExecutionResult:
        if calls is not None:
            calls.append(context.phase_key)
        return results.get(
            context.phase_key,
            PhaseExecutionResult(status=PipelinePhaseStatus.SUCCEEDED),
        )

    return {definition.key: handler for definition in CANONICAL_PIPELINE_PHASES}


def test_start_initializes_pending_run_without_executing_daily_slate(
    tmp_path: Path,
) -> None:
    calls: list[PipelinePhaseKey] = []
    controller, repository = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: _success_handler(calls)},
    )

    summary = controller.start("2026-07-26")

    assert summary.run.status is PipelineRunStatus.PENDING
    assert summary.run.database_schema_version == CURRENT_SCHEMA_VERSION
    assert len(summary.phases) == 15
    assert all(
        phase.status is PipelinePhaseStatus.PENDING
        and phase.attempt_count == 0
        for phase in summary.phases
    )
    assert summary.next_actionable_phase is PipelinePhaseKey.DAILY_SLATE
    assert summary.phase_status_counts["pending"] == 15
    assert calls == []
    assert len(repository.get_pipeline_run_transitions(summary.run.run_id)) == 16


def test_start_validates_exact_date_aware_clock_timezone_and_force_identity(
    tmp_path: Path,
) -> None:
    controller, _ = _build(tmp_path)
    with pytest.raises(ValueError):
        controller.start("07/26/2026")

    ordinary = controller.start("2026-07-26")
    forced = controller.start("2026-07-26", force_refresh=True)

    assert ordinary.run.requested_date == "2026-07-26"
    assert forced.run.requested_date == ordinary.run.requested_date
    assert ordinary.run.as_of_time == NOW.isoformat()
    assert datetime.fromisoformat(ordinary.run.as_of_time).tzinfo is not None
    assert ordinary.run.timezone == "America/Los_Angeles"

    controller.clock = lambda: datetime(2026, 7, 26, 15)
    with pytest.raises(ValueError, match="timezone-aware"):
        controller.start("2026-07-27")


def test_duplicate_start_refuses_and_force_creates_intentional_new_run(
    tmp_path: Path,
) -> None:
    controller, _ = _build(tmp_path)
    first = controller.start("2026-07-26")
    with pytest.raises(DuplicatePipelineRunError):
        controller.start("2026-07-26")

    forced = controller.start("2026-07-26", force_refresh=True)

    assert forced.run.run_id != first.run.run_id
    assert forced.run.force_refresh is True
    assert forced.run.status is PipelineRunStatus.PENDING


def test_show_is_read_only_canonical_and_json_deterministic(tmp_path: Path) -> None:
    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")
    before = repository.get_pipeline_run_transitions(created.run.run_id)

    first = controller.show(created.run.run_id)
    second = controller.show(created.run.run_id)

    assert [phase.phase_key for phase in first.phases] == [
        definition.key for definition in CANONICAL_PIPELINE_PHASES
    ]
    assert json.dumps(first.as_dict(), sort_keys=True) == json.dumps(
        second.as_dict(),
        sort_keys=True,
    )
    assert "configuration_metadata" not in first.as_dict()
    assert first.run.updated_at == second.run.updated_at
    assert [phase.updated_at for phase in first.phases] == [
        phase.updated_at for phase in second.phases
    ]
    assert repository.get_pipeline_run_transitions(created.run.run_id) == before


def test_missing_initial_handler_blocks_without_state_corruption(
    tmp_path: Path,
) -> None:
    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.execute(created.run.run_id)

    assert captured.value.phase_key is PipelinePhaseKey.DAILY_SLATE
    persisted = controller.show(created.run.run_id)
    assert persisted.run.status is PipelineRunStatus.PENDING
    assert all(phase.status is PipelinePhaseStatus.PENDING for phase in persisted.phases)
    assert len(repository.get_pipeline_run_transitions(created.run.run_id)) == 16


def test_synthetic_success_persists_output_and_never_skips_unhandled_phase(
    tmp_path: Path,
) -> None:
    calls: list[PipelinePhaseKey] = []
    controller, _ = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: _success_handler(calls)},
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.execute(created.run.run_id)

    assert captured.value.phase_key is PipelinePhaseKey.GAME_STATE
    summary = controller.show(created.run.run_id)
    daily = summary.phases[0]
    assert summary.run.status is PipelineRunStatus.RUNNING
    assert daily.status is PipelinePhaseStatus.SUCCEEDED
    assert daily.attempt_count == 1
    assert daily.input_checksum == HASH_A
    assert daily.output_checksum == HASH_B
    assert daily.artifact_relpath == "2026-07-26/run/daily_slate.json"
    assert summary.phases[1].status is PipelinePhaseStatus.PENDING
    assert calls == [PipelinePhaseKey.DAILY_SLATE]


def test_resume_after_clean_block_without_handler_blocks_again_not_recovery(
    tmp_path: Path,
) -> None:
    controller, repository = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: _success_handler()},
    )
    created = controller.start("2026-07-26")
    with pytest.raises(ManualRunExecutionBlocked):
        controller.execute(created.run.run_id)
    before = controller.show(created.run.run_id)
    before_transitions = repository.get_pipeline_run_transitions(created.run.run_id)

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.resume(created.run.run_id)

    after = controller.show(created.run.run_id)
    assert captured.value.phase_key is PipelinePhaseKey.GAME_STATE
    assert after.run.status is PipelineRunStatus.RUNNING
    assert after.phases[0] == before.phases[0]
    assert after.phases[1].status is PipelinePhaseStatus.PENDING
    assert after.phases[1].attempt_count == 0
    assert repository.get_pipeline_run_transitions(created.run.run_id) == before_transitions


def test_synthetic_handlers_execute_in_persisted_canonical_order(
    tmp_path: Path,
) -> None:
    calls: list[PipelinePhaseKey] = []
    first_three = [definition.key for definition in CANONICAL_PIPELINE_PHASES[:3]]
    controller, _ = _build(
        tmp_path,
        handlers={key: _success_handler(calls) for key in first_three},
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.execute(created.run.run_id)

    assert calls == first_three
    assert captured.value.phase_key is PipelinePhaseKey.ODDS_WEATHER
    assert controller.show(created.run.run_id).completed_phase_count == 3


@pytest.mark.parametrize(
    ("status", "warnings"),
    (
        (
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
            {"messages": ["fixture warning"]},
        ),
        (PipelinePhaseStatus.DEGRADED, {"messages": ["fixture degraded"]}),
    ),
)
def test_warning_and_degraded_handler_results_persist_and_continue(
    tmp_path: Path,
    status: PipelinePhaseStatus,
    warnings: object,
) -> None:
    controller, _ = _build(
        tmp_path,
        handlers={
            PipelinePhaseKey.DAILY_SLATE: _success_handler(
                status=status,
                warnings=warnings,
            )
        },
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionBlocked):
        controller.execute(created.run.run_id)

    phase = controller.show(created.run.run_id).phases[0]
    assert phase.status is status
    assert phase.warnings == warnings


def test_handler_exception_fails_phase_and_run_with_redacted_error(
    tmp_path: Path,
) -> None:
    secret = "fixture-configured-secret"
    downstream_called = False

    def failing_handler(context: PhaseExecutionContext) -> PhaseExecutionResult:
        raise RuntimeError(f"provider token={secret}")

    def downstream_handler(context: PhaseExecutionContext) -> PhaseExecutionResult:
        nonlocal downstream_called
        downstream_called = True
        return PhaseExecutionResult(status=PipelinePhaseStatus.SUCCEEDED)

    controller, repository = _build(
        tmp_path,
        handlers={
            PipelinePhaseKey.DAILY_SLATE: failing_handler,
            PipelinePhaseKey.GAME_STATE: downstream_handler,
        },
        secret_values=(secret,),
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionError) as captured:
        controller.execute(created.run.run_id)

    summary = controller.show(created.run.run_id)
    serialized = json.dumps(summary.as_dict(), sort_keys=True)
    transitions = json.dumps(
        [
            {
                "reason": transition.reason,
                "audit": transition.audit_metadata,
            }
            for transition in repository.get_pipeline_run_transitions(
                created.run.run_id
            )
        ],
        sort_keys=True,
    )
    assert summary.run.status is PipelineRunStatus.FAILED
    assert summary.run.failure_phase is PipelinePhaseKey.DAILY_SLATE
    assert summary.phases[0].status is PipelinePhaseStatus.FAILED
    assert summary.phases[1].status is PipelinePhaseStatus.PENDING
    assert secret not in str(captured.value)
    assert secret not in serialized
    assert secret not in transitions
    assert downstream_called is False


@pytest.mark.parametrize("control_exception", (KeyboardInterrupt, SystemExit))
def test_process_control_exceptions_propagate_and_leave_recovery_required_state(
    tmp_path: Path,
    control_exception: type[BaseException],
) -> None:
    def interrupted(context: PhaseExecutionContext) -> PhaseExecutionResult:
        raise control_exception()

    controller, _ = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: interrupted},
    )
    created = controller.start("2026-07-26")

    with pytest.raises(control_exception):
        controller.execute(created.run.run_id)

    summary = controller.show(created.run.run_id)
    assert summary.run.status is PipelineRunStatus.RUNNING
    assert summary.phases[0].status is PipelinePhaseStatus.RUNNING
    assert summary.phases[0].attempt_count == 1
    with pytest.raises(ManualRunRecoveryRequired):
        controller.resume(created.run.run_id)


@pytest.mark.parametrize("invalid_kind", ("checksum", "artifact", "warnings"))
def test_invalid_handler_result_fails_closed_instead_of_leaving_running_state(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    def invalid_result(context: PhaseExecutionContext) -> PhaseExecutionResult:
        if invalid_kind == "artifact":
            return PhaseExecutionResult(
                status=PipelinePhaseStatus.SUCCEEDED,
                artifact_relpath="../escape.json",
            )
        if invalid_kind == "warnings":
            return PhaseExecutionResult(
                status=PipelinePhaseStatus.SUCCEEDED,
                warnings={"note": "github_" + "pat_" + ("x" * 70)},
            )
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED,
            output_checksum="not-a-checksum",
        )

    controller, _ = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: invalid_result},
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)

    summary = controller.show(created.run.run_id)
    assert summary.run.status is PipelineRunStatus.FAILED
    assert summary.phases[0].status is PipelinePhaseStatus.FAILED
    assert summary.phases[0].attempt_count == 1
    assert summary.phases[0].output_checksum is None
    assert summary.phases[0].artifact_relpath is None
    assert summary.phases[0].warnings is None
    assert summary.phases[1].status is PipelinePhaseStatus.PENDING


@pytest.mark.parametrize(
    "forbidden_status",
    (
        PipelinePhaseStatus.PENDING,
        PipelinePhaseStatus.RUNNING,
        PipelinePhaseStatus.FAILED,
        PipelinePhaseStatus.SKIPPED,
        PipelinePhaseStatus.REUSED,
    ),
)
def test_handler_cannot_return_non_success_or_manufacture_skip_or_reuse(
    tmp_path: Path,
    forbidden_status: PipelinePhaseStatus,
) -> None:
    def forbidden_result(context: PhaseExecutionContext) -> PhaseExecutionResult:
        return PhaseExecutionResult(status=forbidden_status)

    controller, _ = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: forbidden_result},
    )
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)

    summary = controller.show(created.run.run_id)
    assert summary.run.status is PipelineRunStatus.FAILED
    assert summary.phases[0].status is PipelinePhaseStatus.FAILED
    assert summary.phases[0].attempt_count == 1


def test_failed_resume_retries_exact_phase_without_rerunning_completed_phase(
    tmp_path: Path,
) -> None:
    attempts = 0
    calls: list[PipelinePhaseKey] = []

    def game_state(context: PhaseExecutionContext) -> PhaseExecutionResult:
        nonlocal attempts
        attempts += 1
        calls.append(context.phase_key)
        assert context.attempt_number == attempts
        assert context.retry is (attempts == 2)
        if attempts == 1:
            raise RuntimeError("synthetic first-attempt failure")
        return PhaseExecutionResult(status=PipelinePhaseStatus.SUCCEEDED)

    controller, _ = _build(
        tmp_path,
        handlers={
            PipelinePhaseKey.DAILY_SLATE: _success_handler(calls),
            PipelinePhaseKey.GAME_STATE: game_state,
        },
    )
    created = controller.start("2026-07-26")
    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.resume(created.run.run_id)

    summary = controller.show(created.run.run_id)
    assert captured.value.phase_key is PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY
    assert summary.run.status is PipelineRunStatus.RUNNING
    assert summary.phases[0].attempt_count == 1
    assert summary.phases[1].attempt_count == 2
    assert summary.phases[1].status is PipelinePhaseStatus.SUCCEEDED
    assert calls == [
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseKey.GAME_STATE,
    ]


def test_failed_resume_without_handler_remains_failed_and_blocked(
    tmp_path: Path,
) -> None:
    def failure(context: PhaseExecutionContext) -> PhaseExecutionResult:
        raise RuntimeError("synthetic failure")

    controller, repository = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: failure},
    )
    created = controller.start("2026-07-26")
    with pytest.raises(ManualRunExecutionError):
        controller.execute(created.run.run_id)
    controller_without_handlers = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"execution": {"mode": "manual"}},
        clock=_clock,
    )

    with pytest.raises(ManualRunExecutionBlocked):
        controller_without_handlers.resume(created.run.run_id)

    summary = controller_without_handlers.show(created.run.run_id)
    assert summary.run.status is PipelineRunStatus.FAILED
    assert summary.phases[0].status is PipelinePhaseStatus.FAILED
    assert summary.phases[0].attempt_count == 1


def test_orphan_running_phase_requires_explicit_recovery(tmp_path: Path) -> None:
    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")
    repository.transition_pipeline_run(
        created.run.run_id,
        PipelineRunStatus.RUNNING,
    )
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )

    with pytest.raises(ManualRunRecoveryRequired) as captured:
        controller.resume(created.run.run_id)

    assert captured.value.phase_key is PipelinePhaseKey.DAILY_SLATE
    assert controller.show(created.run.run_id).phases[0].attempt_count == 1


def test_multiple_running_phases_fail_closed_as_recovery_required(
    tmp_path: Path,
) -> None:
    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")
    repository.transition_pipeline_run(
        created.run.run_id,
        PipelineRunStatus.RUNNING,
    )
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.RUNNING,
    )

    with pytest.raises(ManualRunRecoveryRequired):
        controller.resume(created.run.run_id)

    phases = controller.show(created.run.run_id).phases
    assert [phase.status for phase in phases[:2]] == [
        PipelinePhaseStatus.RUNNING,
        PipelinePhaseStatus.RUNNING,
    ]


def test_pending_run_with_running_phase_fails_closed_as_recovery_required(
    tmp_path: Path,
) -> None:
    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )

    with pytest.raises(ManualRunRecoveryRequired):
        controller.resume(created.run.run_id)

    summary = controller.show(created.run.run_id)
    assert summary.run.status is PipelineRunStatus.PENDING
    assert summary.phases[0].status is PipelinePhaseStatus.RUNNING


def test_failed_dependency_barrier_blocks_later_registered_handler(
    tmp_path: Path,
) -> None:
    calls: list[PipelinePhaseKey] = []
    controller, repository = _build(
        tmp_path,
        handlers={PipelinePhaseKey.GAME_STATE: _success_handler(calls)},
    )
    created = controller.start("2026-07-26")
    repository.transition_pipeline_run(
        created.run.run_id,
        PipelineRunStatus.RUNNING,
    )
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.FAILED,
        error={"message": "synthetic inconsistent failure"},
    )

    with pytest.raises(ManualRunRecoveryRequired) as captured:
        controller.execute(created.run.run_id)

    assert captured.value.phase_key is PipelinePhaseKey.DAILY_SLATE
    assert calls == []
    assert controller.show(created.run.run_id).phases[1].status is PipelinePhaseStatus.PENDING


def test_reused_and_pending_skipped_phases_are_completed_without_handler_execution(
    tmp_path: Path,
) -> None:
    calls: list[PipelinePhaseKey] = []
    source_controller, repository = _build(
        tmp_path,
        handlers={PipelinePhaseKey.DAILY_SLATE: _success_handler()},
    )
    source = source_controller.start("2026-07-25")
    with pytest.raises(ManualRunExecutionBlocked):
        source_controller.execute(source.run.run_id)

    target_controller = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"execution": {"mode": "manual"}},
        handlers={
            PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY: _success_handler(calls),
        },
        clock=_clock,
    )
    target = target_controller.start("2026-07-26")
    repository.mark_pipeline_phase_reused(
        target.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        reused_from_run_id=source.run.run_id,
        reason="explicit fixture reuse",
    )
    repository.transition_pipeline_phase(
        target.run.run_id,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.SKIPPED,
        reason="explicit fixture skip before execution",
    )

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        target_controller.execute(target.run.run_id)

    summary = target_controller.show(target.run.run_id)
    assert captured.value.phase_key is PipelinePhaseKey.ODDS_WEATHER
    assert summary.phases[0].status is PipelinePhaseStatus.REUSED
    assert summary.phases[0].reused_from_run_id == source.run.run_id
    assert summary.phases[1].status is PipelinePhaseStatus.SKIPPED
    assert summary.phases[1].attempt_count == 0
    assert summary.phases[2].status is PipelinePhaseStatus.SUCCEEDED
    assert summary.completed_phase_count == 3
    assert calls == [PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY]


def test_explicit_skipped_phase_does_not_degrade_final_run(tmp_path: Path) -> None:
    handlers = _all_handlers()
    controller, repository = _build(tmp_path, handlers=handlers)
    created = controller.start("2026-07-26")
    repository.transition_pipeline_phase(
        created.run.run_id,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.SKIPPED,
        reason="explicit fixture skip before execution",
    )

    summary = controller.execute(created.run.run_id)

    assert summary.run.status is PipelineRunStatus.SUCCEEDED
    assert summary.phases[0].status is PipelinePhaseStatus.SKIPPED
    assert summary.phases[0].attempt_count == 0
    assert summary.skipped_phase_count == 1


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        ({}, PipelineRunStatus.SUCCEEDED),
        (
            {
                PipelinePhaseKey.ODDS_WEATHER: PhaseExecutionResult(
                    status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
                    warnings={"messages": ["fixture warning"]},
                )
            },
            PipelineRunStatus.SUCCEEDED_WITH_WARNINGS,
        ),
        (
            {
                PipelinePhaseKey.ODDS_WEATHER: PhaseExecutionResult(
                    status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
                    warnings={"messages": ["fixture warning"]},
                ),
                PipelinePhaseKey.DATA_QUALITY: PhaseExecutionResult(
                    status=PipelinePhaseStatus.DEGRADED,
                ),
            },
            PipelineRunStatus.DEGRADED,
        ),
    ),
)
def test_final_status_precedence_summary_and_human_review(
    tmp_path: Path,
    overrides: dict[PipelinePhaseKey, PhaseExecutionResult],
    expected: PipelineRunStatus,
) -> None:
    calls: list[PipelinePhaseKey] = []
    controller, _ = _build(
        tmp_path,
        handlers=_all_handlers(overrides=overrides, calls=calls),
    )
    created = controller.start("2026-07-26")

    summary = controller.execute(created.run.run_id)

    assert summary.run.status is expected
    assert summary.completed_phase_count == 15
    assert summary.next_actionable_phase is None
    assert summary.current_phase is None
    assert summary.resumable is False
    assert summary.human_review_status in {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
        PipelinePhaseStatus.SKIPPED,
    }
    assert calls[-1] is PipelinePhaseKey.HUMAN_REVIEW
    assert summary.run.final_summary is not None
    assert summary.run.final_summary["status"] == expected.value


def test_human_review_must_complete_before_finalization(tmp_path: Path) -> None:
    handlers: dict[PipelinePhaseKey, Any] = {
        definition.key: _success_handler()
        for definition in CANONICAL_PIPELINE_PHASES
        if definition.key is not PipelinePhaseKey.HUMAN_REVIEW
    }
    controller, repository = _build(tmp_path, handlers=handlers)
    created = controller.start("2026-07-26")

    with pytest.raises(ManualRunExecutionBlocked) as captured:
        controller.execute(created.run.run_id)

    assert captured.value.phase_key is PipelinePhaseKey.HUMAN_REVIEW
    before = controller.show(created.run.run_id)
    assert before.run.status is PipelineRunStatus.RUNNING
    assert before.completed_phase_count == 14
    assert before.human_review_status is PipelinePhaseStatus.PENDING

    review_controller = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"execution": {"mode": "manual"}},
        handlers={PipelinePhaseKey.HUMAN_REVIEW: _success_handler()},
        clock=_clock,
    )
    after = review_controller.execute(created.run.run_id)
    assert after.run.status is PipelineRunStatus.SUCCEEDED
    assert after.human_review_status is PipelinePhaseStatus.SUCCEEDED


def test_finalization_failure_leaves_completed_run_recoverable(
    tmp_path: Path,
) -> None:
    controller, repository = _build(tmp_path, handlers=_all_handlers())
    created = controller.start("2026-07-26")
    with repository.database.connect(write=True) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_reject_run_finalization
            BEFORE UPDATE ON pipeline_runs
            WHEN OLD.status='running'
              AND NEW.status IN ('succeeded','succeeded_with_warnings','degraded')
            BEGIN
                SELECT RAISE(ABORT, 'synthetic finalization failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic finalization failure"):
        controller.execute(created.run.run_id)

    recoverable = controller.show(created.run.run_id)
    assert recoverable.run.status is PipelineRunStatus.RUNNING
    assert recoverable.completed_phase_count == 15
    assert recoverable.next_actionable_phase is None
    assert recoverable.resumable is True

    with repository.database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER test_reject_run_finalization")
    finalized = controller.resume(created.run.run_id)
    assert finalized.run.status is PipelineRunStatus.SUCCEEDED
    assert finalized.completed_phase_count == 15


def test_configured_secret_is_absent_from_summary_and_persistence(
    tmp_path: Path,
) -> None:
    secret = "fixture-secret-redaction-value"
    controller, repository = _build(
        tmp_path,
        secret_values=(secret,),
        configuration_metadata={
            "execution": {"mode": "manual"},
            "provider": {"note": secret},
        },
    )

    summary = controller.start("2026-07-26")

    assert secret not in json.dumps(summary.as_dict(), sort_keys=True)
    with repository.database.connect() as connection:
        database_text = "\n".join(
            str(value)
            for row in connection.execute(
                """
                SELECT configuration_metadata_json,final_summary_json,error_message
                FROM pipeline_runs
                """
            )
            for value in row
            if value is not None
        )
    assert secret not in database_text


def test_code_revision_fallback_current_schema_and_collector_lifecycle_unchanged(
    tmp_path: Path,
) -> None:
    controller, repository = _build(tmp_path)
    collector = repository.database.create_run(
        "run_20260726_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "2026-07-26",
    )

    summary = controller.start("2026-07-26")

    assert summary.run.code_revision == "unavailable"
    assert CURRENT_SCHEMA_VERSION == 10
    assert repository.database.schema_info()["version"] == CURRENT_SCHEMA_VERSION
    assert repository.database.get_run(collector["run_id"]) == collector


def test_start_show_resume_production_configuration_makes_zero_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        calls.append("network")
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    if importlib.util.find_spec("pybaseball") is not None:
        pybaseball = importlib.import_module("pybaseball")
        monkeypatch.setattr(pybaseball, "statcast", forbidden)

    controller, repository = _build(tmp_path)
    created = controller.start("2026-07-26")
    controller.show(created.run.run_id)
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(created.run.run_id)
    executing_controller = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"execution": {"mode": "manual"}},
        handlers={PipelinePhaseKey.DAILY_SLATE: _success_handler()},
        clock=_clock,
    )
    executing = executing_controller.start("2026-07-26", force_refresh=True)
    with pytest.raises(ManualRunExecutionBlocked):
        executing_controller.execute(executing.run.run_id)

    assert calls == []
