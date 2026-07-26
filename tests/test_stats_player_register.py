from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.stats.contracts import FixtureResponse, StatsProviderPayloadError
from app.stats.providers.player_register import (
    PYBASEBALL_REGISTER_COMMIT_SHA,
    PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
    PYBASEBALL_REGISTER_URL,
    PybaseballPlayerRegisterProvider,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport


NOW = datetime(2026, 7, 14, 18, tzinfo=timezone.utc)
HEADER = "key_retro,key_mlbam,key_bbref,key_fangraphs,name_last,name_first\n"


def _archive(*members: tuple[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return stream.getvalue()


def _provider(tmp_path: Path, payload: bytes) -> PybaseballPlayerRegisterProvider:
    raw_store = RawArtifactStore(tmp_path / "raw")
    transport = FixtureStatsTransport(
        raw_store,
        {
            "pybaseball_player_identifier_register": FixtureResponse(
                payload,
                "application/zip",
            )
        },
        clock=lambda: NOW,
    )
    return PybaseballPlayerRegisterProvider(transport)


def test_register_preserves_raw_before_exact_identifier_parsing(tmp_path: Path) -> None:
    payload = _archive(
        (
            "register-main/data/people-a.csv",
            HEADER
            + "ohtas001,660271,ohtansh01,19755,Ohtani,Shohei\n"
            + ",605400,glasnty01,14374,Glasnow,Tyler\n",
        )
    )

    dataset = _provider(tmp_path, payload).collect()

    assert dataset.raw.path.read_bytes() == payload
    assert dataset.raw.endpoint_category == PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
    assert dataset.source_member_count == 1
    assert dataset.source_row_count == 2
    assert len(dataset.mappings) == 1
    mapping = dataset.mappings[0]
    assert mapping.retrosheet_id == "ohtas001"
    assert mapping.mlbam_id == 660271
    assert mapping.baseball_reference_id == "ohtansh01"
    assert mapping.source_member == "register-main/data/people-a.csv"
    assert mapping.source_row_number == 2


def test_register_resource_is_pinned_to_an_exact_reviewed_commit() -> None:
    assert PYBASEBALL_REGISTER_COMMIT_SHA == (
        "7e23e7dfaff51b3ae72c16393703eda7e5ecad27"
    )
    assert PYBASEBALL_REGISTER_URL == (
        "https://codeload.github.com/chadwickbureau/register/zip/"
        "7e23e7dfaff51b3ae72c16393703eda7e5ecad27"
    )
    assert "/refs/heads/" not in PYBASEBALL_REGISTER_URL
    assert not PYBASEBALL_REGISTER_URL.endswith(("/main", "/master"))
    assert PYBASEBALL_REGISTER_COMMIT_SHA in PYBASEBALL_REGISTER_ENDPOINT_CATEGORY


def test_register_conflicting_exact_identity_fails_closed_after_retention(
    tmp_path: Path,
) -> None:
    payload = _archive(
        (
            "register-main/data/people-a.csv",
            HEADER + "samep001,100,one01,1,Same,Player\n",
        ),
        (
            "register-main/data/people-b.csv",
            HEADER + "samep001,101,two01,2,Same,Player\n",
        ),
    )
    provider = _provider(tmp_path, payload)

    with pytest.raises(StatsProviderPayloadError, match="conflicting") as caught:
        provider.collect()

    assert caught.value.capture.path.read_bytes() == payload


def test_register_invalid_nonblank_mlbam_identity_fails_closed(tmp_path: Path) -> None:
    payload = _archive(
        (
            "register-main/data/people-a.csv",
            HEADER + "badp001,not-an-id,bad01,1,Bad,Player\n",
        )
    )

    with pytest.raises(StatsProviderPayloadError, match="invalid MLBAM"):
        _provider(tmp_path, payload).collect()
