from __future__ import annotations

import hashlib
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
from app.game_state.acquisition import (
    GameStateAcquisitionError,
    GameStateNormalizationError,
    GameStateWarningCode,
    MLB_GAME_FEED_FIXTURE_PREFIX,
    MLB_GAME_FEED_SOURCE_VERSION,
    acquire_mlb_game_feed,
    build_mlb_game_feed_request,
    mlb_game_feed_fixture_key,
    normalize_mlb_game_state,
)
from app.game_state.contracts import LineupAvailability, StarterCertainty
from app.stats.contracts import FixtureResponse, StatsProvider
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
SLATE_RAW_CHECKSUM = "a" * 64


def _slate_game(
    game_pk: int = 900001,
    *,
    start: datetime | None = None,
) -> DailySlateGameV1:
    observed = NOW - timedelta(minutes=10)
    source_id = str(game_pk)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date="2026-07-27",
        scheduled_start_time=start or datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc),
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
            upstream_checksum=SLATE_RAW_CHECKSUM,
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
            upstream_checksum=SLATE_RAW_CHECKSUM,
        ),
    )


def _player_record(player_id: int, name: str, *, position_code: str = "7") -> dict[str, object]:
    return {
        "person": {"id": player_id, "fullName": name},
        "position": {"code": position_code, "name": "Position"},
        "status": {"code": "A", "description": "Active"},
        "gameStatus": {
            "isCurrentBatter": False,
            "isCurrentPitcher": False,
            "isOnBench": False,
            "isSubstitute": False,
        },
        "stats": {"pitching": {}},
    }


def _team_box(
    *,
    source_team_id: int,
    batting_ids: list[int],
    starter_id: int,
    bench_id: int,
    bullpen_id: int,
    confirmed_starter: bool,
) -> dict[str, object]:
    player_ids = list(dict.fromkeys([*batting_ids, starter_id, bench_id, bullpen_id]))
    players = {
        f"ID{player_id}": _player_record(player_id, f"Player {player_id}")
        for player_id in player_ids
    }
    starter = players[f"ID{starter_id}"]
    starter["position"] = {"code": "1", "name": "Pitcher"}
    starter["stats"] = {
        "pitching": {
            "gamesStarted": 1 if confirmed_starter else 0,
            "gamesPitched": 1 if confirmed_starter else 0,
        }
    }
    return {
        "team": {"id": source_team_id},
        "batters": batting_ids,
        "pitchers": [starter_id],
        "bench": [bench_id],
        "bullpen": [bullpen_id],
        "battingOrder": batting_ids,
        "players": players,
    }


def _feed(
    game_pk: int = 900001,
    *,
    status: str = "Preview",
    away_confirmed: bool = False,
    home_confirmed: bool = False,
    probable_away_id: int | None = 710001,
    probable_home_id: int | None = 720001,
    away_team_id: int = 137,
    home_team_id: int = 119,
    away_order_count: int = 9,
    include_boxscore: bool = True,
) -> dict[str, object]:
    away_order = [700000 + index for index in range(1, away_order_count + 1)]
    home_order = [730000 + index for index in range(1, 10)]
    away_starter = 710001
    home_starter = 720001
    away_box = _team_box(
        source_team_id=away_team_id,
        batting_ids=away_order,
        starter_id=away_starter,
        bench_id=710010,
        bullpen_id=710020,
        confirmed_starter=away_confirmed,
    )
    home_box = _team_box(
        source_team_id=home_team_id,
        batting_ids=home_order,
        starter_id=home_starter,
        bench_id=720010,
        bullpen_id=720020,
        confirmed_starter=home_confirmed,
    )
    away_box_players = away_box["players"]
    home_box_players = home_box["players"]
    assert isinstance(away_box_players, dict)
    assert isinstance(home_box_players, dict)
    away_player_keys = set(away_box_players)

    all_players: dict[str, object] = {}
    for team_players in (away_box_players, home_box_players):
        for key, record in team_players.items():
            assert isinstance(record, dict)
            person = record["person"]
            assert isinstance(person, dict)
            position = record["position"]
            assert isinstance(position, dict)
            status_record = record["status"]
            assert isinstance(status_record, dict)
            all_players[key] = {
                "id": person["id"],
                "fullName": person["fullName"],
                "primaryPosition": dict(position),
                "active": True,
                "currentTeam": {
                    "id": away_team_id if key in away_player_keys else home_team_id
                },
                "status": dict(status_record),
            }

    probable: dict[str, object] = {}
    if probable_away_id is not None:
        probable["away"] = {
            "id": probable_away_id,
            "fullName": f"Player {probable_away_id}",
        }
    if probable_home_id is not None:
        probable["home"] = {
            "id": probable_home_id,
            "fullName": f"Player {probable_home_id}",
        }

    payload: dict[str, object] = {
        "gamePk": game_pk,
        "gameData": {
            "status": {
                "abstractGameState": "Final" if status == "Final" else "Preview",
                "detailedState": status,
                "statusCode": "F" if status == "Final" else "S",
            },
            "teams": {
                "away": {"id": away_team_id, "name": "San Francisco Giants"},
                "home": {"id": home_team_id, "name": "Los Angeles Dodgers"},
            },
            "players": all_players,
            "probablePitchers": probable,
        },
        "liveData": {},
    }
    if include_boxscore:
        payload["liveData"] = {
            "boxscore": {
                "teams": {"away": away_box, "home": home_box},
            }
        }
    return payload


def _evidence(
    tmp_path: Path,
    payload: dict[str, object],
    *,
    game_pk: int = 900001,
):
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    raw_store = RawArtifactStore(tmp_path / f"raw-{game_pk}")
    transport = FixtureStatsTransport(
        raw_store,
        {
            mlb_game_feed_fixture_key(game_pk): FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
        clock=lambda: NOW,
    )
    evidence = acquire_mlb_game_feed(
        transport=transport,
        raw_store=raw_store,
        source_game_id=game_pk,
    )
    return evidence, raw_store, body, transport


def test_request_uses_authoritative_v11_game_feed_without_cache() -> None:
    request = build_mlb_game_feed_request(900001, timeout_seconds=17, max_attempts=4)

    assert request.provider is StatsProvider.MLB
    assert request.url == "https://statsapi.mlb.com/api/v1.1/game/900001/feed/live"
    assert request.endpoint_category == "game_state_feed"
    assert request.fixture_key == f"{MLB_GAME_FEED_FIXTURE_PREFIX}:900001"
    assert request.timeout_seconds == 17
    assert request.max_attempts == 4
    assert request.persistent_cache is False


def test_fixture_acquisition_retains_exact_feed_bytes(tmp_path: Path) -> None:
    evidence, raw_store, body, transport = _evidence(tmp_path, _feed())

    assert raw_store.read_verified(evidence.response.capture) == body
    assert evidence.raw_checksum == hashlib.sha256(body).hexdigest()
    assert evidence.source_game_id == "900001"
    assert evidence.response.capture.provider is StatsProvider.MLB
    assert evidence.observed_at == NOW
    assert len(transport.requests) == 1


def test_invalid_json_is_retained_then_rejected(tmp_path: Path) -> None:
    body = b"{not-json"
    raw_store = RawArtifactStore(tmp_path / "raw")
    transport = FixtureStatsTransport(
        raw_store,
        {
            mlb_game_feed_fixture_key(900001): FixtureResponse(
                body=body,
                content_type="application/json",
            )
        },
        clock=lambda: NOW,
    )

    with pytest.raises(GameStateAcquisitionError, match="valid UTF-8 JSON"):
        acquire_mlb_game_feed(
            transport=transport,
            raw_store=raw_store,
            source_game_id=900001,
        )

    retained = next((tmp_path / "raw" / "mlb" / "game_state_feed").rglob("*.bin"))
    assert retained.read_bytes() == body


def test_normal_game_retains_probable_starters_and_posted_lineups(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(tmp_path, _feed())

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
    )

    game = result.state.games[0]
    assert game.edge_event_id == slate.games[0].edge_event_id
    assert game.daily_mlb_game_id == slate.games[0].daily_mlb_game_id
    assert game.away.source_team_id == "137"
    assert game.home.source_team_id == "119"
    assert game.away.starter.certainty is StarterCertainty.PROBABLE
    assert game.home.starter.certainty is StarterCertainty.PROBABLE
    assert game.away.lineup.availability is LineupAvailability.POSTED
    assert game.home.lineup.availability is LineupAvailability.POSTED
    assert [entry.batting_order_slot for entry in game.away.lineup.entries] == list(range(1, 10))
    assert game.away.personnel.available is True
    assert len(game.away.personnel.bullpen) == 1
    assert result.state.upstream_daily_slate_checksum == slate.checksum
    assert result.state.source_version == MLB_GAME_FEED_SOURCE_VERSION
    assert result.warnings == ()


def test_explicit_games_started_confirms_starter_even_when_probable_remains(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(
        tmp_path,
        _feed(status="Final", away_confirmed=True, home_confirmed=True),
    )

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
    )

    game = result.state.games[0]
    assert game.game_status is DailySlateGameStatus.FINAL
    assert game.away.starter.certainty is StarterCertainty.CONFIRMED
    assert game.home.starter.certainty is StarterCertainty.CONFIRMED
    assert game.away.starter.source_designation == "boxscore.stats.pitching.gamesStarted=1"
    assert result.warnings == ()


def test_confirmed_starter_different_from_probable_is_retained_with_warning(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    payload = _feed(
        status="Final",
        away_confirmed=True,
        probable_away_id=700001,
    )
    evidence, _, _, _ = _evidence(tmp_path, payload)

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
    )

    game = result.state.games[0]
    assert game.away.starter.certainty is StarterCertainty.CONFIRMED
    assert game.away.starter.player is not None
    assert game.away.starter.player.source_player_id == "710001"
    assert [warning.code for warning in result.warnings] == [
        GameStateWarningCode.STARTER_PROBABLE_CHANGED
    ]


def test_partial_lineup_is_preserved_as_warning_not_promoted_to_posted(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(tmp_path, _feed(away_order_count=5))

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
    )

    assert result.state.games[0].away.lineup.availability is LineupAvailability.PARTIAL
    assert len(result.state.games[0].away.lineup.entries) == 5
    assert [warning.code for warning in result.warnings] == [
        GameStateWarningCode.LINEUP_PARTIAL
    ]


def test_missing_boxscore_preserves_core_game_with_optional_warnings(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(tmp_path, _feed(include_boxscore=False))

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
    )

    game = result.state.games[0]
    assert game.away.starter.certainty is StarterCertainty.PROBABLE
    assert game.away.lineup.availability is LineupAvailability.UNAVAILABLE
    assert game.away.personnel.available is False
    codes = [warning.code for warning in result.warnings]
    assert codes.count(GameStateWarningCode.LINEUP_UNAVAILABLE) == 2
    assert codes.count(GameStateWarningCode.PERSONNEL_UNAVAILABLE) == 2


def test_gamepk_mismatch_fails_closed(tmp_path: Path) -> None:
    slate = _slate(_slate_game(900001))
    evidence, _, _, _ = _evidence(tmp_path, _feed(game_pk=900002), game_pk=900001)

    with pytest.raises(GameStateNormalizationError, match="gamePk disagrees"):
        normalize_mlb_game_state(
            slate=slate,
            evidence_by_game={"900001": evidence},
        )


def test_source_team_mismatch_fails_closed(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(tmp_path, _feed(away_team_id=109))

    with pytest.raises(GameStateNormalizationError, match="away team ID disagrees"):
        normalize_mlb_game_state(
            slate=slate,
            evidence_by_game={"900001": evidence},
        )


def test_evidence_set_must_exactly_equal_daily_slate_games(tmp_path: Path) -> None:
    first = _slate_game(900001, start=datetime(2026, 7, 27, 20, tzinfo=timezone.utc))
    second = _slate_game(900002, start=datetime(2026, 7, 27, 21, tzinfo=timezone.utc))
    slate = _slate(first, second)
    evidence, _, _, _ = _evidence(tmp_path, _feed(game_pk=900001), game_pk=900001)

    with pytest.raises(GameStateNormalizationError, match="exactly match"):
        normalize_mlb_game_state(
            slate=slate,
            evidence_by_game={"900001": evidence},
        )


def test_zero_game_slate_produces_zero_game_state_without_evidence() -> None:
    slate = _slate()

    result = normalize_mlb_game_state(slate=slate, evidence_by_game={})

    assert result.state.games == ()
    assert result.state.requested_date == slate.requested_date
    assert result.state.observed_at == slate.observed_at
    assert result.state.upstream_daily_slate_checksum == slate.checksum
    assert result.warnings == ()


def test_verified_identity_resolver_attaches_canonical_player_ids(tmp_path: Path) -> None:
    slate = _slate(_slate_game())
    evidence, _, _, _ = _evidence(tmp_path, _feed())

    result = normalize_mlb_game_state(
        slate=slate,
        evidence_by_game={"900001": evidence},
        player_identity_resolver=lambda source_id: (
            f"identity:mlb:{source_id}",
            f"player:canonical:{source_id}",
        ),
    )

    game = result.state.games[0]
    assert game.away.starter.player is not None
    assert game.away.starter.player.resolved is True
    assert all(entry.player.resolved for entry in game.away.lineup.entries)
    assert result.warnings == ()
