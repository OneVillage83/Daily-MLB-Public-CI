from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from app.stats.auto_daily import AutoDailyStatsGapError
from app.stats.pipeline_preflight import _verify_existing_raw_evidence_root
from app.stats.raw_store import RawArtifactStore


class _Result:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, str]]:
        return list(self._rows)


class _Connection:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def execute(self, query: str) -> _Result:
        assert "stats_raw_payload_metadata" in query
        return _Result(self._rows)


class _Database:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    @contextmanager
    def connect(self) -> Iterator[_Connection]:
        yield _Connection(self._rows)


def test_existing_raw_evidence_root_accepts_retained_artifacts(tmp_path: Path) -> None:
    raw_store = RawArtifactStore(tmp_path / "raw")
    relative = Path("statcast") / "statcast_search" / "2026-08-08" / "capture.bin"
    artifact = raw_store.root / relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_bytes(b"fixture")
    artifact.with_suffix(".json").write_text("{}", encoding="utf-8")
    database = _Database(
        [
            {
                "raw_payload_id": "raw:test:1",
                "artifact_relpath": relative.as_posix(),
            }
        ]
    )

    _verify_existing_raw_evidence_root(database, raw_store)  # type: ignore[arg-type]


def test_existing_raw_evidence_root_rejects_split_raw_tree(tmp_path: Path) -> None:
    raw_store = RawArtifactStore(tmp_path / "wrong-raw")
    database = _Database(
        [
            {
                "raw_payload_id": "raw:test:missing",
                "artifact_relpath": (
                    "statcast/statcast_search/2026-08-08/capture.bin"
                ),
            }
        ]
    )

    with pytest.raises(AutoDailyStatsGapError, match="--stats-raw-root"):
        _verify_existing_raw_evidence_root(database, raw_store)  # type: ignore[arg-type]
