from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.daily_slate.acquisition import (
    MLB_SCHEDULE_FIXTURE_KEY,
    acquire_mlb_schedule,
)
from app.daily_slate.handler import write_daily_slate_raw_link
from app.stats.contracts import FixtureResponse
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
RUN_ID = "run_20260727_" + ("a" * 32)


def test_duplicate_daily_slate_raw_link_does_not_delete_original(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    raw_store = RawArtifactStore(artifact_root / "provider_raw")
    body = b'{"dates":[],"totalGames":0}'
    transport = FixtureStatsTransport(
        raw_store,
        {
            MLB_SCHEDULE_FIXTURE_KEY: FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
        clock=lambda: NOW,
    )
    evidence = acquire_mlb_schedule(
        transport=transport,
        raw_store=raw_store,
        requested_date="2026-07-27",
    )
    captures = evidence.response.attempt_captures or (evidence.response.capture,)

    def write_manifest() -> str:
        return write_daily_slate_raw_link(
            request=evidence.request,
            captures=captures,
            http_attempts=evidence.response.attempts,
            http_status=evidence.response.status_code,
            response_headers=evidence.response.headers,
            evidence_state="normalized",
            artifact_root=artifact_root,
            run_id=RUN_ID,
            requested_date="2026-07-27",
            phase_attempt=1,
            recorded_at=NOW,
        )

    relpath = write_manifest()
    original = (artifact_root / relpath).read_bytes()

    with pytest.raises(FileExistsError):
        write_manifest()

    assert (artifact_root / relpath).read_bytes() == original
