from __future__ import annotations

import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from app.artifacts import ARCHIVE_FILENAME, UnsafeArtifactPath, is_link_like, resolve_contained_path


def _temporary_file(parent: Path) -> tuple[int, Path]:
    descriptor, raw_path = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=parent)
    return descriptor, Path(raw_path)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    target = Path(path)
    if is_link_like(target) or is_link_like(target.parent):
        raise UnsafeArtifactPath("artifacts must not be written through symbolic links")
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_link_like(target.parent):
        raise UnsafeArtifactPath("artifact directory must not be a symbolic link")

    descriptor, temporary_path = _temporary_file(target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def write_bytes(path: Path, payload: bytes) -> None:
    """Atomically replace a binary artifact without changing its bytes."""
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    _atomic_write_bytes(path, payload)


def write_json(path: Path, payload: Any) -> None:
    """Atomically replace a JSON artifact after complete serialization."""
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")
    _atomic_write_bytes(path, serialized)


def write_text(path: Path, payload: str) -> None:
    """Atomically replace a UTF-8 text artifact."""
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _zip_sources(run_dir: Path, archive_path: Path) -> list[tuple[Path, Path]]:
    sources: list[tuple[Path, Path]] = []
    for candidate in sorted(run_dir.rglob("*"), key=lambda item: item.as_posix()):
        if is_link_like(candidate):
            raise UnsafeArtifactPath("symbolic links are not allowed in artifact archives")

        resolved = candidate.resolve(strict=True)
        try:
            relative = resolved.relative_to(run_dir)
        except ValueError as exc:
            raise UnsafeArtifactPath("artifact archive source escapes the run directory") from exc

        if (
            resolved == archive_path
            or candidate.name.endswith(".tmp")
            or (
                candidate.name.startswith(".tmp-")
                and candidate.name.endswith(".part")
            )
        ):
            continue
        if candidate.is_dir():
            continue
        if not stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
            raise UnsafeArtifactPath("artifact archives may contain regular files only")
        sources.append((resolved, relative))
    return sources


def create_zip(run_dir: Path) -> Path:
    """Atomically build the canonical ``artifact.zip`` inside a run directory."""
    lexical_run_dir = Path(run_dir)
    if is_link_like(lexical_run_dir):
        raise UnsafeArtifactPath("run directory must not be a symbolic link")
    if not lexical_run_dir.is_dir():
        raise UnsafeArtifactPath("run directory must exist and be a directory")

    resolved_run_dir = lexical_run_dir.resolve(strict=True)
    archive_path = resolve_contained_path(resolved_run_dir, ARCHIVE_FILENAME)
    if is_link_like(archive_path):
        raise UnsafeArtifactPath("archive path must not be a symbolic link")

    sources = _zip_sources(resolved_run_dir, archive_path)
    descriptor, temporary_path = _temporary_file(resolved_run_dir)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, relative in sources:
                archive.write(source, arcname=relative.as_posix())
        with temporary_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, archive_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return archive_path
