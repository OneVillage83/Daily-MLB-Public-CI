from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from app.daily_slate.acquisition import (
    MLB_SCHEDULE_FIXTURE_KEY,
    DailySlateAcquisitionError,
    DailySlateNormalizationError,
    DailySlateWarningCode,
    acquire_mlb_schedule,
    build_mlb_schedule_request,
    normalize_mlb_schedule,
)
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    VenueMappingStatus,
)
from app.stats.contracts import FixtureResponse, StatsProvider
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
RAW_CHECKSUM = "a" * 64


def _game(game_pk: int = 900001) -> dict[str, object]:
    return {
        "gamePk": game_pk,
        "gameDate": "2026-07-27T23:10:00Z",
        "officialDate": "2026-07-27",
        "status": {
            "abstractGameState": "Preview",
            "codedGameState": "S",
            "detailedState": "Scheduled",
            "statusCode": "S",
            "startTimeTBD": False,
        },
        "teams": {
            "away": {
                "team": {
                    "id": 137,
                    "name": "San Francisco Giants",
                }
            },
            "home": {
                "team": {
                    "id": 119,
                    "name": "Los Angeles Dodgers",
                }
            },
        },
        "venue": {
            "id": 22,
            "name": "Dodger Stadium",
        },
        "gameNumber": 1,
        "doubleHeader": "N",
    }


def _payload(*games: dict[str, object]) -> dict[str, object]:
    if not games:
        return {
            "totalGames": 0,
            "dates": [],
        }
    return {
        "totalGames": len(games),
        "dates": [
            {
                "date": "2026-07-27",
                "totalGames": len(games),
                "games": list(games),
            }
        ],
    }


def _normalize(payload: dict[str, object], *, requested_date: str = "2026-07-27"):
    return normalize_mlb_schedule(
        payload,
        requested_date=requested_date,
        as_of_time=NOW,
        observed_at=NOW,
        upstream_checksum=RAW_CHECKSUM,
    )


def test_request_targets_authoritative_mlb_schedule_without_cache() -> None:
    request = build_mlb_schedule_request("2026-07-27", timeout_seconds=17, max_attempts=3)

    assert request.provider is StatsProvider.MLB
    assert request.url == "https://statsapi.mlb.com/api/v1/schedule"
    assert request.endpoint_category == "daily_slate_schedule"
    assert request.params == {
        "sportId": 1,
        "date": "2026-07-27",
        "hydrate": "probablePitcher(note)",
    }
    assert request.headers["Accept"] == "application/json"
    assert request.timeout_seconds == 17
    assert request.max_attempts == 3
    assert request.persistent_cache is False


def test_zero_game_slate_requires_positive_authoritative_zero_evidence() -> None:
    result = _normalize(_payload())

    assert result.slate.games == ()
    assert result.warnings == ()
    assert result.slate.provenance.upstream_checksum == RAW_CHECKSUM


def test_normal_game_uses_frozen_canonical_identity_and_venue_mapping() -> None:
    result = _normalize(_payload(_game()))

    assert result.warnings == ()
    assert len(result.slate.games) == 1
    game = result.slate.games[0]
    assert game.edge_event_id == "edge:mlb:900001"
    assert game.daily_mlb_game_id == "game:mlb:900001"
    assert game.source_game_id == "900001"
    assert game.away_team_id == "SF"
    assert game.home_team_id == "LAD"
    assert game.source_away_team_id == "137"
    assert game.source_home_team_id == "119"
    assert game.venue_mapping_status is VenueMappingStatus.RESOLVED
    assert game.game_status is DailySlateGameStatus.SCHEDULED
    assert game.doubleheader_status is DailySlateDoubleheaderStatus.SINGLE


def test_requested_date_never_overwrites_authoritative_official_date() -> None:
    game = _game()
    game["officialDate"] = "2026-07-28"

    result = _normalize(_payload(game), requested_date="2026-07-27")

    assert result.slate.requested_date == "2026-07-27"
    assert result.slate.games[0].official_date == "2026-07-28"


def test_doubleheader_games_remain_distinct_by_authoritative_gamepk() -> None:
    first = _game(900001)
    second = _game(900002)
    first["doubleHeader"] = "Y"
    second["doubleHeader"] = "Y"
    first["gameNumber"] = 1
    second["gameNumber"] = 2
    second["gameDate"] = "2026-07-28T03:10:00Z"

    result = _normalize(_payload(first, second))

    assert [game.source_game_id for game in result.slate.games] == ["900001", "900002"]
    assert all(
        game.doubleheader_status is DailySlateDoubleheaderStatus.DOUBLEHEADER
        for game in result.slate.games
    )


def test_unknown_status_and_doubleheader_are_preserved_as_warnings() -> None:
    game = _game()
    game["status"] = {
        "abstractGameState": "Mystery",
        "detailedState": "New MLB State",
        "statusCode": "ZZ",
        "startTimeTBD": False,
    }
    game["doubleHeader"] = "Z"

    result = _normalize(_payload(game))

    normalized = result.slate.games[0]
    assert normalized.game_status is DailySlateGameStatus.UNKNOWN
    assert normalized.doubleheader_status is DailySlateDoubleheaderStatus.UNKNOWN
    assert {warning.code for warning in result.warnings} == {
        DailySlateWarningCode.UNKNOWN_GAME_STATUS,
        DailySlateWarningCode.UNKNOWN_DOUBLEHEADER_STATUS,
    }


def test_explicit_start_time_tbd_is_allowed_with_warning() -> None:
    game = _game()
    raw_status = game["status"]
    assert isinstance(raw_status, dict)
    status = dict(raw_status)
    status["startTimeTBD"] = True
    game["status"] = status
    game.pop("gameDate")

    result = _normalize(_payload(game))

    assert result.slate.games[0].scheduled_start_time is None
    assert [warning.code for warning in result.warnings] == [
        DailySlateWarningCode.START_TIME_TBD
    ]


def test_unresolved_venue_is_warning_not_fuzzy_guess() -> None:
    game = _game()
    game["venue"] = {"id": 999999, "name": "Unmapped Neutral Site"}

    result = _normalize(_payload(game))

    normalized = result.slate.games[0]
    assert normalized.venue_id is None
    assert normalized.venue_mapping_status is VenueMappingStatus.UNRESOLVED
    assert normalized.source_venue_id == "999999"
    assert normalized.source_venue_name == "Unmapped Neutral Site"
    assert [warning.code for warning in result.warnings] == [
        DailySlateWarningCode.UNRESOLVED_VENUE
    ]


def test_missing_probable_starter_is_not_a_warning() -> None:
    result = _normalize(_payload(_game()))

    game = result.slate.games[0]
    assert game.away_probable_starter is None
    assert game.home_probable_starter is None
    assert result.warnings == ()


def test_source_probable_starter_is_retained_when_canonical_identity_is_unresolved() -> None:
    game = _game()
    teams = deepcopy(game["teams"])
    assert isinstance(teams, dict)
    away = teams["away"]
    assert isinstance(away, dict)
    away["probablePitcher"] = {"id": 660271, "fullName": "Authoritative Pitcher"}
    game["teams"] = teams

    result = _normalize(_payload(game))

    starter = result.slate.games[0].away_probable_starter
    assert starter is not None
    assert starter.source_player_id == "660271"
    assert starter.full_name == "Authoritative Pitcher"
    assert starter.canonical_player_id is None
    assert starter.player_identity_id is None
    assert [warning.code for warning in result.warnings] == [
        DailySlateWarningCode.UNRESOLVED_PROBABLE_STARTER
    ]


def test_verified_probable_starter_resolver_may_attach_existing_identity() -> None:
    game = _game()
    teams = deepcopy(game["teams"])
    assert isinstance(teams, dict)
    away = teams["away"]
    assert isinstance(away, dict)
    away["probablePitcher"] = {"id": 660271, "fullName": "Authoritative Pitcher"}
    game["teams"] = teams

    result = normalize_mlb_schedule(
        _payload(game),
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        upstream_checksum=RAW_CHECKSUM,
        player_identity_resolver=lambda source_id: (
            ("identity:mlb:660271", "player:canonical:660271")
            if source_id == "660271"
            else None
        ),
    )

    starter = result.slate.games[0].away_probable_starter
    assert starter is not None
    assert starter.player_identity_id == "identity:mlb:660271"
    assert starter.canonical_player_id == "player:canonical:660271"
    assert result.warnings == ()


def test_malformed_optional_probable_starter_is_omitted_with_warning() -> None:
    game = _game()
    teams = deepcopy(game["teams"])
    assert isinstance(teams, dict)
    home = teams["home"]
    assert isinstance(home, dict)
    home["probablePitcher"] = {"id": 12345}
    game["teams"] = teams

    result = _normalize(_payload(game))

    assert result.slate.games[0].home_probable_starter is None
    assert [warning.code for warning in result.warnings] == [
        DailySlateWarningCode.MALFORMED_PROBABLE_STARTER
    ]


def test_partial_authoritative_slate_count_mismatch_fails_closed() -> None:
    payload = _payload(_game())
    payload["totalGames"] = 2

    with pytest.raises(DailySlateNormalizationError, match="totalGames"):
        _normalize(payload)


def test_date_level_count_mismatch_fails_closed() -> None:
    payload = _payload(_game())
    dates = payload["dates"]
    assert isinstance(dates, list)
    assert isinstance(dates[0], dict)
    dates[0]["totalGames"] = 2

    with pytest.raises(DailySlateNormalizationError, match="date count"):
        _normalize(payload)


def test_duplicate_authoritative_game_identity_fails_closed() -> None:
    first = _game(900001)
    duplicate = _game(900001)

    with pytest.raises(DailySlateNormalizationError, match="duplicate authoritative"):
        _normalize(_payload(first, duplicate))


def test_unknown_team_fails_closed_without_fuzzy_guessing() -> None:
    game = _game()
    teams = deepcopy(game["teams"])
    assert isinstance(teams, dict)
    away = teams["away"]
    assert isinstance(away, dict)
    team = away["team"]
    assert isinstance(team, dict)
    team["name"] = "San Fransisco Giunts"
    game["teams"] = teams

    with pytest.raises(Exception, match="does not resolve"):
        _normalize(_payload(game))


def test_fixture_transport_retains_exact_mlb_bytes_before_normalization(tmp_path) -> None:
    body = json.dumps(_payload(_game()), separators=(",", ":"), sort_keys=True).encode()
    raw_store = RawArtifactStore(tmp_path / "raw")
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

    assert raw_store.read_verified(evidence.response.capture) == body
    assert evidence.response.capture.provider is StatsProvider.MLB
    assert evidence.observed_at == NOW
    assert evidence.request.params["date"] == "2026-07-27"
    assert evidence.payload["totalGames"] == 1
    assert len(transport.requests) == 1


def test_invalid_json_is_retained_then_rejected(tmp_path) -> None:
    body = b"{not-json"
    raw_store = RawArtifactStore(tmp_path / "raw")
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

    with pytest.raises(DailySlateAcquisitionError, match="valid UTF-8 JSON"):
        acquire_mlb_schedule(
            transport=transport,
            raw_store=raw_store,
            requested_date="2026-07-27",
        )

    assert len(transport.requests) == 1
    retained = next((tmp_path / "raw" / "mlb" / "daily_slate_schedule").rglob("*.bin"))
    assert retained.read_bytes() == body
