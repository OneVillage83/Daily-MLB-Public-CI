from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.stats.retrosheet_normalization import (
    RetrosheetGame,
    RetrosheetNormalizationError,
    normalize_retrosheet_game_row,
    normalize_retrosheet_members,
    normalize_retrosheet_team_game_row,
    parse_retrosheet_game_id,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "stats" / "retrosheet_normalization"
MEMBERS = (
    "allplayers.csv",
    "gameinfo.csv",
    "teamstats.csv",
    "batting.csv",
    "pitching.csv",
    "fielding.csv",
    "plays.csv",
)


def _members() -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for name in MEMBERS:
        with (FIXTURES / name).open(encoding="utf-8", newline="") as handle:
            result[name] = list(csv.DictReader(handle))
    return result


def _lineup_semantic_rows() -> dict[str, dict[str, str]]:
    with (FIXTURES / "lineup_semantics.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        return {row["gid"]: row for row in csv.DictReader(handle)}


def test_documented_game_identity_and_exact_regular_scope() -> None:
    normalized = normalize_retrosheet_members(_members(), through_season=2026)
    game_ids = [game.provider_game_id for game in normalized.games]

    assert game_ids == ["BRO195509010", "LAN202607100", "LAN202607110"]
    assert normalized.games[1].home_team_key == "LAD"
    assert normalized.games[1].away_team_key == "SF"
    assert normalized.games[2].away_team_key == "CWS"
    assert normalized.games[0].home_team_provider_id == "BRO"
    assert normalized.games[0].home_team_key is None
    assert {
        (row.source_key, row.reason)
        for row in normalized.excluded_rows
        if row.member_name == "gameinfo.csv"
    } == {
        ("LAN202603010", "nonregular_game_type"),
        ("LAN202610010", "nonregular_game_type"),
        ("LAN202707100", "after_through_season"),
    }


def test_player_mappings_are_distinct_and_unknown_teams_remain_nullable() -> None:
    normalized = normalize_retrosheet_members(_members(), through_season=2026)
    mappings = {row.provider_player_id: row for row in normalized.player_mappings}

    assert mappings["known001"].team_provider_id == "LAN"
    assert mappings["known001"].canonical_team_key == "LAD"
    assert mappings["former01"].team_provider_id == "BRO"
    assert mappings["former01"].canonical_team_key is None
    assert "future01" not in mappings


def test_starting_lineups_pair_player_and_fielding_columns() -> None:
    normalized = normalize_retrosheet_members(_members(), through_season=2026)
    lineup = [
        row
        for row in normalized.lineups
        if row.provider_game_id == "LAN202607100"
    ]

    assert [(row.batting_order, row.provider_player_id) for row in lineup] == [
        (1, "known001"),
        (2, "known002"),
    ]
    assert [row.fielding_position for row in lineup] == ["8", "3"]


def test_official_start_columns_use_batting_slot_and_reverse_fielding_lookup() -> None:
    row = _lineup_semantic_rows()["WBS193805220"]

    _, lineup = normalize_retrosheet_team_game_row(
        row, regular_game_ids={"WBS193805220"}
    )

    assert [(entry.batting_order, entry.provider_player_id) for entry in lineup] == [
        (1, "burbb102"),
        (2, "johnt107"),
        (3, "speah101"),
        (4, "speah101"),
        (5, "wrigz102"),
        (6, "casem101"),
        (7, "andrj104"),
        (8, "hughc101"),
        (9, "yokel101"),
    ]
    by_order = {entry.batting_order: entry for entry in lineup}
    assert by_order[1].fielding_position == "8"
    assert by_order[2].fielding_position == "4"
    assert by_order[3].fielding_position == "5"
    assert by_order[4].fielding_position == "5"
    assert by_order[9].fielding_position == "1"
    assert by_order[3].diagnostic_codes == ("duplicate_lineup_player",)
    assert by_order[4].diagnostic_codes == ("duplicate_lineup_player",)


def test_lineup_fielding_diagnostics_are_explicit_and_fail_closed() -> None:
    row = _lineup_semantic_rows()["TST202607120"]

    _, lineup = normalize_retrosheet_team_game_row(
        row, regular_game_ids={"TST202607120"}
    )

    by_order = {entry.batting_order: entry for entry in lineup}
    assert by_order[1].fielding_position == "8"
    assert by_order[1].fielding_positions == ("8",)
    assert by_order[1].diagnostic_codes == ()
    assert by_order[2].fielding_position is None
    assert by_order[2].fielding_positions == ()
    assert by_order[2].diagnostic_codes == ("missing_fielding_position",)
    assert by_order[3].fielding_position is None
    assert by_order[3].fielding_positions == ("5", "6")
    assert by_order[3].diagnostic_codes == ("ambiguous_fielding_positions",)


def test_stat_lines_preserve_missing_and_legitimate_zero_values() -> None:
    normalized = normalize_retrosheet_members(_members(), through_season=2026)
    batting = next(
        row
        for row in normalized.player_game_lines
        if row.provider_game_id == "LAN202607100" and row.record_kind == "batting"
    )
    old_pitching = next(
        row
        for row in normalized.player_game_lines
        if row.provider_game_id == "BRO195509010" and row.record_kind == "pitching"
    )

    assert batting.stats["b_pa"] is None
    assert batting.stats["b_ab"] == 0
    assert batting.stats["b_h"] == 0
    assert batting.stats["b_bb"] is None
    assert old_pitching.canonical_team_key is None
    assert old_pitching.stats["p_er"] is None
    assert {row.record_kind for row in normalized.player_game_lines} == {
        "batting",
        "pitching",
        "fielding",
    }


def test_invalid_game_ids_fail_closed() -> None:
    with pytest.raises(RetrosheetNormalizationError, match="invalid date"):
        parse_retrosheet_game_id("LAN202602300")


def test_explicit_team_roles_preserve_gid_team_and_provider_exceptions() -> None:
    ordinary = normalize_retrosheet_game_row(
        {
            "gid": "LAN202607100",
            "visteam": "SFN",
            "hometeam": "LAN",
            "gametype": "regular",
        },
        through_season=2026,
    )
    ny6_exception = normalize_retrosheet_game_row(
        {
            "gid": "NY6194905040",
            "visteam": "NY6",
            "hometeam": "IN9",
            "gametype": "regular",
        },
        through_season=2026,
    )
    cha_forfeit_exception = normalize_retrosheet_game_row(
        {
            "gid": "CHA197907122",
            "visteam": "CHA",
            "hometeam": "DET",
            "gametype": "regular",
            "forfeit": "V",
        },
        through_season=2026,
    )

    assert isinstance(ordinary, RetrosheetGame)
    assert ordinary.home_team_provider_id == "LAN"
    assert ordinary.away_team_provider_id == "SFN"
    assert ordinary.game_id_encoded_team_provider_id == "LAN"
    assert ordinary.game_id_team_role_matches is True

    assert isinstance(ny6_exception, RetrosheetGame)
    assert ny6_exception.home_team_provider_id == "IN9"
    assert ny6_exception.away_team_provider_id == "NY6"
    assert ny6_exception.game_id_encoded_team_provider_id == "NY6"
    assert ny6_exception.game_id_team_role_matches is False

    assert isinstance(cha_forfeit_exception, RetrosheetGame)
    assert cha_forfeit_exception.home_team_provider_id == "DET"
    assert cha_forfeit_exception.away_team_provider_id == "CHA"
    assert cha_forfeit_exception.game_id_encoded_team_provider_id == "CHA"
    assert cha_forfeit_exception.game_id_team_role_matches is False


def test_plays_are_parsed_and_nonregular_play_rows_remain_excluded() -> None:
    normalized = normalize_retrosheet_members(_members(), through_season=2026)

    assert [play.provider_game_id for play in normalized.plays] == [
        "LAN202607100",
        "BRO195509010",
    ]
    assert normalized.plays[0].event == "S7"
    assert normalized.plays[0].inning == 1
    assert {
        (row.source_key, row.reason)
        for row in normalized.excluded_rows
        if row.member_name == "plays.csv"
    } == {("LAN202610010", "game_not_in_regular_scope")}
