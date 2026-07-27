from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
)
from app.game_state.acquisition import mlb_game_feed_fixture_key
from app.game_state.batch import (
    GameStateBatchAcquisitionError,
    acquire_mlb_game_state_feeds,
)
from app.game_state.raw_link import (
    GAME_STATE_RAW_LINK_CONTRACT,
    GameStateRawLinkOutcome,
    game_state_raw_link_relpath,
    write_game_state_raw_link,
)
from app.stats.contracts import FixtureResponse
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
RAW_CHECKSUM = "a" * 64
STATE_CHECKSUM = "b" * 64
RUN_ID = "run_20260727_" + ("c" * 32)


def _game(game_pk: int, start_hour: int) -> DailySlateGameV1:
    observed = NOW - timedelta(minutes=10)
    source_id = str(game_pk)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date="2026-07-27",
        scheduled_start_time=datetime(2026, 7, 27, start_hour, tzinfo=timezone.utc),
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id=source_id,
        source_provider="mlb",
        observed_at=observed,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=source_id,
            observed_at=observed,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum=RAW_CHECKSUM,
        ),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    observed = NOW - timedelta(minutes=10)
    return DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=NOW - timedelta(minutes=15),
        observed_at=observed,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=games,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=observed,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum=RAW_CHECKSUM,
        ),
    )


def _acquired(
    artifact_root: Path,
    slate: DailySlateV1,
):
    raw_store = RawArtifactStore(artifact_root / "provider_raw")
    fixtures = {
        mlb_game_feed_fixture_key(game.source_game_id): FixtureResponse(
            body=(f'{{"gamePk":{game.source_game_id}}}').encode(),
            content_type="application/json",
        )
        for game in slate.games
    }
    transport = FixtureStatsTransport(raw_store, fixtures, clock=lambda: NOW)
    evidence = acquire_mlb_game_state_feeds(
        transport=transport,
        raw_store=raw_store,
        slate=slate,
    )
    return evidence, raw_store


def test_normalized_manifest_links_every_game_capture_and_checksum(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    slate = _slate(_game(900001, 20), _game(900002, 21))
    evidence, _ = _acquired(artifact_root, slate)

    relpath = write_game_state_raw_link(
        artifact_root=artifact_root,
        run_id=RUN_ID,
        requested_date="2026-07-27",
        phase_attempt=1,
        upstream_daily_slate_checksum=slate.checksum,
        outcome=GameStateRawLinkOutcome.NORMALIZED,
        evidence_set=evidence,
        game_state_checksum=STATE_CHECKSUM,
    )

    assert relpath == game_state_raw_link_relpath(RUN_ID, 1)
    document = json.loads((artifact_root / relpath).read_text(encoding="utf-8"))
    assert document["contract_version"] == GAME_STATE_RAW_LINK_CONTRACT
    assert document["outcome"] == "normalized"
    assert document["phase_attempt"] == 1
    assert document["upstream_daily_slate_checksum"] == slate.checksum
    assert document["game_state_checksum"] == STATE_CHECKSUM
    assert document["failure"] is None
    assert [game["source_game_id"] for game in document["games"]] == ["900001", "900002"]
    for game in document["games"]:
        assert len(game["captures"]) == 1
        capture = game["captures"][0]
        retained = artifact_root / capture["raw_artifact_relpath"]
        assert retained.exists()
        assert capture["raw_checksum_sha256"] == game["final_raw_checksum_sha256"]


def test_duplicate_manifest_write_fails_without_deleting_original(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    slate = _slate(_game(900001, 20))
    evidence, _ = _acquired(artifact_root, slate)
    kwargs = {
        "artifact_root": artifact_root,
        "run_id": RUN_ID,
        "requested_date": "2026-07-27",
        "phase_attempt": 1,
        "upstream_daily_slate_checksum": slate.checksum,
        "outcome": GameStateRawLinkOutcome.NORMALIZED,
        "evidence_set": evidence,
        "game_state_checksum": STATE_CHECKSUM,
    }
    relpath = write_game_state_raw_link(**kwargs)
    original = (artifact_root / relpath).read_bytes()

    with pytest.raises(FileExistsError):
        write_game_state_raw_link(**kwargs)

    assert (artifact_root / relpath).read_bytes() == original


def test_acquisition_failure_manifest_keeps_completed_and_failed_evidence(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    slate = _slate(_game(900001, 20), _game(900002, 21))
    raw_store = RawArtifactStore(artifact_root / "provider_raw")
    failed_body = b"{not-json"
    transport = FixtureStatsTransport(
        raw_store,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=b'{"gamePk":900001}',
                content_type="application/json",
            ),
            mlb_game_feed_fixture_key(900002): FixtureResponse(
                body=failed_body,
                content_type="application/json",
            ),
        },
        clock=lambda: NOW,
    )
    with pytest.raises(GameStateBatchAcquisitionError) as captured:
        acquire_mlb_game_state_feeds(
            transport=transport,
            raw_store=raw_store,
            slate=slate,
        )

    relpath = write_game_state_raw_link(
        artifact_root=artifact_root,
        run_id=RUN_ID,
        requested_date="2026-07-27",
        phase_attempt=1,
        upstream_daily_slate_checksum=slate.checksum,
        outcome=GameStateRawLinkOutcome.ACQUISITION_FAILED,
        batch_error=captured.value,
    )
    document = json.loads((artifact_root / relpath).read_text(encoding="utf-8"))

    assert document["outcome"] == "acquisition_failed"
    assert document["game_state_checksum"] is None
    assert [game["source_game_id"] for game in document["games"]] == ["900001"]
    failure = document["failure"]
    assert failure["source_game_id"] == "900002"
    assert failure["status_code"] == 200
    assert failure["attempts"] == 1
    assert len(failure["captures"]) == 1
    retained = artifact_root / failure["captures"][0]["raw_artifact_relpath"]
    assert retained.read_bytes() == failed_body


def test_normalization_failure_redacts_error_and_retains_all_feed_evidence(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    slate = _slate(_game(900001, 20))
    evidence, _ = _acquired(artifact_root, slate)
    secret = "fixture-secret-12345"

    relpath = write_game_state_raw_link(
        artifact_root=artifact_root,
        run_id=RUN_ID,
        requested_date="2026-07-27",
        phase_attempt=1,
        upstream_daily_slate_checksum=slate.checksum,
        outcome=GameStateRawLinkOutcome.NORMALIZATION_FAILED,
        evidence_set=evidence,
        normalization_error=RuntimeError(f"normalizer token={secret}"),
        secret_values=(secret,),
    )
    serialized = (artifact_root / relpath).read_text(encoding="utf-8")
    document = json.loads(serialized)

    assert secret not in serialized
    assert document["outcome"] == "normalization_failed"
    assert len(document["games"]) == 1
    assert document["failure"]["source_game_id"] is None
    assert "[REDACTED]" in document["failure"]["message"]


def test_manifest_requires_evidence_set_matching_daily_slate_checksum(tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    slate = _slate(_game(900001, 20))
    evidence, _ = _acquired(artifact_root, slate)

    with pytest.raises(ValueError, match="does not match"):
        write_game_state_raw_link(
            artifact_root=artifact_root,
            run_id=RUN_ID,
            requested_date="2026-07-27",
            phase_attempt=1,
            upstream_daily_slate_checksum="d" * 64,
            outcome=GameStateRawLinkOutcome.NORMALIZED,
            evidence_set=evidence,
            game_state_checksum=STATE_CHECKSUM,
        )
