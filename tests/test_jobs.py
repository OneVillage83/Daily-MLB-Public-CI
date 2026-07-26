from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from app.artifacts import ArtifactPaths
from app.config import Settings
from app.database import Database, utc_now
from app.identifiers import generate_run_id
from app.jobs import DuplicateRunExecution, JobRunner, RunExecutionError
from app.pipeline import CollectionResult
from app.run_state import FailureStage, RunStatus

ODDS_SECRET = "test-job-odds-key-not-a-real-credential"
Collection = Callable[[str, date, Settings, Database, ArtifactPaths], CollectionResult]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "phase1.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token="test-job-service-token-not-a-real-credential",
        odds_api_key=ODDS_SECRET,
        openweather_enabled=False,
        openweather_api_key="",
        nws_user_agent="Tests/1.0 (owner@example.test)",
    )


def completed_result(artifacts: ArtifactPaths) -> CollectionResult:
    return CollectionResult(
        status=RunStatus.COMPLETED,
        artifact_relpath=artifacts.archive_path.relative_to(artifacts.root).as_posix(),
        completed_at=utc_now(),
        warning_count=0,
    )


def create_run(database: Database, requested_date: date) -> str:
    run_id = generate_run_id(requested_date)
    database.create_run(run_id, requested_date)
    return run_id


def test_job_runner_is_single_worker_fifo_and_terminal_state_is_queryable(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    run_ids = [create_run(database, requested_date) for _ in range(3)]
    entered_first = threading.Event()
    release_first = threading.Event()
    lock = threading.Lock()
    observed: list[str] = []
    active = 0
    max_active = 0

    def collection(
        run_id: str,
        current_date: date,
        configured_settings: Settings,
        current_database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        nonlocal active, max_active
        del current_date, configured_settings, current_database
        with lock:
            active += 1
            max_active = max(max_active, active)
            observed.append(run_id)
        try:
            if run_id == run_ids[0]:
                entered_first.set()
                assert release_first.wait(timeout=3)
            return completed_result(artifacts)
        finally:
            with lock:
                active -= 1

    runner = JobRunner(settings, collection)
    try:
        submissions = [runner.submit(run_ids[0], requested_date)]
        assert entered_first.wait(timeout=3)
        submissions.extend(
            runner.submit(run_id, requested_date) for run_id in run_ids[1:]
        )
        release_first.set()
        for submission in submissions:
            submission.future.result(timeout=3)
    finally:
        release_first.set()
        runner.shutdown(wait=True)

    assert observed == run_ids
    assert max_active == 1
    for run_id in run_ids:
        row = database.get_run(run_id)
        assert row is not None
        assert row["status"] == RunStatus.COMPLETED.value
        assert row["artifact_relpath"].endswith(f"/{run_id}/artifact.zip")
        transitions = database.get_run_transitions(run_id)
        assert [transition["to_status"] for transition in transitions] == [
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
            RunStatus.COMPLETED.value,
        ]


def test_duplicate_scheduled_run_id_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    run_id = create_run(database, requested_date)
    entered = threading.Event()
    release = threading.Event()

    def collection(
        current_run_id: str,
        current_date: date,
        configured_settings: Settings,
        current_database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del current_run_id, current_date, configured_settings, current_database
        entered.set()
        assert release.wait(timeout=3)
        return completed_result(artifacts)

    runner = JobRunner(settings, collection)
    try:
        submission = runner.submit(run_id, requested_date)
        assert entered.wait(timeout=3)
        with pytest.raises(DuplicateRunExecution, match="already scheduled"):
            runner.submit(run_id, requested_date)
        release.set()
        submission.future.result(timeout=3)
    finally:
        release.set()
        runner.shutdown(wait=True)


def test_submission_failure_records_before_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    run_id = create_run(database, requested_date)
    runner = JobRunner(settings, lambda *args: completed_result(args[-1]))

    def reject_submission(*args: object, **kwargs: object) -> None:
        raise RuntimeError("executor rejected submission")

    monkeypatch.setattr(runner._executor, "submit", reject_submission)
    try:
        with pytest.raises(RuntimeError, match="rejected submission"):
            runner.submit(run_id, requested_date)
    finally:
        runner.shutdown(wait=True)

    row = database.get_run(run_id)
    assert row is not None
    assert row["status"] == RunStatus.FAILED.value
    assert row["failure_stage"] == FailureStage.BEFORE_WORKER_START.value


@pytest.mark.parametrize(
    ("error", "expected_stage"),
    [
        (RunExecutionError(FailureStage.COLLECTOR, "collector failed"), FailureStage.COLLECTOR),
        (
            RunExecutionError(FailureStage.PERSISTENCE, "persistence failed"),
            FailureStage.PERSISTENCE,
        ),
        (
            RunExecutionError(FailureStage.ARTIFACT_GENERATION, "artifact failed"),
            FailureStage.ARTIFACT_GENERATION,
        ),
        (RuntimeError("unexpected worker failure"), FailureStage.WORKER_EXECUTION),
    ],
)
def test_job_runner_records_typed_failure_stage(
    tmp_path: Path,
    error: BaseException,
    expected_stage: FailureStage,
) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    run_id = create_run(database, requested_date)

    def collection(
        current_run_id: str,
        current_date: date,
        configured_settings: Settings,
        current_database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del current_run_id, current_date, configured_settings, current_database, artifacts
        raise error

    runner = JobRunner(settings, collection)
    try:
        runner.execute_now(run_id, requested_date)
    finally:
        runner.shutdown(wait=True)

    row = database.get_run(run_id)
    assert row is not None
    assert row["status"] == RunStatus.FAILED.value
    assert row["failure_stage"] == expected_stage.value
    assert row["completed_at"] is not None


def test_startup_reconciliation_fails_queued_and_running_runs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    queued_run_id = create_run(database, requested_date)
    running_run_id = create_run(database, requested_date)
    database.transition_run(running_run_id, RunStatus.RUNNING)

    def collection(
        run_id: str,
        current_date: date,
        configured_settings: Settings,
        current_database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del run_id, current_date, configured_settings, current_database
        return completed_result(artifacts)

    runner = JobRunner(settings, collection)
    try:
        reconciled = runner.reconcile_startup()
    finally:
        runner.shutdown(wait=True)

    assert reconciled == 2
    for run_id in (queued_run_id, running_run_id):
        row = database.get_run(run_id)
        assert row is not None
        assert row["status"] == RunStatus.FAILED.value
        assert row["failure_stage"] == FailureStage.STARTUP_RECONCILIATION.value
        assert row["completed_at"] is not None


def test_job_failure_is_redacted_in_run_and_diagnostic_record(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    requested_date = date(2030, 1, 2)
    run_id = create_run(database, requested_date)

    def collection(
        current_run_id: str,
        current_date: date,
        configured_settings: Settings,
        current_database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del current_run_id, current_date, configured_settings, current_database, artifacts
        raise RunExecutionError(
            FailureStage.COLLECTOR,
            f"provider error apiKey={ODDS_SECRET} at https://example.test?apiKey={ODDS_SECRET}",
        )

    runner = JobRunner(settings, collection)
    try:
        runner.execute_now(run_id, requested_date)
    finally:
        runner.shutdown(wait=True)

    row = database.get_run(run_id)
    assert row is not None
    assert ODDS_SECRET not in str(row)
    assert "[REDACTED]" in row["error_message"]
    with database.connect() as connection:
        diagnostic = connection.execute(
            "SELECT message, details FROM collector_errors WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert diagnostic is not None
    assert ODDS_SECRET not in str(tuple(diagnostic))
    assert "[REDACTED]" in diagnostic["message"]
