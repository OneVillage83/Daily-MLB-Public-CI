from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.daily_slate.contracts import DailySlateGameStatus
from app.game_state.artifact import (
    game_state_artifact_relpath,
    write_game_state_artifact,
)
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateContractError,
    GameStateGameV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
SLATE_CHECKSUM = "a" * 64
GAME_CHECKSUM = "b" * 64


def _team(team_id: str, source_team_id: str) -> TeamGameStateV1:
    return TeamGameStateV1(
        team_id=team_id,
        source_team_id=source_team_id,
        starter=StarterStateV1(certainty=StarterCertainty.UNAVAILABLE),
        lineup=LineupStateV1(availability=LineupAvailability.UNAVAILABLE),
        personnel=GamedayPersonnelV1(available=False),
    )


def _state() -> GameStateV1:
    game = GameStateGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        source_game_id="900001",
        away_team_id="SF",
        home_team_id="LAD",
        game_status=DailySlateGameStatus.PREGAME,
        away=_team("SF", "137"),
        home=_team("LAD", "119"),
        observed_at=NOW,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id="900001",
            observed_at=NOW,
            source_version="statsapi-game-feed-v1",
            raw_status="Pre-Game",
            upstream_checksum=GAME_CHECKSUM,
        ),
    )
    return GameStateV1(
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1",
        upstream_daily_slate_checksum=SLATE_CHECKSUM,
        games=(game,),
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=NOW,
            source_version="statsapi-game-feed-v1",
            raw_status="game_state_snapshot",
            upstream_checksum=SLATE_CHECKSUM,
        ),
    )


def test_artifact_is_content_addressed_and_matches_canonical_bytes(tmp_path: Path) -> None:
    state = _state()

    artifact = write_game_state_artifact(state, tmp_path)

    assert artifact.relpath == game_state_artifact_relpath(state)
    assert artifact.relpath == (
        f"game_state/snapshots/{state.checksum}/game_state_v1.json"
    )
    path = tmp_path / artifact.relpath
    assert path.read_bytes() == state.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(state.canonical_json_bytes())


def test_rewriting_same_state_is_idempotent(tmp_path: Path) -> None:
    state = _state()

    first = write_game_state_artifact(state, tmp_path)
    second = write_game_state_artifact(state, tmp_path)

    assert first == second
    assert (tmp_path / first.relpath).read_bytes() == state.canonical_json_bytes()


def test_artifact_path_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_game_state_artifact(_state(), tmp_path, relpath="../escape.json")


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    secret = "fixture-secret-12345"
    state = _state()

    with pytest.raises(GameStateContractError, match="credential-bearing"):
        write_game_state_artifact(
            GameStateV1(
                requested_date=state.requested_date,
                as_of_time=state.as_of_time,
                observed_at=state.observed_at,
                source_authority=state.source_authority,
                source_version=secret,
                upstream_daily_slate_checksum=state.upstream_daily_slate_checksum,
                games=state.games,
                provenance=state.provenance,
            ),
            tmp_path,
            secret_values=(secret,),
        )
