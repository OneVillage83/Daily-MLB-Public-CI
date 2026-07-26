from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from functools import partial
from typing import TYPE_CHECKING

from app.artifacts import ArtifactPaths
from app.config import Settings
from app.database import Database, DatabaseError
from app.identifiers import validate_run_id
from app.redaction import redact_text
from app.run_state import FailureStage, InvalidRunTransition, RunStatus

if TYPE_CHECKING:
    from app.pipeline import CollectionResult

logger = logging.getLogger(__name__)

CollectionCallable = Callable[
    [str, date, Settings, Database, ArtifactPaths], "CollectionResult"
]


class DuplicateRunExecution(RuntimeError):
    pass


class RunExecutionError(RuntimeError):
    def __init__(self, failure_stage: FailureStage, message: str) -> None:
        super().__init__(message)
        self.failure_stage = failure_stage


@dataclass(frozen=True)
class Submission:
    run_id: str
    future: Future[None]


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        collection: CollectionCallable | None = None,
    ) -> None:
        if collection is None:
            from app.pipeline import run_collection

            collection = run_collection
        self.settings = settings
        self.collection = collection
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mlb-collection"
        )
        self._lock = threading.Lock()
        self._active_run_ids: set[str] = set()
        self._closed = False

    def _database(self) -> Database:
        return Database(
            self.settings.database_path,
            busy_timeout_ms=self.settings.sqlite_busy_timeout_ms,
        )

    def reconcile_startup(self) -> int:
        return self._database().reconcile_incomplete_runs(
            error_message="Collection interrupted by service restart"
        )

    def submit(self, run_id: str, requested_date: date) -> Submission:
        normalized_run_id = validate_run_id(run_id, expected_date=requested_date)
        with self._lock:
            if self._closed:
                raise RuntimeError("Job runner is shut down")
            if normalized_run_id in self._active_run_ids:
                raise DuplicateRunExecution(
                    f"Run {normalized_run_id} is already scheduled"
                )
            row = self._database().get_run(normalized_run_id)
            if row is None or row["status"] != RunStatus.QUEUED.value:
                raise DuplicateRunExecution(
                    f"Run {normalized_run_id} is not available for scheduling"
                )
            self._active_run_ids.add(normalized_run_id)
            try:
                future = self._executor.submit(
                    self._execute, normalized_run_id, requested_date
                )
            except Exception as exc:
                self._active_run_ids.discard(normalized_run_id)
                self._fail_run(
                    normalized_run_id,
                    FailureStage.BEFORE_WORKER_START,
                    exc,
                )
                raise
        future.add_done_callback(partial(self._completed, normalized_run_id))
        return Submission(normalized_run_id, future)

    def execute_now(self, run_id: str, requested_date: date) -> None:
        normalized_run_id = validate_run_id(run_id, expected_date=requested_date)
        with self._lock:
            if normalized_run_id in self._active_run_ids:
                raise DuplicateRunExecution(
                    f"Run {normalized_run_id} is already scheduled"
                )
            self._active_run_ids.add(normalized_run_id)
        try:
            self._execute(normalized_run_id, requested_date)
        finally:
            with self._lock:
                self._active_run_ids.discard(normalized_run_id)

    def _completed(self, run_id: str, future: Future[None]) -> None:
        with self._lock:
            self._active_run_ids.discard(run_id)
        unexpected = future.exception()
        if unexpected is not None:
            logger.error(
                "Worker future for run %s escaped its exception boundary: %s",
                run_id,
                redact_text(unexpected, self.settings.credential_values()),
            )

    def _execute(self, run_id: str, requested_date: date) -> None:
        database = self._database()
        artifacts = ArtifactPaths(
            self.settings.artifact_dir, requested_date, run_id
        )
        try:
            database.transition_run(run_id, RunStatus.RUNNING)
            result = self.collection(
                run_id, requested_date, self.settings, database, artifacts
            )
            if result.status not in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_WARNINGS,
            }:
                raise RunExecutionError(
                    FailureStage.WORKER_EXECUTION,
                    f"Collection returned invalid terminal state {result.status}",
                )
            database.transition_run(
                run_id,
                result.status,
                artifact_relpath=result.artifact_relpath,
                transitioned_at=result.completed_at,
            )
        except RunExecutionError as exc:
            self._fail_run(run_id, exc.failure_stage, exc)
        except InvalidRunTransition as exc:
            # A duplicate executor loses the queued->running compare-and-swap and
            # must not change the state owned by the first worker.
            logger.error("Run %s rejected an invalid worker transition: %s", run_id, exc)
        except (DatabaseError, sqlite3.Error) as exc:
            self._fail_run(run_id, FailureStage.PERSISTENCE, exc)
        except Exception as exc:
            self._fail_run(run_id, FailureStage.WORKER_EXECUTION, exc)

    def _fail_run(
        self, run_id: str, failure_stage: FailureStage, error: BaseException
    ) -> None:
        safe_error = redact_text(error, self.settings.credential_values())
        try:
            database = self._database()
            database.transition_run(
                run_id,
                RunStatus.FAILED,
                failure_stage=failure_stage,
                error_message=safe_error,
            )
            try:
                database.add_error(
                    run_id,
                    stage="run",
                    provider=None,
                    message=safe_error,
                    details=None,
                )
            except Exception as diagnostic_error:
                logger.error(
                    "Run %s failed to persist its diagnostic record: %s",
                    run_id,
                    redact_text(
                        diagnostic_error, self.settings.credential_values()
                    ),
                )
        except InvalidRunTransition:
            logger.error(
                "Run %s could not transition to failed because it is already terminal",
                run_id,
            )
        except Exception as persistence_error:
            logger.error(
                "Run %s could not persist failed state (%s): %s",
                run_id,
                failure_stage.value,
                redact_text(persistence_error, self.settings.credential_values()),
            )
        try:
            from app.pipeline import write_failure_manifest

            row = self._database().get_run(run_id)
            if row is None:
                return
            from app.identifiers import parse_requested_date

            requested_date = parse_requested_date(row["requested_date"])
            write_failure_manifest(
                run_id,
                self._database(),
                ArtifactPaths(
                    self.settings.artifact_dir, requested_date, run_id
                ),
                self.settings,
            )
        except Exception as manifest_error:
            logger.error(
                "Run %s could not write its failure manifest: %s",
                run_id,
                redact_text(manifest_error, self.settings.credential_values()),
            )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)
