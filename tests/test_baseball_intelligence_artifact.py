from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.baseball_intelligence.artifact import (
    baseball_intelligence_artifact_relpath,
    write_baseball_intelligence_artifact,
)
from app.baseball_intelligence.contracts import BaseballIntelligenceAssemblyV1


def _empty_assembly(*, source_stats_run_ids: tuple[str, ...] = ()) -> BaseballIntelligenceAssemblyV1:
    return BaseballIntelligenceAssemblyV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc),
        upstream_daily_slate_checksum="a" * 64,
        upstream_game_state_checksum="b" * 64,
        source_stats_run_ids=source_stats_run_ids,
        source_feature_checksums=(),
        games=(),
    )


def test_artifact_is_content_addressed_and_exact_canonical_bytes(tmp_path: Path) -> None:
    assembly = _empty_assembly()

    artifact = write_baseball_intelligence_artifact(assembly, tmp_path)

    assert artifact.relpath == baseball_intelligence_artifact_relpath(assembly)
    assert artifact.relpath == (
        f"baseball_intelligence/snapshots/{assembly.checksum}/"
        "baseball_intelligence_assembly_v1.json"
    )
    path = tmp_path / artifact.relpath
    assert path.read_bytes() == assembly.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(assembly.canonical_json_bytes())


def test_rewriting_same_content_addressed_assembly_is_idempotent(tmp_path: Path) -> None:
    assembly = _empty_assembly()

    first = write_baseball_intelligence_artifact(assembly, tmp_path)
    second = write_baseball_intelligence_artifact(assembly, tmp_path)

    assert first == second
    assert (tmp_path / first.relpath).read_bytes() == assembly.canonical_json_bytes()


def test_short_atomic_temporary_name_supports_long_windows_validation_root(
    tmp_path: Path,
) -> None:
    assembly = _empty_assembly()
    # The final semantic path remains content-addressed; only the same-directory
    # temporary name is short enough not to push Windows over its path limit.
    # This keeps the semantic destination below the Windows limit while the
    # former destination-derived temporary filename would exceed it.
    long_root = tmp_path / ("validation_root_" + "x" * 12)

    artifact = write_baseball_intelligence_artifact(assembly, long_root)

    destination = long_root / artifact.relpath
    assert destination.read_bytes() == assembly.canonical_json_bytes()
    assert not list(destination.parent.glob("*.part"))


def test_interrupted_atomic_write_cleans_short_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assembly = _empty_assembly()

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("fixture replacement failure")

    monkeypatch.setattr("app.exporter.os.replace", fail_replace)
    with pytest.raises(OSError, match="replacement failure"):
        write_baseball_intelligence_artifact(assembly, tmp_path)

    assert not list(tmp_path.rglob("*.part"))


def test_artifact_relpath_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_baseball_intelligence_artifact(
            _empty_assembly(),
            tmp_path,
            relpath="../escape.json",
        )


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    secret = "fixture-secret-12345"
    assembly = _empty_assembly(source_stats_run_ids=(secret,))

    with pytest.raises(ValueError, match="credential-bearing"):
        write_baseball_intelligence_artifact(
            assembly,
            tmp_path,
            secret_values=(secret,),
        )
