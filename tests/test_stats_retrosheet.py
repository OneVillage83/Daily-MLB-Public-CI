from __future__ import annotations

import hashlib
import io
import itertools
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.stats.contracts import FixtureResponse, StatsProviderPayloadError
from app.stats.providers.retrosheet import RETROSHEET_SEVEN_MEMBERS, RetrosheetProvider
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

FIXTURES = Path(__file__).with_name("fixtures") / "stats"
NOW = datetime(2026, 7, 14, 20, tzinfo=timezone.utc)


def archive_bytes(
    *,
    omitted: set[str] | None = None,
    added: dict[str, bytes] | None = None,
    replacements: dict[str, bytes] | None = None,
) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in RETROSHEET_SEVEN_MEMBERS:
            if name in (omitted or set()):
                continue
            payload = (replacements or {}).get(name, (FIXTURES / name).read_bytes())
            archive.writestr(name, payload)
        for name, payload in (added or {}).items():
            archive.writestr(name, payload)
    return target.getvalue()


def provider(tmp_path: Path, payload: bytes) -> tuple[RetrosheetProvider, RawArtifactStore]:
    ids = itertools.count(1)
    store = RawArtifactStore(
        tmp_path / "raw",
        capture_id_factory=lambda: f"capture_{next(ids):04d}",
    )
    transport = FixtureStatsTransport(
        store,
        {
            "retrosheet_regular_season_archive": FixtureResponse(
                payload, "application/zip"
            )
        },
        clock=lambda: NOW,
    )
    return RetrosheetProvider(transport, store), store


def test_exact_seven_file_archive_is_retained_before_csv_parsing(tmp_path: Path) -> None:
    collector, store = provider(tmp_path, archive_bytes())

    dataset = collector.collect_regular_season_major_league()

    assert tuple(dataset.members) == RETROSHEET_SEVEN_MEMBERS
    assert dataset.scope.competition_level == "major_league"
    assert dataset.scope.season_type == "regular_season"
    assert dataset.archive.path.exists()
    assert dataset.archive.checksum_sha256 == hashlib.sha256(
        dataset.archive.path.read_bytes()
    ).hexdigest()
    for name, member in dataset.members.items():
        assert member.raw.parent_checksum_sha256 == dataset.archive.checksum_sha256
        assert store.read_verified(member.raw) == (FIXTURES / name).read_bytes()
    assert [row["gametype"] for row in dataset.members["gameinfo.csv"].normalized_rows] == [
        "regular"
    ]
    assert dataset.members["gameinfo.csv"].row_count == 3
    assert dataset.members["gameinfo.csv"].normalized_row_count == 1
    assert dict(dataset.members["gameinfo.csv"].season_row_counts) == {2026: 3}
    assert dict(dataset.members["allplayers.csv"].season_row_counts) == {2026: 1}
    assert {row["gametype"] for row in dataset.members["gameinfo.csv"].rows} == {
        "regular",
        "worldseries",
        "playoff",
    }
    assert dataset.members["allplayers.csv"].normalized_rows == ()


@pytest.mark.parametrize(
    ("omitted", "added", "message"),
    [
        ({"plays.csv"}, None, "seven-member"),
        (set(), {"readme.txt": b"extra"}, "seven-member"),
        (set(), {"nested/plays.csv": b"duplicate"}, "root-level"),
    ],
)
def test_archive_contract_rejects_missing_extra_and_nested_members(
    tmp_path: Path,
    omitted: set[str],
    added: dict[str, bytes] | None,
    message: str,
) -> None:
    collector, _ = provider(
        tmp_path, archive_bytes(omitted=omitted, added=added)
    )

    with pytest.raises(StatsProviderPayloadError, match=message) as caught:
        collector.collect_regular_season_major_league()

    assert caught.value.capture.path.exists()


def test_duplicate_archive_member_is_rejected(tmp_path: Path) -> None:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        for name in RETROSHEET_SEVEN_MEMBERS:
            archive.writestr(name, (FIXTURES / name).read_bytes())
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("plays.csv", (FIXTURES / "plays.csv").read_bytes())
    collector, _ = provider(tmp_path, target.getvalue())

    with pytest.raises(StatsProviderPayloadError, match="duplicate"):
        collector.collect_regular_season_major_league()


def test_retrosheet_block_page_is_retained_and_rejected(tmp_path: Path) -> None:
    payload = b"<!doctype html><html><body>Access Denied</body></html>"
    collector, _ = provider(tmp_path, payload)

    with pytest.raises(StatsProviderPayloadError, match="block page") as caught:
        collector.collect_regular_season_major_league()

    assert caught.value.capture.path.read_bytes() == payload


def test_all_member_bytes_exist_when_later_csv_parsing_fails(tmp_path: Path) -> None:
    collector, _ = provider(
        tmp_path,
        archive_bytes(replacements={"pitching.csv": b"not,the,required,columns\n1,2,3,4\n"}),
    )

    with pytest.raises(StatsProviderPayloadError, match="missing required columns"):
        collector.collect_regular_season_major_league()

    assert len(list((tmp_path / "raw").rglob("*.bin"))) == 8


def test_team_or_league_codes_never_substitute_for_explicit_game_type(tmp_path: Path) -> None:
    gameinfo = b"gid,visteam,hometeam,gametype\nALN202607140,ALN,NLN,\n"
    collector, _ = provider(
        tmp_path,
        archive_bytes(replacements={"gameinfo.csv": gameinfo}),
    )

    dataset = collector.collect_regular_season_major_league()

    assert dataset.members["gameinfo.csv"].rows[0]["visteam"] == "ALN"
    assert dataset.members["gameinfo.csv"].normalized_rows == ()
