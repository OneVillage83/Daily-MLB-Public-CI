from __future__ import annotations

import io
import json
import multiprocessing
import time
from datetime import date
from pathlib import Path
from typing import Protocol

import pytest

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionExecutionError,
    AcquisitionMode,
    AcquisitionRequest,
    StatsAcquisitionService,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.run_lock import (
    StatsRunAlreadyExecutingError,
    StatsRunExecutionLock,
    stats_run_lock_path,
)
from app.stats.transport import FixtureStatsTransport
from scripts.stats_acquisition import EXIT_OPERATIONAL_FAILURE, main


class _Event(Protocol):
    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def _hold_run_lock(
    database_path: str,
    stats_run_id: str,
    acquired: _Event,
    release: _Event,
) -> None:
    with StatsRunExecutionLock(Path(database_path), stats_run_id):
        acquired.set()
        release.wait(timeout=30)


def _service(root: Path) -> tuple[StatsAcquisitionService, Database]:
    database = Database(root / "stats.db")
    raw_store = RawArtifactStore(root / "raw")
    return (
        StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=FixtureStatsTransport(raw_store, {}),
            report_path=root / "report.json",
        ),
        database,
    )


def test_run_lock_is_fail_fast_cross_process_and_released_after_termination(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stats.db"
    database_path.touch()
    stats_run_id = "stats_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_run_lock,
        args=(str(database_path), stats_run_id, acquired, release),
    )
    process.start()
    assert acquired.wait(timeout=10)

    started = time.monotonic()
    with pytest.raises(
        StatsRunAlreadyExecutingError,
        match="already executing for this database",
    ):
        with StatsRunExecutionLock(database_path, stats_run_id):
            raise AssertionError("contended lock must not be entered")
    assert time.monotonic() - started < 2

    process.terminate()
    process.join(timeout=10)
    assert not process.is_alive()

    with StatsRunExecutionLock(database_path, stats_run_id):
        assert stats_run_lock_path(database_path, stats_run_id).is_file()


def test_contended_resume_fails_nonzero_without_terminalizing_running_run(
    tmp_path: Path,
) -> None:
    service, database = _service(tmp_path)
    request = AcquisitionRequest(
        requested_through_date=date(2026, 7, 16),
        mode=AcquisitionMode.OFFLINE,
    )
    context = service._begin_run(AcquisitionCommand.DAILY, request)
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    stdout = io.StringIO()
    stderr = io.StringIO()

    with StatsRunExecutionLock(database.path, context.stats_run_id):
        exit_code = main(
            [
                "daily",
                "--date",
                "2026-07-16",
                "--database",
                str(database.path),
                "--raw-root",
                str(tmp_path / "raw"),
                "--report",
                str(tmp_path / "report.json"),
                "--mode",
                "offline",
                "--fixture-root",
                str(fixture_root),
                "--resume-run-id",
                context.stats_run_id,
            ],
            stdout=stdout,
            stderr=stderr,
        )

    assert exit_code == EXIT_OPERATIONAL_FAILURE
    assert stdout.getvalue() == ""
    payload = json.loads(stderr.getvalue())
    assert payload == {
        "error_type": AcquisitionExecutionError.__name__,
        "exit_code": EXIT_OPERATIONAL_FAILURE,
        "message": (
            "statistics acquisition run is already executing for this database"
        ),
        "run_id": context.run_id,
        "stats_run_id": context.stats_run_id,
        "status": "failed",
    }
    collection_run = database.get_run(context.run_id)
    assert collection_run is not None
    assert collection_run["status"] == "running"
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status,failure_stage,error_json FROM stats_ingestion_runs "
            "WHERE stats_run_id=?",
            (context.stats_run_id,),
        ).fetchone()
    assert row is not None
    assert dict(row) == {
        "status": "running",
        "failure_stage": None,
        "error_json": None,
    }
