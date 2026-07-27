from __future__ import annotations

import hashlib
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
from app.stats.contracts import FixtureResponse
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
RAW_CHECKSUM = "a" * 64


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


def _transport(
    tmp_path: Path,
    fixtures: dict[str, FixtureResponse],
) -> tuple[RawArtifactStore, FixtureStatsTransport]:
    raw_store = RawArtifactStore(tmp_path / "raw")
    transport = FixtureStatsTransport(raw_store, fixtures, clock=lambda: NOW)
    return raw_store, transport


def test_zero_game_slate_makes_no_provider_requests(tmp_path: Path) -> None:
    raw_store, transport = _transport(tmp_path, {})

    evidence = acquire_mlb_game_state_feeds(
        transport=transport,
        raw_store=raw_store,
        slate=_slate(),
    )

    assert evidence.evidence_by_game == {}
    assert transport.requests == []


def test_batch_acquires_every_daily_slate_game_in_canonical_order(tmp_path: Path) -> None:
    first_body = b'{"gamePk":900001}'
    second_body = b'{"gamePk":900002}'
    raw_store, transport = _transport(
        tmp_path,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=first_body,
                content_type="application/json",
            ),
            mlb_game_feed_fixture_key(900002): FixtureResponse(
                body=second_body,
                content_type="application/json",
            ),
        },
    )
    slate = _slate(_game(900002, 21), _game(900001, 20))

    evidence = acquire_mlb_game_state_feeds(
        transport=transport,
        raw_store=raw_store,
        slate=slate,
    )

    assert tuple(evidence.evidence_by_game) == ("900001", "900002")
    assert [request.fixture_key for request in transport.requests] == [
        mlb_game_feed_fixture_key(900001),
        mlb_game_feed_fixture_key(900002),
    ]
    assert raw_store.read_verified(evidence.evidence_by_game["900001"].response.capture) == first_body
    assert raw_store.read_verified(evidence.evidence_by_game["900002"].response.capture) == second_body
    assert evidence.slate_checksum == slate.checksum


def test_parser_failure_retains_completed_and_failed_raw_lineage(tmp_path: Path) -> None:
    first_body = b'{"gamePk":900001}'
    failed_body = b"{not-json"
    raw_store, transport = _transport(
        tmp_path,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=first_body,
                content_type="application/json",
            ),
            mlb_game_feed_fixture_key(900002): FixtureResponse(
                body=failed_body,
                content_type="application/json",
            ),
        },
    )
    slate = _slate(_game(900001, 20), _game(900002, 21))

    with pytest.raises(GameStateBatchAcquisitionError) as captured:
        acquire_mlb_game_state_feeds(
            transport=transport,
            raw_store=raw_store,
            slate=slate,
        )

    error = captured.value
    assert error.failed_source_game_id == "900002"
    assert tuple(error.completed_evidence) == ("900001",)
    assert len(error.failure_captures) == 1
    assert error.failure_captures[0].checksum_sha256 == hashlib.sha256(failed_body).hexdigest()
    assert raw_store.read_verified(error.failure_captures[0]) == failed_body
    assert error.attempts == 1
    assert error.status_code == 200


def test_transport_failure_retains_completed_and_failed_attempt_capture(tmp_path: Path) -> None:
    first_body = b'{"gamePk":900001}'
    failed_body = b'{"error":"temporary"}'
    raw_store, transport = _transport(
        tmp_path,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=first_body,
                content_type="application/json",
            ),
            mlb_game_feed_fixture_key(900002): FixtureResponse(
                body=failed_body,
                content_type="application/json",
                status_code=503,
            ),
        },
    )
    slate = _slate(_game(900001, 20), _game(900002, 21))

    with pytest.raises(GameStateBatchAcquisitionError) as captured:
        acquire_mlb_game_state_feeds(
            transport=transport,
            raw_store=raw_store,
            slate=slate,
            max_attempts=1,
        )

    error = captured.value
    assert error.failed_source_game_id == "900002"
    assert tuple(error.completed_evidence) == ("900001",)
    assert len(error.failure_captures) == 1
    assert error.failure_captures[0].checksum_sha256 == hashlib.sha256(failed_body).hexdigest()
    assert raw_store.read_verified(error.failure_captures[0]) == failed_body
    assert error.attempts == 1
    assert error.status_code == 503


def test_evidence_mapping_is_immutable(tmp_path: Path) -> None:
    body = b'{"gamePk":900001}'
    raw_store, transport = _transport(
        tmp_path,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
    )
    evidence = acquire_mlb_game_state_feeds(
        transport=transport,
        raw_store=raw_store,
        slate=_slate(_game(900001, 20)),
    )

    with pytest.raises(TypeError):
        evidence.evidence_by_game["900002"] = evidence.evidence_by_game["900001"]  # type: ignore[index]
