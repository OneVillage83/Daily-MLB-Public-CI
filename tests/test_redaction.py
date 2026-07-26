from __future__ import annotations

import io
import json
import logging
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import requests

from app.config import Settings
from app.database import Database
from app.http import HttpClient, HttpError
from app.identifiers import generate_run_id
from app.jobs import JobRunner, RunExecutionError
from app.redaction import REDACTED, RedactingFilter, redact_text, redact_url, redact_value
from app.run_state import FailureStage


def test_query_string_credentials_are_redacted() -> None:
    url = (
        "https://provider.example/data?apiKey=odds-secret&appid=weather-secret&"
        "access_token=access-secret&regions=us"
    )

    redacted = redact_url(url)

    assert "odds-secret" not in redacted
    assert "weather-secret" not in redacted
    assert "access-secret" not in redacted
    assert "regions=us" in redacted
    assert redacted.count(REDACTED) == 3


def test_error_message_redacts_labels_bearer_and_known_values() -> None:
    known_secret = "opaque-known-secret"
    message = (
        "ODDS_API_KEY=label-secret Authorization: Bearer bearer-secret "
        "url=https://example.test/?appid=query-secret opaque-known-secret"
    )

    redacted = redact_text(message, (known_secret,))

    for secret in ("label-secret", "bearer-secret", "query-secret", known_secret):
        assert secret not in redacted
    assert REDACTED in redacted


def test_provider_identifier_key_can_be_preserved_without_preserving_a_secret() -> None:
    secret = "configured-provider-secret"
    payload = {
        "key": "fanduel",
        "apiKey": secret,
        "nested": {"key": secret},
    }

    redacted = redact_value(
        payload,
        (secret,),
        preserve_field_names=("key",),
    )

    assert redacted["key"] == "fanduel"
    assert redacted["apiKey"] == REDACTED
    assert redacted["nested"]["key"] == REDACTED


def test_http_error_omits_api_key_and_provider_body(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "http-odds-secret"
    response = requests.Response()
    response.status_code = 401
    response.url = f"https://provider.example/data?apiKey={secret}&regions=us"
    response._content = f"provider echoed {secret}".encode()
    client = HttpClient(timeout=1)
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: response)

    with pytest.raises(HttpError) as captured:
        client.get_json(
            "https://provider.example/data",
            params={"apiKey": secret, "regions": "us"},
        )

    rendered = str(captured.value)
    assert secret not in rendered
    assert "provider echoed" not in rendered
    assert REDACTED in rendered


def test_log_filter_redacts_request_url_and_exception_text() -> None:
    api_key = "logged-api-secret"
    service_token = "logged-service-secret"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter((api_key, service_token)))
    logger = logging.getLogger("tests.redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    try:
        raise RuntimeError(
            f"GET https://example.test/data?appid={api_key} Authorization: Bearer {service_token}"
        )
    except RuntimeError:
        logger.exception("provider request failed")

    rendered = stream.getvalue()
    assert api_key not in rendered
    assert service_token not in rendered
    assert REDACTED in rendered


def test_database_error_sink_redacts_message_and_details(tmp_path: Path) -> None:
    database = Database(tmp_path / "redaction.sqlite")
    run_id = generate_run_id(date(2026, 7, 10))
    database.create_run(run_id, date(2026, 7, 10))
    database.add_error(
        run_id,
        "odds",
        "failed https://example.test/?apiKey=collector-secret",
        details="Authorization: Bearer collector-bearer-secret",
    )

    with sqlite3.connect(database.path) as connection:
        row = connection.execute(
            "SELECT message, details FROM collector_errors WHERE run_id=?",
            (run_id,),
        ).fetchone()

    assert row is not None
    combined = " ".join(str(value) for value in row)
    assert "collector-secret" not in combined
    assert "collector-bearer-secret" not in combined
    assert REDACTED in combined


def test_fatal_worker_error_is_redacted_from_manifest_and_status(tmp_path: Path) -> None:
    odds_secret = "fatal-odds-secret"
    service_secret = "fatal-service-secret"
    settings = Settings(
        database_path=tmp_path / "pipeline.sqlite",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token=service_secret,
        odds_api_key=odds_secret,
        openweather_enabled=False,
    )
    database = Database(settings.database_path)
    requested_date = date(2026, 7, 10)
    run_id = generate_run_id(requested_date)
    database.create_run(run_id, requested_date)

    def fail_collection(*args: object, **kwargs: object) -> Any:
        raise RunExecutionError(
            FailureStage.COLLECTOR,
            f"HTTP 401 https://provider.test/?apiKey={odds_secret} "
            f"Authorization: Bearer {service_secret}",
        )

    runner = JobRunner(settings, collection=fail_collection)
    try:
        runner.execute_now(run_id, requested_date)
    finally:
        runner.shutdown(wait=True)

    manifest_path = settings.artifact_dir / "2026-07-10" / run_id / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status = database.get_run(run_id)
    combined = "\n".join((json.dumps(manifest), json.dumps(status)))
    assert odds_secret not in combined
    assert service_secret not in combined
    assert REDACTED in combined
    assert manifest["failure_stage"] == FailureStage.COLLECTOR.value
    assert status is not None
    assert status["failure_stage"] == FailureStage.COLLECTOR.value


def test_api_status_boundary_redacts_legacy_error() -> None:
    from app import main

    service_secret = main.settings.service_auth_token
    run_id = generate_run_id(date(2026, 7, 10))
    legacy_row = {
        "run_id": run_id,
        "status": "failed",
        "failure_stage": FailureStage.WORKER_EXECUTION.value,
        "artifact_relpath": None,
        "error_message": (
            f"https://provider.test/?apiKey=legacy-query-secret "
            f"Authorization: Bearer {service_secret}"
        ),
    }
    response = main._public_run(legacy_row, main.settings)
    rendered = json.dumps(response)
    assert "legacy-query-secret" not in rendered
    assert service_secret not in rendered
    assert REDACTED in rendered


def test_settings_repr_excludes_credentials(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "settings.sqlite",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token="repr-service-secret",
        odds_api_key="repr-odds-secret",
        openweather_api_key="repr-weather-secret",
    )

    rendered = repr(settings)
    assert "repr-service-secret" not in rendered
    assert "repr-odds-secret" not in rendered
    assert "repr-weather-secret" not in rendered
