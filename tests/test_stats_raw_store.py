from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.stats.contracts import StatsProvider
from app.stats.raw_store import RawArtifactStore, RawStoreError

NOW = datetime(2026, 7, 14, 19, 30, tzinfo=timezone.utc)


def test_raw_store_retains_exact_bytes_checksum_and_safe_metadata(tmp_path: Path) -> None:
    payload = b"game_pk,game_date\n1,2026-07-14\n"
    store = RawArtifactStore(
        tmp_path / "raw", capture_id_factory=lambda: "capture_0001"
    )

    artifact = store.retain_bytes(
        provider=StatsProvider.STATCAST,
        endpoint_category="search_csv",
        payload=payload,
        retrieved_at=NOW,
        content_type="text/csv; charset=utf-8",
    )

    assert store.read_verified(artifact) == payload
    assert artifact.checksum_sha256 == hashlib.sha256(payload).hexdigest()
    metadata = json.loads(artifact.metadata_path.read_text(encoding="utf-8"))
    assert metadata["artifact_relpath"] == artifact.path.relative_to(store.root).as_posix()
    assert metadata["retrieved_at"] == NOW.isoformat()
    serialized = artifact.metadata_path.read_text(encoding="utf-8")
    assert "request_url" not in serialized
    assert "request_headers" not in serialized
    assert "Authorization" not in serialized


def test_raw_store_never_replaces_an_existing_capture(tmp_path: Path) -> None:
    store = RawArtifactStore(
        tmp_path / "raw", capture_id_factory=lambda: "same_capture"
    )
    first = store.retain_bytes(
        provider=StatsProvider.RETROSHEET,
        endpoint_category="regular_season_archive",
        payload=b"first",
        retrieved_at=NOW,
        content_type="application/zip",
    )

    with pytest.raises(RawStoreError, match="collision|already exists"):
        store.retain_bytes(
            provider=StatsProvider.RETROSHEET,
            endpoint_category="regular_season_archive",
            payload=b"first",
            retrieved_at=NOW,
            content_type="application/zip",
        )
    assert first.path.read_bytes() == b"first"


def test_raw_store_removes_partial_file_when_stream_fails(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path / "raw")

    def broken_stream() -> object:
        yield b"partial"
        raise RuntimeError("fixture stream failed")

    with pytest.raises(RuntimeError, match="fixture stream failed"):
        store.retain_stream(
            provider=StatsProvider.STATCAST,
            endpoint_category="search_csv",
            chunks=broken_stream(),  # type: ignore[arg-type]
            retrieved_at=NOW,
            content_type="text/csv",
        )

    assert list((tmp_path / "raw").rglob("*.bin")) == []
    assert list((tmp_path / "raw").rglob("*.part")) == []


def test_raw_store_rejects_unsafe_components_and_parent_checksum(tmp_path: Path) -> None:
    store = RawArtifactStore(tmp_path / "raw")

    with pytest.raises(RawStoreError, match="safe path"):
        store.retain_bytes(
            provider=StatsProvider.STATCAST,
            endpoint_category="../escape",
            payload=b"x",
            retrieved_at=NOW,
            content_type="text/csv",
        )
    with pytest.raises(RawStoreError, match="parent checksum"):
        store.retain_bytes(
            provider=StatsProvider.STATCAST,
            endpoint_category="search_csv",
            payload=b"x",
            retrieved_at=NOW,
            content_type="text/csv",
            parent_checksum_sha256="invalid",
        )
