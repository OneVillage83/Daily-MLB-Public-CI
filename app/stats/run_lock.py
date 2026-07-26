from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import TracebackType

from app.stats.transport import (
    StatsLockUnavailableError,
    _ExclusiveFileLock,
)


STATS_RUN_EXECUTION_LOCK_CONTRACT = "DSE_STATS_RUN_EXECUTION_LOCK_V1"
_LOCK_DIRECTORY_NAME = ".dse-stats-run-locks"


class StatsRunLockError(RuntimeError):
    """Base error for acquisition-run execution locking."""


class StatsRunAlreadyExecutingError(StatsRunLockError):
    """The same stats run already has an active OS-level executor lock."""


def stats_run_lock_path(database_path: Path, stats_run_id: str) -> Path:
    """Return the stable lock path for one canonical database/run identity."""

    run_id = str(stats_run_id).strip()
    if not run_id:
        raise StatsRunLockError("stats run execution lock requires a stats_run_id")
    database = Path(database_path).resolve()
    lock_directory = database.parent / _LOCK_DIRECTORY_NAME
    if lock_directory.is_symlink():
        raise StatsRunLockError(
            "stats run execution lock directory must not be a symlink"
        )
    lock_directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    resolved_directory = lock_directory.resolve()
    if resolved_directory.parent != database.parent:
        raise StatsRunLockError(
            "stats run execution lock directory escaped the database directory"
        )
    database_identity = os.path.normcase(str(database))
    identity = f"{database_identity}\0{run_id}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return resolved_directory / f"stats-run-{digest}.lock"


class StatsRunExecutionLock:
    """Fail-fast cross-process lock for one database and stats-run identity."""

    def __init__(self, database_path: Path, stats_run_id: str) -> None:
        self.database_path = Path(database_path)
        self.stats_run_id = str(stats_run_id)
        self.path = stats_run_lock_path(self.database_path, self.stats_run_id)
        self._lock = _ExclusiveFileLock(self.path, blocking=False)

    def __enter__(self) -> StatsRunExecutionLock:
        try:
            self._lock.__enter__()
        except Exception as exc:
            if isinstance(exc, StatsLockUnavailableError):
                raise StatsRunAlreadyExecutingError(
                    "statistics acquisition run is already executing for this database"
                ) from exc
            raise StatsRunLockError(
                "statistics acquisition run lock could not be acquired"
            ) from exc
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.__exit__(exc_type, exc, traceback)
