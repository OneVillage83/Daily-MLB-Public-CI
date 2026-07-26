from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.artifacts import (
    ARCHIVE_FILENAME,
    FIXED_JSON_FILENAMES,
    ArtifactPaths,
    UnsafeArtifactPath,
    resolve_contained_path,
)
from app.exporter import create_zip, write_bytes, write_json


UUID_HEX = "0123456789abcdef" * 2
RUN_ID = f"run_20260711_{UUID_HEX}"
CHECKSUM = "a" * 64


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are not supported in this environment: {exc}")


def test_artifact_paths_are_canonical_and_contained(tmp_path: Path) -> None:
    paths = ArtifactPaths(tmp_path / "artifacts", date(2026, 7, 11), RUN_ID)

    assert paths.root == (tmp_path / "artifacts").resolve()
    assert paths.date_dir == paths.root / "2026-07-11"
    assert paths.run_dir == paths.date_dir / RUN_ID
    assert paths.archive_path == paths.run_dir / ARCHIVE_FILENAME
    assert paths.archive_path.is_relative_to(paths.root)


def test_artifact_paths_require_run_id_date_match(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not match"):
        ArtifactPaths(tmp_path, date(2026, 7, 12), RUN_ID)


@pytest.mark.parametrize("filename", sorted(FIXED_JSON_FILENAMES))
def test_json_path_accepts_only_fixed_contract_names(tmp_path: Path, filename: str) -> None:
    paths = ArtifactPaths(tmp_path, date(2026, 7, 11), RUN_ID)

    assert paths.json_path(filename) == paths.run_dir / filename


@pytest.mark.parametrize(
    "filename",
    [
        "extra.json",
        "../games.json",
        "subdir/games.json",
        "games.json%2f..",
        "C:\\games.json",
        "\\\\server\\share\\games.json",
        "/tmp/games.json",
    ],
)
def test_json_path_rejects_arbitrary_or_unsafe_names(tmp_path: Path, filename: str) -> None:
    paths = ArtifactPaths(tmp_path, date(2026, 7, 11), RUN_ID)

    with pytest.raises(UnsafeArtifactPath):
        paths.json_path(filename)


def test_raw_json_path_uses_provider_category_and_checksum(tmp_path: Path) -> None:
    paths = ArtifactPaths(tmp_path, date(2026, 7, 11), RUN_ID)

    assert paths.raw_json_path("the_odds_api", "game_odds", CHECKSUM) == (
        paths.run_dir / "raw" / "the_odds_api" / "game_odds" / f"{CHECKSUM}.json"
    )


@pytest.mark.parametrize(
    ("provider", "category"),
    [
        ("", "odds"),
        ("The_Odds_API", "odds"),
        ("the-odds-api", "odds"),
        ("../odds", "odds"),
        ("odds/api", "odds"),
        ("odds%2fapi", "odds"),
        ("odds", ""),
        ("odds", "Game_Odds"),
        ("odds", "game-odds"),
        ("odds", "../game_odds"),
        ("odds", "game/odds"),
        ("odds", "game%2fodds"),
    ],
)
def test_raw_json_path_rejects_malformed_segments(tmp_path: Path, provider: str, category: str) -> None:
    paths = ArtifactPaths(tmp_path, date(2026, 7, 11), RUN_ID)

    with pytest.raises(UnsafeArtifactPath):
        paths.raw_json_path(provider, category, CHECKSUM)


@pytest.mark.parametrize(
    "checksum",
    ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "../" + "a" * 64, "%61" * 64],
)
def test_raw_json_path_rejects_invalid_checksum(tmp_path: Path, checksum: str) -> None:
    paths = ArtifactPaths(tmp_path, date(2026, 7, 11), RUN_ID)

    with pytest.raises(UnsafeArtifactPath):
        paths.raw_json_path("nws", "forecast_hourly", checksum)


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.json",
        "subdir/../../outside.json",
        "..\\outside.json",
        "%2e%2e%2foutside.json",
        "/tmp/outside.json",
        "C:\\outside.json",
        "C:outside.json",
        "\\\\server\\share\\outside.json",
    ],
)
def test_resolve_contained_path_rejects_escape_forms(tmp_path: Path, relative_path: str) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()

    with pytest.raises(UnsafeArtifactPath):
        resolve_contained_path(root, relative_path)


def test_resolve_contained_path_rejects_sibling_prefix_escape(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    sibling = tmp_path / "artifacts_backup"
    root.mkdir()
    sibling.mkdir()
    assert str(sibling).startswith(str(root))

    with pytest.raises(UnsafeArtifactPath):
        resolve_contained_path(root, Path("..") / sibling.name / "stolen.json")


def test_resolve_contained_path_rejects_symlink_even_when_target_is_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    target = root / "real"
    link = root / "linked"
    target.mkdir(parents=True)
    _symlink_or_skip(link, target, target_is_directory=True)

    with pytest.raises(UnsafeArtifactPath, match="symbolic links"):
        resolve_contained_path(root, Path("linked") / "file.json")


def test_resolve_contained_path_rejects_symlink_to_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    link = root / "linked"
    root.mkdir()
    outside.mkdir()
    _symlink_or_skip(link, outside, target_is_directory=True)

    with pytest.raises(UnsafeArtifactPath, match="symbolic links"):
        resolve_contained_path(root, Path("linked") / "file.json")


def test_write_json_atomically_replaces_utf8_content(tmp_path: Path) -> None:
    target = tmp_path / "collection_manifest.json"
    target.write_text('{"old": true}', encoding="utf-8")

    write_json(target, {"name": "Jos\u00e9", "status": "completed"})

    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "Jos\u00e9", "status": "completed"}
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_json_serialization_failure_preserves_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "collection_manifest.json"
    original = b'{"old": true}'
    target.write_bytes(original)
    circular: dict[str, Any] = {}
    circular["self"] = circular

    with pytest.raises(ValueError, match="Circular reference"):
        write_json(target, circular)

    assert target.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_bytes_preserves_exact_payload(tmp_path: Path) -> None:
    target = tmp_path / "raw.json"
    payload = b'{"source":"provider","spacing":  true}\r\n'

    write_bytes(target, payload)

    assert target.read_bytes() == payload
    assert list(tmp_path.glob(".*.tmp")) == []


def test_write_json_rejects_symlink_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("original", encoding="utf-8")
    link = tmp_path / "collection_manifest.json"
    _symlink_or_skip(link, outside, target_is_directory=False)

    with pytest.raises(UnsafeArtifactPath, match="symbolic links"):
        write_json(link, {"unsafe": True})

    assert outside.read_text(encoding="utf-8") == "original"


def test_create_zip_contains_regular_files_and_excludes_archive_and_temps(tmp_path: Path) -> None:
    run_dir = tmp_path / RUN_ID
    raw_dir = run_dir / "raw" / "nws" / "forecast_hourly"
    raw_dir.mkdir(parents=True)
    (run_dir / "collection_manifest.json").write_text('{"status":"completed"}', encoding="utf-8")
    (raw_dir / f"{CHECKSUM}.json").write_bytes(b"{}")
    (run_dir / ".collection_manifest.json.interrupted.tmp").write_text("partial", encoding="utf-8")
    (run_dir / ARCHIVE_FILENAME).write_bytes(b"old archive")

    archive_path = create_zip(run_dir)

    assert archive_path == run_dir.resolve() / ARCHIVE_FILENAME
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [
            "collection_manifest.json",
            f"raw/nws/forecast_hourly/{CHECKSUM}.json",
        ]


def test_create_zip_rejects_symlink_source(tmp_path: Path) -> None:
    run_dir = tmp_path / RUN_ID
    outside = tmp_path / "outside.json"
    run_dir.mkdir()
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(run_dir / "outside.json", outside, target_is_directory=False)

    with pytest.raises(UnsafeArtifactPath, match="symbolic links"):
        create_zip(run_dir)


def test_create_zip_rejects_symlink_run_directory(tmp_path: Path) -> None:
    real_run_dir = tmp_path / "real_run"
    linked_run_dir = tmp_path / RUN_ID
    real_run_dir.mkdir()
    _symlink_or_skip(linked_run_dir, real_run_dir, target_is_directory=True)

    with pytest.raises(UnsafeArtifactPath, match="symbolic link"):
        create_zip(linked_run_dir)
