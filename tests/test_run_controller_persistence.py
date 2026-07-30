from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.artifacts import validate_artifact_relpath
from app.database import Database
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_FINGERPRINT,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_FINGERPRINT,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V9_FINGERPRINT,
    MIGRATION_HISTORY,
    MIGRATION_V6_CHECKSUM,
    MIGRATION_V6_NAME,
    ensure_schema,
    schema_fingerprint,
)
from app.run_controller.contracts import (
    CANONICAL_PIPELINE_PHASES,
    InvalidPipelineTransition,
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
    PipelineTransitionType,
    REUSABLE_PHASE_STATUSES,
    SUCCESSFUL_PHASE_STATUSES,
)
from app.run_controller.repository import (
    DuplicatePipelineRunError,
    PipelinePhaseNotFoundError,
    PipelineRepositoryInvariantError,
    PipelineRunRepository,
    resolve_code_revision,
)


RUN_ID = "run_20260726_11111111111111111111111111111111"
FORCED_RUN_ID = "run_20260726_22222222222222222222222222222222"
SOURCE_RUN_ID = "run_20260725_33333333333333333333333333333333"
TARGET_RUN_ID = "run_20260726_44444444444444444444444444444444"
SECOND_FORCED_RUN_ID = "run_20260726_55555555555555555555555555555555"
REUSE_TARGET_RUN_ID = "run_20260727_66666666666666666666666666666666"
TS = "2026-07-26T15:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64


def _repository(
    tmp_path: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> PipelineRunRepository:
    return PipelineRunRepository(
        Database(tmp_path / "pipeline.db"),
        secret_values=secret_values,
        repository_root=tmp_path / "not-a-git-repository",
    )


def _create(
    repository: PipelineRunRepository,
    *,
    requested_date: str = "2026-07-26",
    run_id: str = RUN_ID,
    force_refresh: bool = False,
    configuration_metadata: dict[str, object] | None = None,
):
    return repository.create_pipeline_run(
        requested_date=requested_date,
        as_of_time=datetime(2026, 7, 26, 8, tzinfo=timezone.utc),
        timezone_name="America/Los_Angeles",
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_metadata=configuration_metadata
        or {
            "odds": {"enabled": True, "region_key": "us", "timeout_seconds": 10},
            "weather": {"enabled": True, "provider": "nws"},
        },
        force_refresh=force_refresh,
        code_revision="f" * 40,
        run_id=run_id,
        created_at=TS,
    )


def _complete_phase(
    repository: PipelineRunRepository,
    run_id: str,
    phase_key: PipelinePhaseKey,
) -> None:
    repository.transition_pipeline_phase(
        run_id,
        phase_key,
        PipelinePhaseStatus.RUNNING,
        input_checksum=HASH_A,
        transitioned_at=TS,
    )
    repository.transition_pipeline_phase(
        run_id,
        phase_key,
        PipelinePhaseStatus.SUCCEEDED,
        output_checksum=HASH_B,
        transitioned_at=TS,
    )


def test_canonical_phase_registry_is_exact_and_deterministic() -> None:
    assert [(item.ordinal, item.key.value) for item in CANONICAL_PIPELINE_PHASES] == [
        (1, "daily_slate"),
        (2, "game_state"),
        (3, "baseball_intelligence_assembly"),
        (4, "odds_weather"),
        (5, "data_quality"),
        (6, "matchup_packet"),
        (7, "model_feature_set"),
        (8, "predictions"),
        (9, "value_engine"),
        (10, "recommendation_gate"),
        (11, "rankings"),
        (12, "pdf_report"),
        (13, "infographic"),
        (14, "final_qc"),
        (15, "human_review"),
    ]


def test_creation_is_atomic_and_initializes_exact_phase_and_audit_rows(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    summary = _create(repository)

    assert summary.run.run_id == RUN_ID
    assert summary.run.requested_date == "2026-07-26"
    assert summary.run.status is PipelineRunStatus.PENDING
    assert summary.run.code_revision == "f" * 40
    assert summary.run.database_schema_version == CURRENT_SCHEMA_VERSION
    assert len(summary.phases) == 15
    assert [phase.ordinal for phase in summary.phases] == list(range(1, 16))
    assert {phase.status for phase in summary.phases} == {PipelinePhaseStatus.PENDING}
    assert {phase.attempt_count for phase in summary.phases} == {0}
    transitions = repository.get_pipeline_run_transitions(RUN_ID)
    assert len(transitions) == 16
    assert {item.transition_type for item in transitions} == {
        PipelineTransitionType.CREATED
    }
    assert sum(item.phase_key is None for item in transitions) == 1
    assert sum(item.phase_key is not None for item in transitions) == 15
    assert transitions[0].audit_metadata == {"force_refresh": False}
    assert {
        item.audit_metadata["attempt_count"]
        for item in transitions
        if item.phase_key is not None
    } == {0}
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM collector_runs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM collector_run_transitions"
        ).fetchone()[0] == 0

    failing_repository = PipelineRunRepository(
        Database(tmp_path / "atomic.db"),
        repository_root=tmp_path,
    )
    with failing_repository.database.connect(write=True) as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_pipeline_phase_failure
            BEFORE INSERT ON pipeline_run_phases
            BEGIN
                SELECT RAISE(ABORT, 'injected phase initialization failure');
            END
            """
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        _create(failing_repository)
    with failing_repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_run_phases"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_run_transitions"
        ).fetchone()[0] == 0


def test_run_id_date_invariant_duplicate_guard_and_explicit_force(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ValueError, match="does not match"):
        _create(repository, requested_date="2026-07-25")

    first = _create(repository)
    with pytest.raises(DuplicatePipelineRunError) as duplicate:
        _create(repository, run_id=FORCED_RUN_ID)
    assert duplicate.value.existing_run_id == first.run.run_id

    forced = _create(repository, run_id=FORCED_RUN_ID, force_refresh=True)
    assert forced.run.force_refresh is True
    assert repository.find_existing_manual_run(date(2026, 7, 26)) is not None
    with pytest.raises(DuplicatePipelineRunError):
        _create(repository, run_id=SECOND_FORCED_RUN_ID)
    second_forced = _create(
        repository,
        run_id=SECOND_FORCED_RUN_ID,
        force_refresh=True,
    )
    assert second_forced.run.force_refresh is True


def test_run_transitions_fail_closed_and_success_requires_terminal_phases(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.SUCCEEDED)
    running = repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    assert running.status is PipelineRunStatus.RUNNING
    with pytest.raises(PipelineRepositoryInvariantError, match="every phase"):
        repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.SUCCEEDED)

    for definition in CANONICAL_PIPELINE_PHASES:
        _complete_phase(repository, RUN_ID, definition.key)
    succeeded = repository.transition_pipeline_run(
        RUN_ID,
        PipelineRunStatus.SUCCEEDED,
        final_summary={"phase_count": 15},
    )
    assert succeeded.status is PipelineRunStatus.SUCCEEDED
    assert succeeded.final_summary == {"phase_count": 15}
    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)


@pytest.mark.parametrize(
    "target",
    (
        PipelineRunStatus.SUCCEEDED,
        PipelineRunStatus.SUCCEEDED_WITH_WARNINGS,
        PipelineRunStatus.DEGRADED,
    ),
)
@pytest.mark.parametrize(
    "blocking_status",
    (
        PipelinePhaseStatus.PENDING,
        PipelinePhaseStatus.RUNNING,
        PipelinePhaseStatus.FAILED,
    ),
)
def test_all_success_like_run_targets_reject_nonterminal_or_failed_phase(
    tmp_path: Path,
    target: PipelineRunStatus,
    blocking_status: PipelinePhaseStatus,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    if blocking_status is PipelinePhaseStatus.RUNNING:
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.HUMAN_REVIEW,
            PipelinePhaseStatus.RUNNING,
        )
    elif blocking_status is PipelinePhaseStatus.FAILED:
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.HUMAN_REVIEW,
            PipelinePhaseStatus.RUNNING,
        )
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.HUMAN_REVIEW,
            PipelinePhaseStatus.FAILED,
            error={"message": "review failed"},
        )

    with pytest.raises(PipelineRepositoryInvariantError, match="every phase"):
        repository.transition_pipeline_run(RUN_ID, target)


def test_failed_phase_retry_and_failed_run_resume_are_explicit(
    tmp_path: Path,
) -> None:
    secret = "private-provider-value"
    repository = _repository(tmp_path, secret_values=(secret,))
    _create(repository)
    repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    started = repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    assert started.attempt_count == 1
    failed = repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.FAILED,
        error={"message": f"Authorization: Bearer {secret}"},
        reason=f"provider token={secret}",
    )
    assert failed.status is PipelinePhaseStatus.FAILED
    assert secret not in json.dumps(failed.error)
    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.DAILY_SLATE,
            PipelinePhaseStatus.RUNNING,
        )
    retried = repository.retry_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        reason="manual retry after correction",
    )
    assert retried.status is PipelinePhaseStatus.RUNNING
    assert retried.attempt_count == 2
    assert retried.completed_at is None
    assert retried.error is None
    assert retried.output_checksum is None
    assert all(
        phase.status is PipelinePhaseStatus.PENDING
        for phase in repository.get_pipeline_run_phases(RUN_ID)[1:]
    )
    phase_transitions = [
        item
        for item in repository.get_pipeline_run_transitions(RUN_ID)
        if item.phase_key is PipelinePhaseKey.DAILY_SLATE
    ]
    failed_transition = next(
        item
        for item in phase_transitions
        if item.to_status == PipelinePhaseStatus.FAILED.value
    )
    assert failed_transition.audit_metadata["error"] is not None
    assert secret not in json.dumps(failed_transition.audit_metadata)
    retry_transition = next(
        item
        for item in phase_transitions
        if item.transition_type is PipelineTransitionType.RETRY
    )
    assert retry_transition.audit_metadata["error"] is None
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.FAILED,
        error={"message": "still failed"},
    )
    failed_run = repository.transition_pipeline_run(
        RUN_ID,
        PipelineRunStatus.FAILED,
        failure_phase=PipelinePhaseKey.DAILY_SLATE,
        error_message=f"api_key={secret}",
    )
    assert failed_run.status is PipelineRunStatus.FAILED
    assert secret not in str(failed_run.error_message)
    with pytest.raises(DuplicatePipelineRunError):
        _create(repository, run_id=FORCED_RUN_ID)
    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    resumed = repository.resume_pipeline_run(RUN_ID, reason="operator-approved resume")
    assert resumed.status is PipelineRunStatus.RUNNING
    assert resumed.failure_phase is None
    assert resumed.error_message is None


def test_frozen_phase_contract_rejects_running_to_skipped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )

    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.DAILY_SLATE,
            PipelinePhaseStatus.SKIPPED,
        )

    phase = repository.get_pipeline_run_phases(RUN_ID)[0]
    assert phase.status is PipelinePhaseStatus.RUNNING
    assert phase.attempt_count == 1


def test_compound_failed_resume_updates_run_and_phase_atomically(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    repository.fail_running_pipeline_phase_and_run(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        error={"message": "synthetic failure"},
        error_message="synthetic failure",
        reason="atomic failure fixture",
    )

    resumed = repository.resume_failed_pipeline_run_and_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        reason="atomic resume fixture",
    )

    assert resumed.run.status is PipelineRunStatus.RUNNING
    assert resumed.run.failure_phase is None
    assert resumed.run.error_message is None
    assert resumed.phases[0].status is PipelinePhaseStatus.RUNNING
    assert resumed.phases[0].attempt_count == 2
    assert resumed.phases[0].error is None


def test_compound_failed_resume_rolls_back_run_if_phase_retry_fails(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    repository.fail_running_pipeline_phase_and_run(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        error={"message": "synthetic failure"},
        error_message="synthetic failure",
        reason="atomic failure fixture",
    )
    with repository.database.connect(write=True) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_reject_phase_retry
            BEFORE UPDATE ON pipeline_run_phases
            WHEN OLD.status='failed' AND NEW.status='running'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic retry failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic retry failure"):
        repository.resume_failed_pipeline_run_and_phase(
            RUN_ID,
            PipelinePhaseKey.DAILY_SLATE,
            reason="must roll back",
        )

    run = repository.get_pipeline_run(RUN_ID)
    phase = repository.get_pipeline_run_phases(RUN_ID)[0]
    assert run is not None
    assert run.status is PipelineRunStatus.FAILED
    assert run.failure_phase is PipelinePhaseKey.DAILY_SLATE
    assert phase.status is PipelinePhaseStatus.FAILED
    assert phase.attempt_count == 1


def test_compound_failure_rolls_back_phase_if_run_failure_fails(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    repository.transition_pipeline_run(RUN_ID, PipelineRunStatus.RUNNING)
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
    )
    with repository.database.connect(write=True) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_reject_run_failure
            BEFORE UPDATE ON pipeline_runs
            WHEN OLD.status='running' AND NEW.status='failed'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic run failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic run failure"):
        repository.fail_running_pipeline_phase_and_run(
            RUN_ID,
            PipelinePhaseKey.DAILY_SLATE,
            error={"message": "must roll back"},
            error_message="must roll back",
            reason="must roll back",
        )

    run = repository.get_pipeline_run(RUN_ID)
    phase = repository.get_pipeline_run_phases(RUN_ID)[0]
    assert run is not None
    assert run.status is PipelineRunStatus.RUNNING
    assert run.failure_phase is None
    assert phase.status is PipelinePhaseStatus.RUNNING
    assert phase.error is None
    assert phase.attempt_count == 1


def test_reuse_and_forced_refresh_require_explicit_valid_source_metadata(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(
        repository,
        requested_date="2026-07-25",
        run_id=SOURCE_RUN_ID,
    )
    repository.transition_pipeline_phase(
        SOURCE_RUN_ID,
        PipelinePhaseKey.DATA_QUALITY,
        PipelinePhaseStatus.RUNNING,
        input_checksum=HASH_A,
    )
    source = repository.transition_pipeline_phase(
        SOURCE_RUN_ID,
        PipelinePhaseKey.DATA_QUALITY,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        output_checksum=HASH_B,
        artifact_relpath="2026-07-25/source/data-quality.json",
        warnings=[{"message": "known degraded input"}],
    )
    _create(repository, run_id=TARGET_RUN_ID)

    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_phase(
            TARGET_RUN_ID,
            PipelinePhaseKey.DATA_QUALITY,
            PipelinePhaseStatus.REUSED,
        )
    with pytest.raises(PipelinePhaseNotFoundError):
        repository.mark_pipeline_phase_reused(
            TARGET_RUN_ID,
            PipelinePhaseKey.DATA_QUALITY,
            reused_from_run_id=(
                "run_20260724_55555555555555555555555555555555"
            ),
            reason="missing source run",
        )
    with pytest.raises(PipelineRepositoryInvariantError, match="not successfully"):
        repository.mark_pipeline_phase_reused(
            TARGET_RUN_ID,
            PipelinePhaseKey.GAME_STATE,
            reused_from_run_id=SOURCE_RUN_ID,
            reason="no source phase evidence",
        )
    reused = repository.mark_pipeline_phase_reused(
        TARGET_RUN_ID,
        PipelinePhaseKey.DATA_QUALITY,
        reused_from_run_id=SOURCE_RUN_ID,
        reason="operator selected retained output",
    )
    assert reused.status is PipelinePhaseStatus.REUSED
    assert reused.reused_from_run_id == SOURCE_RUN_ID
    assert reused.output_checksum == source.output_checksum
    assert reused.artifact_relpath == source.artifact_relpath

    repository.create_pipeline_run(
        requested_date="2026-07-27",
        as_of_time=TS,
        timezone_name="America/Los_Angeles",
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_metadata={"provider": {"enabled": False}},
        run_id=REUSE_TARGET_RUN_ID,
        code_revision="f" * 40,
    )
    reused_again = repository.mark_pipeline_phase_reused(
        REUSE_TARGET_RUN_ID,
        PipelinePhaseKey.DATA_QUALITY,
        reused_from_run_id=TARGET_RUN_ID,
        reason="reuse retained provenance chain",
    )
    assert reused_again.status is PipelinePhaseStatus.REUSED
    assert reused_again.reused_from_run_id == TARGET_RUN_ID

    with pytest.raises(InvalidPipelineTransition):
        repository.transition_pipeline_phase(
            TARGET_RUN_ID,
            PipelinePhaseKey.DATA_QUALITY,
            PipelinePhaseStatus.RUNNING,
        )
    refreshed = repository.force_refresh_pipeline_phase(
        TARGET_RUN_ID,
        PipelinePhaseKey.DATA_QUALITY,
        reason="operator forced recomputation",
    )
    assert refreshed.status is PipelinePhaseStatus.RUNNING
    assert refreshed.attempt_count == 1
    assert refreshed.reused_from_run_id is None
    assert refreshed.output_checksum is None
    assert refreshed.artifact_relpath is None

    repository.transition_pipeline_phase(
        SOURCE_RUN_ID,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.SKIPPED,
    )
    with pytest.raises(PipelineRepositoryInvariantError, match="not successfully"):
        repository.mark_pipeline_phase_reused(
            TARGET_RUN_ID,
            PipelinePhaseKey.GAME_STATE,
            reused_from_run_id=SOURCE_RUN_ID,
            reason="skipped phase has no reusable output",
        )


def test_phase_finalization_and_reuse_status_sets_are_explicit() -> None:
    assert SUCCESSFUL_PHASE_STATUSES == {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
        PipelinePhaseStatus.SKIPPED,
        PipelinePhaseStatus.REUSED,
    }
    assert REUSABLE_PHASE_STATUSES == {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
        PipelinePhaseStatus.REUSED,
    }


def test_forced_refresh_resets_stale_result_state_and_increments_attempt(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    first_started = "2026-07-26T15:01:00+00:00"
    first_completed = "2026-07-26T15:02:00+00:00"
    refreshed_at = "2026-07-26T15:03:00+00:00"
    repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.RUNNING,
        input_checksum=HASH_A,
        transitioned_at=first_started,
    )
    completed = repository.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        output_checksum=HASH_B,
        artifact_relpath="2026-07-26/run/odds-weather.json",
        warnings=[{"message": "retained warning"}],
        transitioned_at=first_completed,
    )
    assert completed.attempt_count == 1

    refreshed = repository.force_refresh_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        reason="operator forced a fresh computation",
        transitioned_at=refreshed_at,
    )

    assert refreshed.status is PipelinePhaseStatus.RUNNING
    assert refreshed.started_at == refreshed_at
    assert refreshed.completed_at is None
    assert refreshed.attempt_count == 2
    assert refreshed.input_checksum == HASH_A
    assert refreshed.output_checksum is None
    assert refreshed.artifact_relpath is None
    assert refreshed.warnings is None
    assert refreshed.error is None
    assert refreshed.reused_from_run_id is None
    transitions = [
        item
        for item in repository.get_pipeline_run_transitions(RUN_ID)
        if item.phase_key is PipelinePhaseKey.ODDS_WEATHER
    ]
    completed_audit = next(
        item
        for item in transitions
        if item.to_status == PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS.value
    )
    assert completed_audit.audit_metadata["output_checksum"] == HASH_B
    assert completed_audit.audit_metadata["artifact_relpath"] is not None
    assert completed_audit.audit_metadata["warnings"] is not None
    assert transitions[-1].transition_type is PipelineTransitionType.FORCED_REFRESH
    assert transitions[-1].audit_metadata["output_checksum"] is None


@pytest.mark.parametrize(
    "artifact_relpath",
    (
        "../escape.json",
        "contained/../escape.json",
        "/absolute/output.json",
        r"C:\private\output.json",
        r"C:drive-relative\output.json",
        r"..\escape.json",
    ),
)
def test_pipeline_artifact_references_reject_unsafe_paths(
    tmp_path: Path,
    artifact_relpath: str,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    with pytest.raises(ValueError):
        repository.transition_pipeline_phase(
            RUN_ID,
            PipelinePhaseKey.DAILY_SLATE,
            PipelinePhaseStatus.RUNNING,
            artifact_relpath=artifact_relpath,
        )


def test_pipeline_artifact_reference_accepts_contained_relative_path() -> None:
    assert (
        validate_artifact_relpath("2026-07-26/run/output.json")
        == "2026-07-26/run/output.json"
    )


def test_configuration_and_phase_metadata_are_canonical_and_redacted(
    tmp_path: Path,
) -> None:
    secret = "configured-secret-value"
    repository = _repository(tmp_path, secret_values=(secret,))
    summary = _create(
        repository,
        configuration_metadata={
            "provider": {
                "enabled": True,
                "api_key": secret,
                "endpoint": f"https://user:{secret}@private.example/path",
                "note": secret,
            }
        },
    )
    serialized = json.dumps(summary.run.configuration_metadata, sort_keys=True)
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    fingerprint_payload = json.dumps(
        {
            "configuration_version": "DSE_DAILY_MLB_CONFIG_V1",
            "metadata": summary.run.configuration_metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert summary.run.configuration_fingerprint == hashlib.sha256(
        fingerprint_payload.encode()
    ).hexdigest()
    with repository.database.connect() as connection:
        raw = connection.execute(
            """
            SELECT configuration_metadata_json FROM pipeline_runs WHERE run_id=?
            """,
            (RUN_ID,),
        ).fetchone()[0]
    assert secret not in raw

    equivalent = _create(
        repository,
        run_id=FORCED_RUN_ID,
        force_refresh=True,
        configuration_metadata={
            "provider": {
                "note": secret,
                "endpoint": f"https://user:{secret}@private.example/path",
                "api_key": secret,
                "enabled": True,
            }
        },
    )
    assert (
        equivalent.run.configuration_fingerprint
        == summary.run.configuration_fingerprint
    )


def test_configuration_rejects_bearer_and_provider_credential_material(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    bearer_fixture = "Bearer " + "fixture-token-value"
    with pytest.raises(ValueError, match="bearer credential material"):
        _create(
            repository,
            configuration_metadata={"provider": {"note": bearer_fixture}},
        )
    api_key_fixture = "github_" + "pat_" + ("x" * 70)
    with pytest.raises(ValueError, match="credential material"):
        _create(
            repository,
            configuration_metadata={"provider": {"note": api_key_fixture}},
        )
    with repository.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0] == 0


def test_phase_key_ordinal_uniqueness_and_transition_audit_are_immutable(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _create(repository)
    with repository.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE pipeline_run_phases SET ordinal=1
                WHERE run_id=? AND phase_key='game_state'
                """,
                (RUN_ID,),
            )
    with repository.database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM pipeline_run_transitions WHERE run_id=?",
                (RUN_ID,),
            )


def _install_formal_v6(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, (history_version, name, checksum) in zip(
            range(1, 7),
            (
                FORMAL_SCHEMA_V1_STATEMENTS,
                FORMAL_SCHEMA_V2_STATEMENTS,
                FORMAL_SCHEMA_V3_STATEMENTS,
                FORMAL_SCHEMA_V4_STATEMENTS,
                FORMAL_SCHEMA_V5_STATEMENTS,
                FORMAL_SCHEMA_V6_STATEMENTS,
            ),
            MIGRATION_HISTORY[:6],
            strict=True,
        ):
            assert version == history_version
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES (?,?,?,?)
                """,
                (version, name, checksum, TS),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V6_FINGERPRINT
    finally:
        connection.close()


def test_v6_upgrades_through_v9_and_empty_database_installs_current_schema(
    tmp_path: Path,
) -> None:
    v6_path = tmp_path / "formal-v6.db"
    _install_formal_v6(v6_path)

    upgraded = ensure_schema(v6_path)
    assert upgraded.version == 9
    assert upgraded.schema_fingerprint == FORMAL_SCHEMA_V9_FINGERPRINT
    assert MIGRATION_HISTORY[5] == (6, MIGRATION_V6_NAME, MIGRATION_V6_CHECKSUM)
    verification = sqlite3.connect(v6_path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 9
        assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        verification.close()

    empty = Database(tmp_path / "empty.db")
    assert empty.schema_info()["version"] == CURRENT_SCHEMA_VERSION
    assert empty.schema_info()["fingerprint"] == FORMAL_SCHEMA_V9_FINGERPRINT
    assert empty.schema_info()["checksum"] == MIGRATION_HISTORY[-1][2]
    assert empty.integrity_check() == {
        "ok": True,
        "integrity_check": ["ok"],
        "foreign_key_violations": [],
    }


def test_v7_collector_run_rebuild_preserves_structure_data_and_dependencies(
    tmp_path: Path,
) -> None:
    path = tmp_path / "collector-v6.db"
    _install_formal_v6(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executemany(
            """
            INSERT INTO collector_runs(
                run_id,requested_date,status,created_at,queued_at,started_at,
                completed_at,updated_at,failure_stage,error_message,
                artifact_relpath,app_version,schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                (
                    RUN_ID,
                    "2026-07-26",
                    "completed",
                    TS,
                    TS,
                    TS,
                    TS,
                    TS,
                    None,
                    None,
                    "2026-07-26/run/artifact.zip",
                    "collector-v6",
                    6,
                ),
                (
                    SOURCE_RUN_ID,
                    "2026-07-25",
                    "failed",
                    TS,
                    TS,
                    TS,
                    TS,
                    TS,
                    "collector",
                    "redacted failure",
                    None,
                    "collector-v6",
                    6,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO games(
                event_id,sport_key,commence_time,home_team,away_team,
                home_team_key,away_team_key,raw_json,first_seen_at,last_seen_at
            ) VALUES (
                'event-v6','baseball_mlb',?,'Home','Away','home','away','{}',?,?
            )
            """,
            (TS, TS, TS),
        )
        connection.execute(
            """
            INSERT INTO run_games(run_id,event_id,associated_at)
            VALUES (?, 'event-v6', ?)
            """,
            (RUN_ID, TS),
        )
        connection.execute(
            """
            INSERT INTO collector_run_transitions(
                run_id,from_status,to_status,failure_stage,error_message,
                transitioned_at
            ) VALUES (?, 'running', 'completed', NULL, NULL, ?)
            """,
            (RUN_ID, TS),
        )
        connection.execute(
            """
            INSERT INTO odds_snapshots(
                run_id,event_id,bookmaker_key,bookmaker_title,market_key,
                market_last_update,outcome_name,price,point,
                bookmaker_last_update,retrieved_at,raw_json
            ) VALUES (
                ?,'event-v6','book','Book','h2h',?,'Home',-110,NULL,?,?, '{}'
            )
            """,
            (RUN_ID, TS, TS, TS),
        )
        connection.execute(
            """
            INSERT INTO weather_snapshots(
                run_id,event_id,provider,forecast_time,temperature_f,
                humidity_pct,precipitation_probability_pct,wind_speed_mph,
                wind_direction_deg,short_forecast,raw_json,retrieved_at
            ) VALUES (
                ?,'event-v6','nws',?,72,40,0,8,270,'Clear','{}',?
            )
            """,
            (RUN_ID, TS, TS),
        )
        connection.commit()
        before_rows = connection.execute(
            "SELECT * FROM collector_runs ORDER BY run_id"
        ).fetchall()
        before_sql = " ".join(
            str(
                connection.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='collector_runs'
                    """
                ).fetchone()[0]
            ).split()
        )
        before_indexes = connection.execute(
            "PRAGMA index_list(collector_runs)"
        ).fetchall()
        before_dependent_fks = sorted(
            (
                str(table_row[0]),
                tuple(foreign_key),
            )
            for table_row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                """
            )
            for foreign_key in connection.execute(
                f"PRAGMA foreign_key_list({table_row[0]})"
            )
            if foreign_key[2] == "collector_runs"
        )
        before_triggers = sorted(
            (str(row[0]), " ".join(str(row[1]).split()))
            for row in connection.execute(
                """
                SELECT name,sql FROM sqlite_master
                WHERE type='trigger' AND sql LIKE '%collector_runs%'
                """
            )
        )
        before_dependents = {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in (
                "collector_run_transitions",
                "run_games",
                "odds_snapshots",
                "weather_snapshots",
            )
        }
    finally:
        connection.close()

    ensure_schema(path)
    verification = sqlite3.connect(path)
    try:
        after_rows = verification.execute(
            "SELECT * FROM collector_runs ORDER BY run_id"
        ).fetchall()
        after_sql = " ".join(
            str(
                verification.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='collector_runs'
                    """
                ).fetchone()[0]
            ).split()
        )
        after_indexes = verification.execute(
            "PRAGMA index_list(collector_runs)"
        ).fetchall()
        after_dependent_fks = sorted(
            (
                str(table_row[0]),
                tuple(foreign_key),
            )
            for table_row in verification.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name NOT LIKE 'sqlite_%'
                """
            )
            for foreign_key in verification.execute(
                f"PRAGMA foreign_key_list({table_row[0]})"
            )
            if foreign_key[2] == "collector_runs"
        )
        after_triggers = sorted(
            (str(row[0]), " ".join(str(row[1]).split()))
            for row in verification.execute(
                """
                SELECT name,sql FROM sqlite_master
                WHERE type='trigger' AND sql LIKE '%collector_runs%'
                """
            )
        )
        after_dependents = {
            table: verification.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in before_dependents
        }
        assert after_rows == before_rows
        assert after_indexes == before_indexes
        assert after_dependent_fks == before_dependent_fks
        assert after_triggers == before_triggers
        assert after_dependents == before_dependents
        compact_before = re.sub(r"\s+", "", before_sql)
        compact_after = re.sub(r"\s+", "", after_sql)
        assert compact_before.replace(
            "schema_versionIN(1,2,3,4,5,6)",
            "schema_versionIN(1,2,3,4,5,6,7,8,9)",
        ) == compact_after
        assert verification.execute(
            "SELECT run_id,event_id,associated_at FROM run_games"
        ).fetchall() == [(RUN_ID, "event-v6", TS)]
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
        assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        verification.close()


def _install_formal_v7(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, (history_version, name, checksum) in zip(
            range(1, 8),
            (
                FORMAL_SCHEMA_V1_STATEMENTS,
                FORMAL_SCHEMA_V2_STATEMENTS,
                FORMAL_SCHEMA_V3_STATEMENTS,
                FORMAL_SCHEMA_V4_STATEMENTS,
                FORMAL_SCHEMA_V5_STATEMENTS,
                FORMAL_SCHEMA_V6_STATEMENTS,
                FORMAL_SCHEMA_V7_STATEMENTS,
            ),
            MIGRATION_HISTORY[:7],
            strict=True,
        ):
            assert version == history_version
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES (?,?,?,?)
                """,
                (version, name, checksum, TS),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V7_FINGERPRINT
    finally:
        connection.close()


def test_v7_through_v9_preserves_pipeline_runs_and_widens_schema_version(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pipeline-v7.db"
    _install_formal_v7(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO pipeline_runs(
                run_id,sport,run_type,requested_date,as_of_time,timezone,
                pipeline_version,configuration_version,
                configuration_fingerprint,configuration_metadata_json,
                code_revision,database_schema_version,force_refresh,status,
                failure_phase,error_message,final_summary_json,created_at,
                started_at,completed_at,updated_at
            ) VALUES (
                ?,'MLB','manual_daily','2026-07-26',?,'America/Los_Angeles',
                'DSE_MANUAL_RUN_CONTROLLER_V1','DSE_DAILY_MLB_CONFIG_V1',
                ?,'{}',?,7,0,'pending',NULL,NULL,NULL,?,NULL,NULL,?
            )
            """,
            (RUN_ID, TS, HASH_A, "f" * 40, TS, TS),
        )
        for ordinal, definition in enumerate(CANONICAL_PIPELINE_PHASES, start=1):
            connection.execute(
                """
                INSERT INTO pipeline_run_phases(
                    run_id,phase_key,ordinal,status,attempt_count,
                    started_at,completed_at,updated_at,input_checksum,
                    output_checksum,artifact_relpath,warnings_json,error_json,
                    reused_from_run_id
                ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, NULL,
                          NULL, NULL, NULL, NULL)
                """,
                (RUN_ID, definition.key.value, ordinal, TS),
            )
        connection.execute(
            """
            UPDATE pipeline_run_phases
            SET status='succeeded',attempt_count=1,started_at=?,completed_at=?,
                input_checksum=?,output_checksum=?,artifact_relpath=?,
                updated_at=?
            WHERE run_id=? AND phase_key='daily_slate'
            """,
            (TS, TS, HASH_A, HASH_A, "daily_slate/example.json", TS, RUN_ID),
        )
        connection.execute(
            """
            UPDATE pipeline_run_phases
            SET status='succeeded_with_warnings',attempt_count=1,started_at=?,
                completed_at=?,warnings_json='["warning"]',updated_at=?
            WHERE run_id=? AND phase_key='game_state'
            """,
            (TS, TS, TS, RUN_ID),
        )
        connection.execute(
            """
            UPDATE pipeline_run_phases
            SET status='degraded',attempt_count=1,started_at=?,completed_at=?,
                warnings_json='["degraded"]',updated_at=?
            WHERE run_id=? AND phase_key='baseball_intelligence_assembly'
            """,
            (TS, TS, TS, RUN_ID),
        )
        connection.execute(
            """
            UPDATE pipeline_run_phases
            SET status='skipped',completed_at=?,updated_at=?
            WHERE run_id=? AND phase_key='odds_weather'
            """,
            (TS, TS, RUN_ID),
        )
        connection.execute(
            """
            UPDATE pipeline_run_phases
            SET status='failed',attempt_count=1,started_at=?,completed_at=?,
                error_json='{"message":"redacted failure"}',updated_at=?
            WHERE run_id=? AND phase_key='data_quality'
            """,
            (TS, TS, TS, RUN_ID),
        )
        connection.executemany(
            """
            INSERT INTO pipeline_run_transitions(
                run_id,phase_key,from_status,to_status,transition_type,reason,
                audit_metadata_json,transitioned_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                (RUN_ID, None, None, "pending", "created", "created", "{}", TS),
                (
                    RUN_ID,
                    "daily_slate",
                    "running",
                    "succeeded",
                    "state_transition",
                    "completed",
                    "{}",
                    TS,
                ),
                (
                    RUN_ID,
                    "odds_weather",
                    "pending",
                    "skipped",
                    "state_transition",
                    "not required",
                    "{}",
                    TS,
                ),
            ),
        )
        connection.commit()
        before_run = connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id=?", (RUN_ID,)
        ).fetchone()
        before_phases = connection.execute(
            "SELECT * FROM pipeline_run_phases WHERE run_id=? ORDER BY ordinal",
            (RUN_ID,),
        ).fetchall()
        before_transitions = connection.execute(
            "SELECT * FROM pipeline_run_transitions ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    result = ensure_schema(path)
    assert result.version == 9
    assert result.schema_fingerprint == FORMAL_SCHEMA_V9_FINGERPRINT
    verification = sqlite3.connect(path)
    try:
        assert verification.execute(
            "SELECT * FROM pipeline_runs WHERE run_id=?", (RUN_ID,)
        ).fetchone() == before_run
        assert verification.execute(
            "SELECT * FROM pipeline_run_phases WHERE run_id=? ORDER BY ordinal",
            (RUN_ID,),
        ).fetchall() == before_phases
        assert verification.execute(
            "SELECT * FROM pipeline_run_transitions ORDER BY id"
        ).fetchall() == before_transitions
        pipeline_sql = "".join(
            str(
                verification.execute(
                    "SELECT sql FROM sqlite_master WHERE name='pipeline_runs'"
                ).fetchone()[0]
            ).split()
        )
        assert "database_schema_versionIN(7,8,9)" in pipeline_sql
        assert {
            str(row[0])
            for row in verification.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name LIKE 'daily_slate_%'
                """
            )
        } == {"daily_slate_snapshots", "daily_slate_games"}
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 9
        assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        verification.close()


def test_code_revision_has_safe_deterministic_fallback(tmp_path: Path) -> None:
    assert resolve_code_revision(tmp_path / "missing") == "unavailable"
