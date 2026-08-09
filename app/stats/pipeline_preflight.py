from __future__ import annotations

from datetime import date
from pathlib import Path

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionMode,
    AcquisitionRequest,
    StatsAcquisitionService,
    build_stats_transport,
    default_stats_user_agent,
)
from app.stats.auto_daily import AutoDailyStatsCoordinator, AutoDailyStatsExecutionV1
from app.stats.raw_store import RawArtifactStore
from app.stats.repository import StatsRepository


def run_pipeline_stats_preflight(
    *,
    database_path: Path,
    raw_root: Path,
    report_path: Path,
    target_date: date,
    mode: AcquisitionMode = AcquisitionMode.LIVE,
    fixture_root: Path | None = None,
) -> AutoDailyStatsExecutionV1:
    """Repair current-season stats gaps, then acquire the requested daily cutoff."""

    database = Database(database_path)
    raw_store = RawArtifactStore(raw_root)
    repository = StatsRepository(database)

    def run_command(command: AcquisitionCommand, requested_date: date):
        # A new transport/service per command keeps request/capture accounting and
        # immutable acquisition-run evidence scoped to exactly one retained run.
        transport = build_stats_transport(
            mode=mode,
            raw_store=raw_store,
            fixture_root=fixture_root,
            user_agent=default_stats_user_agent(),
        )
        service = StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=transport,
            report_path=report_path,
        )
        return service.execute(
            command,
            AcquisitionRequest(
                requested_through_date=requested_date,
                mode=mode,
            ),
        )

    return AutoDailyStatsCoordinator(
        repository=repository,
        run_daily=lambda requested_date: run_command(
            AcquisitionCommand.DAILY,
            requested_date,
        ),
        run_baseline_backfill=lambda requested_date: run_command(
            AcquisitionCommand.BACKFILL_CURRENT,
            requested_date,
        ),
    ).execute(target_date)
