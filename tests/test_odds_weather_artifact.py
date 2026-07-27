from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.odds_weather.artifact import (
    odds_weather_artifact_relpath,
    write_odds_weather_artifact,
)
from app.odds_weather.contracts import (
    OddsWeatherV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
)


def _empty_snapshot(
    *,
    warnings: tuple[OddsWeatherWarningV1, ...] = (),
) -> OddsWeatherV1:
    return OddsWeatherV1(
        requested_date="2026-07-27",
        as_of_time=datetime(2026, 7, 27, 14, tzinfo=timezone.utc),
        observed_at=datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc),
        upstream_daily_slate_checksum="a" * 64,
        upstream_baseball_intelligence_checksum="b" * 64,
        source_raw_capture_checksums=(),
        games=(),
        warnings=warnings,
    )


def test_artifact_is_content_addressed_and_exact_canonical_bytes(tmp_path: Path) -> None:
    snapshot = _empty_snapshot()

    artifact = write_odds_weather_artifact(snapshot, tmp_path)

    assert artifact.relpath == odds_weather_artifact_relpath(snapshot)
    assert artifact.relpath == (
        f"odds_weather/snapshots/{snapshot.checksum}/odds_weather_v1.json"
    )
    path = tmp_path / artifact.relpath
    assert path.read_bytes() == snapshot.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(snapshot.canonical_json_bytes())


def test_rewriting_same_content_addressed_snapshot_is_idempotent(tmp_path: Path) -> None:
    snapshot = _empty_snapshot()

    first = write_odds_weather_artifact(snapshot, tmp_path)
    second = write_odds_weather_artifact(snapshot, tmp_path)

    assert first == second
    assert (tmp_path / first.relpath).read_bytes() == snapshot.canonical_json_bytes()


def test_artifact_relpath_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_odds_weather_artifact(
            _empty_snapshot(),
            tmp_path,
            relpath="../escape.json",
        )


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    configured_value = "z9Q3v7Lm2p8K4x6N"
    snapshot = _empty_snapshot(
        warnings=(
            OddsWeatherWarningV1(
                code="fixture_value",
                domain=OddsWeatherWarningDomain.ODDS,
                message=f"accidental configured value: {configured_value}",
            ),
        )
    )

    with pytest.raises(ValueError, match="credential-bearing"):
        write_odds_weather_artifact(
            snapshot,
            tmp_path,
            secret_values=(configured_value,),
        )
