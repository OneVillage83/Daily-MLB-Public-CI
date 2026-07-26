from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, field_validator

from app.artifacts import ArtifactPaths, UnsafeArtifactPath, resolve_contained_path
from app.config import Settings, settings
from app.database import Database
from app.identifiers import generate_run_id, parse_requested_date, validate_run_id
from app.jobs import DuplicateRunExecution, JobRunner
from app.redaction import install_redacting_log_filter, redact_value
from app.run_state import RunStatus

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
install_redacting_log_filter(settings.credential_values())
logger = logging.getLogger(__name__)


class CollectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requested_date: date

    @field_validator("requested_date", mode="before")
    @classmethod
    def validate_requested_date(cls, value: object) -> date:
        if not isinstance(value, str):
            raise ValueError("requested_date must be an explicit YYYY-MM-DD string")
        return parse_requested_date(value)


def _public_run(row: dict[str, Any], configured_settings: Settings) -> dict[str, Any]:
    safe = redact_value(row, configured_settings.credential_values())
    safe.pop("artifact_relpath", None)
    if safe.get("status") in {
        RunStatus.COMPLETED.value,
        RunStatus.COMPLETED_WITH_WARNINGS.value,
    }:
        safe["artifact_url"] = f"/jobs/{safe['run_id']}/artifact"
    else:
        safe["artifact_url"] = None
    return safe


def create_app(configured_settings: Settings = settings) -> FastAPI:
    database = Database(
        configured_settings.database_path,
        busy_timeout_ms=configured_settings.sqlite_busy_timeout_ms,
    )
    runner = JobRunner(configured_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runner.reconcile_startup()
        try:
            yield
        finally:
            runner.shutdown(wait=True)

    application = FastAPI(
        title="MLB Phase 1 Collector",
        version=configured_settings.app_version,
        lifespan=lifespan,
    )
    application.state.settings = configured_settings
    application.state.database = database
    application.state.job_runner = runner

    def authorize(authorization: str | None = Header(default=None)) -> None:
        if not configured_settings.service_auth_token:
            raise HTTPException(
                status_code=500, detail="SERVICE_AUTH_TOKEN is not configured"
            )
        if authorization != f"Bearer {configured_settings.service_auth_token}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    def canonical_run_id(run_id: str) -> str:
        try:
            return validate_run_id(run_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid run ID") from exc

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": configured_settings.app_env}

    @application.post(
        "/jobs/daily-collection", dependencies=[Depends(authorize)], status_code=202
    )
    def start_collection(request: CollectionRequest) -> dict[str, Any]:
        configured_settings.validate_for_collection()
        requested_date = request.requested_date
        run_id = generate_run_id(requested_date)
        database.create_run(
            run_id,
            requested_date,
            app_version=configured_settings.app_version,
        )
        try:
            runner.submit(run_id, requested_date)
        except DuplicateRunExecution as exc:
            raise HTTPException(status_code=409, detail="Run is already scheduled") from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"run_id": run_id, "message": "Run could not be scheduled"},
            ) from exc
        return {
            "run_id": run_id,
            "requested_date": requested_date.isoformat(),
            "status": RunStatus.QUEUED.value,
            "status_url": f"/jobs/{run_id}",
            "artifact_url": f"/jobs/{run_id}/artifact",
        }

    @application.get("/jobs/{run_id}", dependencies=[Depends(authorize)])
    def job_status(run_id: str) -> dict[str, Any]:
        canonical = canonical_run_id(run_id)
        row = database.get_run(canonical)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _public_run(row, configured_settings)

    @application.get("/jobs/{run_id}/artifact", dependencies=[Depends(authorize)])
    def job_artifact(run_id: str) -> FileResponse:
        canonical = canonical_run_id(run_id)
        row = database.get_run(canonical)
        if row is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if row["status"] not in {
            RunStatus.COMPLETED.value,
            RunStatus.COMPLETED_WITH_WARNINGS.value,
        }:
            raise HTTPException(
                status_code=409,
                detail=f"Artifact not ready; status={row['status']}",
            )
        try:
            requested_date = parse_requested_date(row["requested_date"])
            paths = ArtifactPaths(
                configured_settings.artifact_dir, requested_date, canonical
            )
            stored_path = resolve_contained_path(
                paths.root, row.get("artifact_relpath") or ""
            )
            if stored_path != paths.archive_path or not stored_path.is_file():
                raise UnsafeArtifactPath("artifact is missing or does not match its run")
        except (OSError, TypeError, ValueError, UnsafeArtifactPath) as exc:
            logger.error("Artifact unavailable for run %s: %s", canonical, exc)
            raise HTTPException(status_code=404, detail="Artifact unavailable") from exc
        return FileResponse(
            stored_path,
            media_type="application/zip",
            filename=f"MLB-Phase1-{requested_date.isoformat()}-{canonical}.zip",
        )

    return application


app = create_app(settings)
db: Database = app.state.database
job_runner: JobRunner = app.state.job_runner
