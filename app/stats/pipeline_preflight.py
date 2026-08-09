from __future__ import annotations

from datetime import date
from pathlib import Path, PurePosixPath

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionMode,
    AcquisitionRequest,
    AcquisitionResult,
    StatsAcquisitionService,
    build_stats_transport,
    default_stats_user_agent,
)
from app.stats.auto_daily import (
    AutoDailyStatsCoordinator,
    AutoDailyStatsExecutionV1,
    AutoDailyStatsGapError,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.repository import StatsRepository


def _verify_existing_raw_evidence_root(
    database: Database,
    raw_store: RawArtifactStore,
) -> None:
    """Refuse to split one retained stats ledger across unrelated raw roots."""

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT raw_payload_id, artifact_relpath "
            "FROM stats_raw_payload_metadata "
            "ORDER BY raw_payload_id"
        ).fetchall()
    if not rows:
        return

    root = raw_store.root.resolve(strict=True)
    missing: list[str] = []
    for row in rows:
        relpath = PurePosixPath(str(row["artifact_relpath"]))
        if relpath.is_absolute() or any(
            part in {"", ".", ".."} for part in relpath.parts
        ):
            raise AutoDailyStatsGapError(
                "retained stats raw evidence contains an invalid artifact path"
            )
        artifact = root.joinpath(*relpath.parts)
        metadata = artifact.with_suffix(".json")
        try:
            artifact_resolved = artifact.resolve(strict=True)
            metadata_resolved = metadata.resolve(strict=True)
        except FileNotFoundError:
            missing.append(str(row["raw_payload_id"]))
            continue
        if (
            artifact.is_symlink()
            or metadata.is_symlink()
            or not artifact_resolved.is_relative_to(root)
            or not metadata_resolved.is_relative_to(root)
            or not artifact_resolved.is_file()
            or not metadata_resolved.is_file()
        ):
            missing.append(str(row["raw_payload_id"]))

    if missing:
        sample = ", ".join(missing[:3])
        suffix = "" if len(missing) <= 3 else f" (+{len(missing) - 3} more)"
        raise AutoDailyStatsGapError(
            "selected stats raw root does not contain the retained raw evidence "
            f"referenced by the database: {sample}{suffix}; pass --stats-raw-root "
            "with the existing append-only raw store"
        )


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
    _verify_existing_raw_evidence_root(database, raw_store)
    repository = StatsRepository(database)

    def run_command(
        command: AcquisitionCommand,
        requested_date: date,
    ) -> AcquisitionResult:
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
