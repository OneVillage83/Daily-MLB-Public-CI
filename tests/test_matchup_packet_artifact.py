from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.matchup_packet.artifact import (
    matchup_packet_artifact_relpath,
    write_matchup_packet_artifact,
)
from app.matchup_packet.contracts import MatchupPacketV1


def _empty_packet() -> MatchupPacketV1:
    return MatchupPacketV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 27, 14, 20, tzinfo=timezone.utc),
        upstream_daily_slate_checksum="a" * 64,
        upstream_game_state_checksum="b" * 64,
        upstream_baseball_intelligence_checksum="c" * 64,
        upstream_odds_weather_checksum="d" * 64,
        upstream_data_quality_checksum="e" * 64,
        games=(),
    )


def test_artifact_is_content_addressed_and_exact_canonical_bytes(tmp_path: Path) -> None:
    packet = _empty_packet()
    artifact = write_matchup_packet_artifact(packet, tmp_path)
    assert artifact.relpath == matchup_packet_artifact_relpath(packet)
    assert artifact.relpath == (
        f"matchup_packet/snapshots/{packet.checksum}/matchup_packet_v1.json"
    )
    path = tmp_path / artifact.relpath
    assert path.read_bytes() == packet.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(packet.canonical_json_bytes())


def test_rewriting_same_content_addressed_packet_is_idempotent(tmp_path: Path) -> None:
    packet = _empty_packet()
    first = write_matchup_packet_artifact(packet, tmp_path)
    second = write_matchup_packet_artifact(packet, tmp_path)
    assert first == second
    assert (tmp_path / first.relpath).read_bytes() == packet.canonical_json_bytes()


def test_artifact_relpath_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_matchup_packet_artifact(
            _empty_packet(),
            tmp_path,
            relpath="../escape.json",
        )


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential-bearing"):
        write_matchup_packet_artifact(
            _empty_packet(),
            tmp_path,
            secret_values=("2026",),
        )
