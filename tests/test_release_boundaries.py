import hashlib
from pathlib import Path

import app.config as config_module
from app.config import Settings
from app.main import create_app


def test_http_api_exposes_collection_but_not_review_or_publication(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "collector.db",
            artifact_dir=tmp_path / "artifacts",
            service_auth_token="test-service-token",
            odds_api_key="test-odds-key",
            openweather_enabled=False,
        )
    )
    paths = {str(getattr(route, "path", "")) for route in app.routes}

    assert "/jobs/daily-collection" in paths
    assert not any("approve" in path or "publish" in path for path in paths)


def test_runtime_image_includes_canonical_release_evidence() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    dockerignore = {
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "COPY docs ./docs" in dockerfile
    assert "docs" not in dockerignore
    assert "docs/" not in dockerignore


def test_phase_two_checkpoint_remains_explicitly_paused() -> None:
    checkpoint = Path(
        "docs/PHASE2_LIVE_WEATHER_VALIDATION_CHECKPOINT.md"
    ).read_text(encoding="utf-8")

    assert "Paused, not accepted and not closed" in checkpoint
    assert "Zero NWS requests occurred" in checkpoint
    assert "Zero OpenWeather requests occurred" in checkpoint
    assert "future regular-season MLB club" in checkpoint


def test_phase_two_release_gate_binds_acceptance_to_the_canonical_report(
    tmp_path: Path, monkeypatch,
) -> None:
    report_path = tmp_path / "PHASE2_LIVE_WEATHER_VALIDATION_REPORT.md"
    monkeypatch.setattr(
        config_module, "PHASE2_LIVE_WEATHER_REPORT_PATH", report_path
    )
    assert (
        Settings(
            phase2_live_weather_accepted=False,
            phase2_weather_evidence_sha256="",
        ).phase2_weather_gate_clear()
        is False
    )
    assert (
        Settings(
            phase2_live_weather_accepted=True,
            phase2_weather_evidence_sha256="",
        ).phase2_weather_gate_clear()
        is False
    )
    assert (
        Settings(
            phase2_live_weather_accepted=True,
            phase2_weather_evidence_sha256="a" * 64,
        ).phase2_weather_gate_clear()
        is False
    )
    report_path.write_text("# Phase 2 report\nValidation complete.\n", encoding="utf-8")
    checksum_without_acceptance = hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert not Settings(
        phase2_live_weather_accepted=True,
        phase2_weather_evidence_sha256=checksum_without_acceptance,
    ).phase2_weather_gate_clear()

    report_path.write_text(
        "# Phase 2 report\nPHASE2_RELEASE_GATE: ACCEPTED\n", encoding="utf-8"
    )
    accepted_checksum = hashlib.sha256(report_path.read_bytes()).hexdigest()
    accepted = Settings(
        phase2_live_weather_accepted=True,
        phase2_weather_evidence_sha256=accepted_checksum,
    )
    assert accepted.phase2_weather_gate_clear() is True

    report_path.write_text(
        "# Phase 2 report changed\nPHASE2_RELEASE_GATE: ACCEPTED\n", encoding="utf-8"
    )
    assert accepted.phase2_weather_gate_clear() is False
