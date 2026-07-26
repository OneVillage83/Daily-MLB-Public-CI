from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.artifacts import ArtifactPaths
from app.config import Settings
from app.database import Database, utc_now
from app.identifiers import generate_run_id
from app.jobs import RunExecutionError
from app.main import create_app
from app.pipeline import CollectionResult
from app.run_state import FailureStage, RunStatus

SERVICE_TOKEN = "test-service-token-not-a-real-credential"
ODDS_SECRET = "test-odds-key-not-a-real-credential"
AUTH_HEADERS = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
Collection = Callable[[str, date, Settings, Database, ArtifactPaths], CollectionResult]


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "phase1.sqlite3",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token=SERVICE_TOKEN,
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


def install_collection(application: object, collection: Collection) -> None:
    application.state.job_runner.collection = collection  # type: ignore[attr-defined]


def wait_for_status(
    client: TestClient,
    run_id: str,
    expected: RunStatus,
    *,
    timeout: float = 3.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{run_id}", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.json()
        if body["status"] == expected.value:
            return body
        time.sleep(0.01)
    pytest.fail(f"Run {run_id} did not reach {expected.value}")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"requested_date": None},
        {"requested_date": 20300102},
        {"requested_date": "2030-1-2"},
        {"requested_date": "2030-01-02T00:00:00Z"},
        {"requested_date": "2030-02-30"},
        {"requested_date": " 2030-01-02"},
    ],
)
def test_start_requires_exact_valid_date(tmp_path: Path, body: dict[str, object]) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json=body,
        )
    assert response.status_code == 422


def test_client_supplied_run_id_is_forbidden(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    with TestClient(application) as client:
        response = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json={"requested_date": "2030-01-02", "run_id": generate_run_id(date(2030, 1, 2))},
        )
    assert response.status_code == 422


def test_start_returns_queued_generated_run_id(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)

    def collection(
        run_id: str,
        requested_date: date,
        configured_settings: Settings,
        database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del run_id, requested_date, configured_settings, database
        return completed_result(artifacts)

    install_collection(application, collection)
    with TestClient(application) as client:
        response = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json={"requested_date": "2030-01-02"},
        )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == RunStatus.QUEUED.value
    assert body["requested_date"] == "2030-01-02"
    assert re.fullmatch(r"run_20300102_[0-9a-f]{32}", body["run_id"])
    assert body["status_url"] == f"/jobs/{body['run_id']}"


@pytest.mark.parametrize("suffix", ["", "/artifact"])
def test_invalid_and_unknown_run_ids(tmp_path: Path, suffix: str) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    unknown = generate_run_id(date(2030, 1, 2))
    with TestClient(application) as client:
        invalid = client.get(f"/jobs/not-a-run{suffix}", headers=AUTH_HEADERS)
        missing = client.get(f"/jobs/{unknown}{suffix}", headers=AUTH_HEADERS)

    assert invalid.status_code == 422
    assert missing.status_code == 404


def test_artifact_before_completion_returns_conflict(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    entered = Event()
    release = Event()

    def collection(
        run_id: str,
        requested_date: date,
        configured_settings: Settings,
        database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del run_id, requested_date, configured_settings, database
        entered.set()
        assert release.wait(timeout=3)
        return completed_result(artifacts)

    install_collection(application, collection)
    with TestClient(application) as client:
        started = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json={"requested_date": "2030-01-02"},
        )
        run_id = started.json()["run_id"]
        assert entered.wait(timeout=3)
        response = client.get(f"/jobs/{run_id}/artifact", headers=AUTH_HEADERS)
        release.set()

    assert response.status_code == 409


def test_completed_contained_artifact_downloads(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    archive_bytes = b"PK\x03\x04test-archive"

    def collection(
        run_id: str,
        requested_date: date,
        configured_settings: Settings,
        database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del run_id, requested_date, configured_settings, database
        artifacts.run_dir.mkdir(parents=True, exist_ok=True)
        artifacts.archive_path.write_bytes(archive_bytes)
        return completed_result(artifacts)

    install_collection(application, collection)
    with TestClient(application) as client:
        started = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json={"requested_date": "2030-01-02"},
        )
        run_id = started.json()["run_id"]
        wait_for_status(client, run_id, RunStatus.COMPLETED)
        response = client.get(f"/jobs/{run_id}/artifact", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.content == archive_bytes
    assert response.headers["content-type"] == "application/zip"


@pytest.mark.parametrize("corrupted_relpath", [None, "../outside.zip"])
def test_missing_or_outside_artifact_is_not_served(
    tmp_path: Path,
    corrupted_relpath: str | None,
) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)
    database: Database = application.state.database
    requested_date = date(2030, 1, 2)
    run_id = generate_run_id(requested_date)
    paths = ArtifactPaths(settings.artifact_dir, requested_date, run_id)
    database.create_run(run_id, requested_date)
    database.transition_run(run_id, RunStatus.RUNNING)
    database.transition_run(
        run_id,
        RunStatus.COMPLETED,
        artifact_relpath=paths.archive_path.relative_to(paths.root).as_posix(),
    )
    if corrupted_relpath is not None:
        with database.connect(write=True) as connection:
            connection.execute(
                "UPDATE collector_runs SET artifact_relpath=? WHERE run_id=?",
                (corrupted_relpath, run_id),
            )

    with TestClient(application) as client:
        response = client.get(f"/jobs/{run_id}/artifact", headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact unavailable"}


def test_status_exposes_sanitized_failure_without_internal_path(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    application = create_app(settings)

    def collection(
        run_id: str,
        requested_date: date,
        configured_settings: Settings,
        database: Database,
        artifacts: ArtifactPaths,
    ) -> CollectionResult:
        del run_id, requested_date, configured_settings, database, artifacts
        raise RunExecutionError(
            FailureStage.COLLECTOR,
            f"provider failed: apiKey={ODDS_SECRET}",
        )

    install_collection(application, collection)
    with TestClient(application) as client:
        started = client.post(
            "/jobs/daily-collection",
            headers=AUTH_HEADERS,
            json={"requested_date": "2030-01-02"},
        )
        body = wait_for_status(client, started.json()["run_id"], RunStatus.FAILED)

    serialized = str(body)
    assert body["failure_stage"] == FailureStage.COLLECTOR.value
    assert body["error_message"] == "provider failed: apiKey=[REDACTED]"
    assert "artifact_relpath" not in body
    assert str(settings.artifact_dir) not in serialized
    assert str(settings.database_path) not in serialized
    assert ODDS_SECRET not in serialized
