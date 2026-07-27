from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.daily_slate.contracts import DailySlateGameStatus
from app.game_state.contracts import (
    GAME_STATE_CONTRACT_VERSION,
    GamedayPersonnelV1,
    GameStateContractError,
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupEntryV1,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)

NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
SLATE_CHECKSUM = "a" * 64
GAME_CHECKSUM = "b" * 64


def _player(player_id: int, name: str) -> GameStatePlayerV1:
    return GameStatePlayerV1(source_player_id=str(player_id), full_name=name)


def _starter(
    certainty: StarterCertainty = StarterCertainty.UNAVAILABLE,
    *,
    player: GameStatePlayerV1 | None = None,
) -> StarterStateV1:
    return StarterStateV1(certainty=certainty, player=player)


def _lineup(
    availability: LineupAvailability = LineupAvailability.UNAVAILABLE,
    *,
    start_player_id: int = 700001,
) -> LineupStateV1:
    if availability is LineupAvailability.UNAVAILABLE:
        return LineupStateV1(availability=availability)
    count = 9 if availability is LineupAvailability.POSTED else 4
    return LineupStateV1(
        availability=availability,
        entries=tuple(
            LineupEntryV1(
                player=_player(start_player_id + index, f"Player {index + 1}"),
                batting_order_slot=index + 1,
            )
            for index in range(count)
        ),
    )


def _personnel(*, available: bool = False, start_player_id: int = 710001) -> GamedayPersonnelV1:
    if not available:
        return GamedayPersonnelV1(available=False)
    return GamedayPersonnelV1(
        available=True,
        batters=(_player(start_player_id, "Active Batter"),),
        pitchers=(_player(start_player_id + 1, "Active Pitcher"),),
        bench=(_player(start_player_id + 2, "Bench Player"),),
        bullpen=(_player(start_player_id + 3, "Bullpen Pitcher"),),
    )


def _team(
    team_id: str,
    source_team_id: int,
    *,
    lineup: LineupStateV1 | None = None,
    starter: StarterStateV1 | None = None,
    personnel: GamedayPersonnelV1 | None = None,
) -> TeamGameStateV1:
    return TeamGameStateV1(
        team_id=team_id,
        source_team_id=str(source_team_id),
        starter=starter or _starter(),
        lineup=lineup or _lineup(),
        personnel=personnel or _personnel(),
    )


def _game(
    game_pk: int = 900001,
    *,
    observed_at: datetime = NOW,
    away: TeamGameStateV1 | None = None,
    home: TeamGameStateV1 | None = None,
) -> GameStateGameV1:
    source_game_id = str(game_pk)
    return GameStateGameV1(
        edge_event_id=f"edge:mlb:{source_game_id}",
        daily_mlb_game_id=f"game:mlb:{source_game_id}",
        source_game_id=source_game_id,
        away_team_id="SF",
        home_team_id="LAD",
        game_status=DailySlateGameStatus.PREGAME,
        away=away or _team("SF", 137),
        home=home or _team("LAD", 119),
        observed_at=observed_at,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=source_game_id,
            observed_at=observed_at,
            source_version="statsapi-game-feed-v1",
            raw_status="Pre-Game",
            upstream_checksum=GAME_CHECKSUM,
        ),
    )


def _state(
    *games: GameStateGameV1,
    observed_at: datetime = NOW,
    secret_values: tuple[str, ...] = (),
) -> GameStateV1:
    return GameStateV1(
        requested_date="2026-07-27",
        as_of_time=NOW - timedelta(minutes=5),
        observed_at=observed_at,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1",
        upstream_daily_slate_checksum=SLATE_CHECKSUM,
        games=games,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=observed_at,
            source_version="statsapi-game-feed-v1",
            raw_status="game_state_snapshot",
            upstream_checksum=SLATE_CHECKSUM,
        ),
        secret_values=secret_values,
    )


def test_zero_game_state_is_valid_and_deterministic() -> None:
    first = _state()
    second = _state()

    assert first.contract_version == GAME_STATE_CONTRACT_VERSION
    assert first.games == ()
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert json.loads(first.canonical_json_bytes())["checksum"] == first.checksum


def test_game_identity_must_derive_from_authoritative_game_pk() -> None:
    game = _game()

    with pytest.raises(GameStateContractError, match="edge_event_id"):
        GameStateGameV1(
            edge_event_id="edge:mlb:999999",
            daily_mlb_game_id=game.daily_mlb_game_id,
            source_game_id=game.source_game_id,
            away_team_id=game.away_team_id,
            home_team_id=game.home_team_id,
            game_status=game.game_status,
            away=game.away,
            home=game.home,
            observed_at=game.observed_at,
            provenance=game.provenance,
        )


def test_unavailable_starter_must_not_claim_player() -> None:
    with pytest.raises(GameStateContractError, match="unavailable starter"):
        StarterStateV1(
            certainty=StarterCertainty.UNAVAILABLE,
            player=_player(660271, "Authoritative Pitcher"),
        )


def test_probable_starter_requires_source_player_identity() -> None:
    with pytest.raises(GameStateContractError, match="require a player"):
        StarterStateV1(certainty=StarterCertainty.PROBABLE)

    starter = StarterStateV1(
        certainty=StarterCertainty.PROBABLE,
        player=_player(660271, "Authoritative Pitcher"),
        source_designation="probablePitcher",
    )
    assert starter.player is not None
    assert starter.player.source_player_id == "660271"


def test_player_canonical_mapping_is_all_or_nothing() -> None:
    with pytest.raises(GameStateContractError, match="requires both"):
        GameStatePlayerV1(
            source_player_id="660271",
            full_name="Mapped Player",
            canonical_player_id="player:canonical:660271",
        )

    player = GameStatePlayerV1(
        source_player_id="660271",
        full_name="Mapped Player",
        player_identity_id="identity:mlb:660271",
        canonical_player_id="player:canonical:660271",
    )
    assert player.resolved is True


def test_posted_lineup_requires_exact_slots_one_through_nine() -> None:
    posted = _lineup(LineupAvailability.POSTED)
    assert [entry.batting_order_slot for entry in posted.entries] == list(range(1, 10))

    with pytest.raises(GameStateContractError, match="nine batting-order slots"):
        LineupStateV1(
            availability=LineupAvailability.POSTED,
            entries=posted.entries[:-1],
        )


def test_partial_lineup_preserves_incomplete_source_evidence() -> None:
    partial = _lineup(LineupAvailability.PARTIAL)
    assert len(partial.entries) == 4

    with pytest.raises(GameStateContractError, match="between one and eight"):
        LineupStateV1(availability=LineupAvailability.PARTIAL)


def test_lineup_rejects_duplicate_player_or_slot() -> None:
    player = _player(700001, "Duplicate Player")
    with pytest.raises(GameStateContractError, match="slots must be unique"):
        LineupStateV1(
            availability=LineupAvailability.PARTIAL,
            entries=(
                LineupEntryV1(player=player, batting_order_slot=1),
                LineupEntryV1(player=_player(700002, "Other"), batting_order_slot=1),
            ),
        )
    with pytest.raises(GameStateContractError, match="player IDs must be unique"):
        LineupStateV1(
            availability=LineupAvailability.PARTIAL,
            entries=(
                LineupEntryV1(player=player, batting_order_slot=1),
                LineupEntryV1(player=player, batting_order_slot=2),
            ),
        )


def test_personnel_availability_is_explicit() -> None:
    with pytest.raises(GameStateContractError, match="at least one source player"):
        GamedayPersonnelV1(available=True)

    with pytest.raises(GameStateContractError, match="must not contain source players"):
        GamedayPersonnelV1(
            available=False,
            bullpen=(_player(700010, "Unexpected Pitcher"),),
        )

    personnel = _personnel(available=True)
    assert personnel.available is True
    assert personnel.bullpen[0].full_name == "Bullpen Pitcher"


def test_personnel_bucket_rejects_duplicate_source_player_ids() -> None:
    player = _player(700010, "Same Player")
    with pytest.raises(GameStateContractError, match="bullpen source player IDs must be unique"):
        GamedayPersonnelV1(
            available=True,
            bullpen=(player, player),
        )


def test_nested_team_identity_must_match_game_identity() -> None:
    with pytest.raises(GameStateContractError, match="nested team game-state identities"):
        _game(away=_team("ARI", 109))


def test_snapshot_rejects_duplicate_authoritative_games() -> None:
    first = _game(900001)
    second = _game(900001)
    with pytest.raises(GameStateContractError, match="duplicate edge_event_id"):
        _state(first, second)


def test_snapshot_provenance_must_reference_daily_slate_checksum() -> None:
    with pytest.raises(GameStateContractError, match="upstream_checksum"):
        GameStateV1(
            requested_date="2026-07-27",
            as_of_time=NOW,
            observed_at=NOW,
            source_authority="mlb",
            source_version="statsapi-game-feed-v1",
            upstream_daily_slate_checksum=SLATE_CHECKSUM,
            games=(),
            provenance=GameStateProvenanceV1(
                source_provider="mlb",
                source_record_id=None,
                observed_at=NOW,
                upstream_checksum="c" * 64,
            ),
        )


def test_snapshot_observed_at_cannot_precede_game_observation() -> None:
    future_game = _game(observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(GameStateContractError, match="at least as recent"):
        _state(future_game, observed_at=NOW)


def test_contract_rejects_configured_secret_material() -> None:
    secret = "fixture-secret-12345"
    player = GameStatePlayerV1(source_player_id="700001", full_name=secret)
    away = _team(
        "SF",
        137,
        starter=StarterStateV1(
            certainty=StarterCertainty.PROBABLE,
            player=player,
        ),
    )
    with pytest.raises(GameStateContractError, match="credential-bearing"):
        _state(_game(away=away), secret_values=(secret,))


def test_source_player_ids_must_be_positive_decimal_mlb_ids() -> None:
    with pytest.raises(GameStateContractError, match="positive decimal"):
        GameStatePlayerV1(source_player_id="player-seven", full_name="Invalid")


def test_team_source_id_must_be_positive_decimal() -> None:
    with pytest.raises(GameStateContractError, match="positive decimal"):
        _team("SF", 0)
