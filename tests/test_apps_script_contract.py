from pathlib import Path


SCRIPT = Path("apps_script/Main.gs")


def test_apps_script_remains_collection_only_and_uses_a_lock() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "LockService.getScriptLock()" in source
    assert "/jobs/daily-collection" in source
    assert "/publish" not in source
    assert "/approve" not in source
    assert "release_candidate" not in source.lower()
    assert "publication" not in source.lower()


def test_apps_script_does_not_echo_service_response_bodies_in_errors() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "Collector start failed: HTTP ' + status + '.'" in source
    assert "Status check failed: HTTP " in source
    assert "Artifact download failed: HTTP " in source
    assert "failed: ' + response.getContentText()" not in source
    assert "failed: ' + artifact.getContentText()" not in source
