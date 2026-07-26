from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import TypeAlias

from app.stats.identities import resolve_team_identity


_GAME_ID_RE = re.compile(
    r"^(?P<home>[A-Z0-9]{3})(?P<date>[0-9]{8})(?P<number>[0-9]+)$"
)
_INTEGER_RE = re.compile(r"^[+-]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)$")
_MISSING_VALUES = frozenset({"", "na", "n/a", "null", "none", "nan", "--"})
_IDENTITY_COLUMNS = frozenset({"gid", "id", "team", "stattype", "gametype"})
_PLAYER_STAT_MEMBERS = MappingProxyType(
    {
        "batting.csv": "batting",
        "pitching.csv": "pitching",
        "fielding.csv": "fielding",
    }
)

StatValue: TypeAlias = int | float | str | None


class RetrosheetNormalizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetrosheetGame:
    provider_game_id: str
    game_date: date
    game_number: int
    game_id_encoded_team_provider_id: str
    game_id_team_role_matches: bool
    away_team_provider_id: str
    home_team_provider_id: str
    away_team_key: str | None
    home_team_key: str | None
    game_type: str = "regular"


@dataclass(frozen=True, slots=True)
class RetrosheetPlayerMapping:
    provider_player_id: str
    season: int
    team_provider_id: str | None
    canonical_team_key: str | None
    first_name: str | None
    last_name: str | None


@dataclass(frozen=True, slots=True)
class RetrosheetLineupEntry:
    provider_game_id: str
    team_provider_id: str
    canonical_team_key: str | None
    batting_order: int
    provider_player_id: str
    fielding_position: str | None
    fielding_positions: tuple[str, ...] = ()
    diagnostic_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RetrosheetTeamGameLine:
    provider_game_id: str
    team_provider_id: str
    canonical_team_key: str | None
    stats_type: str | None
    stats: Mapping[str, StatValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stats", MappingProxyType(dict(self.stats)))


@dataclass(frozen=True, slots=True)
class RetrosheetPlayerGameLine:
    provider_game_id: str
    provider_player_id: str
    team_provider_id: str
    canonical_team_key: str | None
    record_kind: str
    stats_type: str | None
    position_code: str | None
    stats: Mapping[str, StatValue]

    def __post_init__(self) -> None:
        if self.record_kind == "fielding" and not self.position_code:
            raise RetrosheetNormalizationError(
                "fielding.csv rows require a non-empty d_pos source grain"
            )
        if self.record_kind != "fielding" and self.position_code is not None:
            raise RetrosheetNormalizationError(
                "position_code is valid only for fielding.csv rows"
            )
        object.__setattr__(self, "stats", MappingProxyType(dict(self.stats)))


@dataclass(frozen=True, slots=True)
class RetrosheetPlay:
    provider_game_id: str
    event: str | None
    inning: int | None
    half_inning: str | None
    details: Mapping[str, StatValue]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class RetrosheetExcludedRow:
    member_name: str
    source_key: str | None
    reason: str
    source_row: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_row", MappingProxyType(dict(self.source_row)))


@dataclass(frozen=True, slots=True)
class RetrosheetNormalizedDataset:
    through_season: int
    games: tuple[RetrosheetGame, ...]
    player_mappings: tuple[RetrosheetPlayerMapping, ...]
    lineups: tuple[RetrosheetLineupEntry, ...]
    team_game_lines: tuple[RetrosheetTeamGameLine, ...]
    player_game_lines: tuple[RetrosheetPlayerGameLine, ...]
    plays: tuple[RetrosheetPlay, ...]
    excluded_rows: tuple[RetrosheetExcludedRow, ...]


def parse_retrosheet_game_id(value: str) -> tuple[date, str, int]:
    game_id = str(value).strip()
    match = _GAME_ID_RE.fullmatch(game_id)
    if match is None:
        raise RetrosheetNormalizationError(
            f"Invalid documented Retrosheet game ID: {game_id!r}"
        )
    try:
        game_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
    except ValueError as exc:
        raise RetrosheetNormalizationError(
            f"Retrosheet game ID contains an invalid date: {game_id!r}"
        ) from exc
    return game_date, match.group("home"), int(match.group("number"))


def _team_key(provider_team_id: str | None) -> str | None:
    if provider_team_id is None:
        return None
    return resolve_team_identity("retrosheet", provider_team_id).canonical_team_key


def _required(row: Mapping[str, str], key: str, member_name: str) -> str:
    value = str(row.get(key, "")).strip()
    if not value:
        raise RetrosheetNormalizationError(
            f"{member_name} row requires non-empty {key}"
        )
    return value


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stat_value(value: object) -> StatValue:
    text = str(value or "").strip()
    if text.casefold() in _MISSING_VALUES:
        return None
    if _INTEGER_RE.fullmatch(text):
        return int(text)
    if _FLOAT_RE.fullmatch(text):
        return float(text)
    return text


def _stats(row: Mapping[str, str]) -> Mapping[str, StatValue]:
    return MappingProxyType(
        {
            key: _stat_value(value)
            for key, value in sorted(row.items())
            if key not in _IDENTITY_COLUMNS
            and not re.fullmatch(r"start_[lf][1-9]", key)
        }
    )


def _normalize_games(
    rows: Iterable[Mapping[str, str]],
    through_season: int,
) -> tuple[list[RetrosheetGame], list[RetrosheetExcludedRow]]:
    games: list[RetrosheetGame] = []
    excluded: list[RetrosheetExcludedRow] = []
    seen: set[str] = set()
    for row in rows:
        game_id = _required(row, "gid", "gameinfo.csv")
        game_date, encoded_team, game_number = parse_retrosheet_game_id(game_id)
        game_type = str(row.get("gametype", "")).strip()
        reason = None
        if game_type != "regular":
            reason = "nonregular_game_type"
        elif game_date.year > through_season:
            reason = "after_through_season"
        if reason is not None:
            excluded.append(
                RetrosheetExcludedRow("gameinfo.csv", game_id, reason, row)
            )
            continue
        away = _required(row, "visteam", "gameinfo.csv")
        home = _required(row, "hometeam", "gameinfo.csv")
        # Official Retrosheet game logs contain a small number of documented
        # provider exceptions where the GID prefix is not the home club. The
        # explicit visteam/hometeam columns carry team roles; the encoded GID
        # team is retained independently so the mismatch remains auditable.
        game_id_team_role_matches = home == encoded_team
        if game_id in seen:
            raise RetrosheetNormalizationError(
                f"Duplicate Retrosheet game ID: {game_id!r}"
            )
        seen.add(game_id)
        games.append(
            RetrosheetGame(
                provider_game_id=game_id,
                game_date=game_date,
                game_number=game_number,
                game_id_encoded_team_provider_id=encoded_team,
                game_id_team_role_matches=game_id_team_role_matches,
                away_team_provider_id=away,
                home_team_provider_id=home,
                away_team_key=_team_key(away),
                home_team_key=_team_key(home),
            )
        )
    games.sort(key=lambda game: (game.game_date, game.provider_game_id))
    return games, excluded


def _normalize_player_mappings(
    rows: Iterable[Mapping[str, str]],
    through_season: int,
) -> tuple[list[RetrosheetPlayerMapping], list[RetrosheetExcludedRow]]:
    mappings: list[RetrosheetPlayerMapping] = []
    excluded: list[RetrosheetExcludedRow] = []
    for row in rows:
        player_id = _required(row, "id", "allplayers.csv")
        try:
            season = int(_required(row, "season", "allplayers.csv"))
        except ValueError as exc:
            raise RetrosheetNormalizationError(
                "allplayers.csv season must be an integer"
            ) from exc
        if season > through_season:
            excluded.append(
                RetrosheetExcludedRow(
                    "allplayers.csv", player_id, "after_through_season", row
                )
            )
            continue
        team = _optional(row.get("team"))
        mappings.append(
            RetrosheetPlayerMapping(
                player_id,
                season,
                team,
                _team_key(team),
                _optional(row.get("first")),
                _optional(row.get("last")),
            )
        )
    mappings.sort(
        key=lambda item: (item.season, item.provider_player_id, item.team_provider_id or "")
    )
    return mappings, excluded


def _normalize_team_lines(
    rows: Iterable[Mapping[str, str]],
    game_ids: set[str],
) -> tuple[
    list[RetrosheetTeamGameLine],
    list[RetrosheetLineupEntry],
    list[RetrosheetExcludedRow],
]:
    lines: list[RetrosheetTeamGameLine] = []
    lineups: list[RetrosheetLineupEntry] = []
    excluded: list[RetrosheetExcludedRow] = []
    for row in rows:
        game_id = _required(row, "gid", "teamstats.csv")
        if game_id not in game_ids:
            excluded.append(
                RetrosheetExcludedRow(
                    "teamstats.csv", game_id, "game_not_in_regular_scope", row
                )
            )
            continue
        team = _required(row, "team", "teamstats.csv")
        canonical = _team_key(team)
        lines.append(
            RetrosheetTeamGameLine(
                game_id,
                team,
                canonical,
                _optional(row.get("stattype")),
                _stats(row),
            )
        )
        fielding_positions: defaultdict[str, list[str]] = defaultdict(list)
        for position in range(1, 11):
            fielding_player = _optional(row.get(f"start_f{position}"))
            if fielding_player is not None:
                fielding_positions[fielding_player].append(str(position))
        lineup_players = [
            player_id
            for order in range(1, 10)
            if (player_id := _optional(row.get(f"start_l{order}"))) is not None
        ]
        lineup_player_counts = Counter(lineup_players)
        for order in range(1, 10):
            player_id = _optional(row.get(f"start_l{order}"))
            if player_id is None:
                continue
            positions = tuple(fielding_positions.get(player_id, ()))
            diagnostics: list[str] = []
            if not positions:
                diagnostics.append("missing_fielding_position")
            elif len(positions) > 1:
                diagnostics.append("ambiguous_fielding_positions")
            if lineup_player_counts[player_id] > 1:
                diagnostics.append("duplicate_lineup_player")
            lineups.append(
                RetrosheetLineupEntry(
                    game_id,
                    team,
                    canonical,
                    order,
                    player_id,
                    positions[0] if len(positions) == 1 else None,
                    positions,
                    tuple(diagnostics),
                )
            )
    lines.sort(key=lambda item: (item.provider_game_id, item.team_provider_id))
    lineups.sort(
        key=lambda item: (
            item.provider_game_id,
            item.team_provider_id,
            item.batting_order,
        )
    )
    return lines, lineups, excluded


def _normalize_player_lines(
    member_name: str,
    rows: Iterable[Mapping[str, str]],
    game_ids: set[str],
) -> tuple[list[RetrosheetPlayerGameLine], list[RetrosheetExcludedRow]]:
    kind = _PLAYER_STAT_MEMBERS[member_name]
    lines: list[RetrosheetPlayerGameLine] = []
    excluded: list[RetrosheetExcludedRow] = []
    for row in rows:
        game_id = _required(row, "gid", member_name)
        if game_id not in game_ids:
            excluded.append(
                RetrosheetExcludedRow(
                    member_name, game_id, "game_not_in_regular_scope", row
                )
            )
            continue
        player_id = _required(row, "id", member_name)
        team = _required(row, "team", member_name)
        position_code = _optional(row.get("d_pos")) if kind == "fielding" else None
        lines.append(
            RetrosheetPlayerGameLine(
                provider_game_id=game_id,
                provider_player_id=player_id,
                team_provider_id=team,
                canonical_team_key=_team_key(team),
                record_kind=kind,
                stats_type=_optional(row.get("stattype")),
                position_code=position_code,
                stats=_stats(row),
            )
        )
    lines.sort(
        key=lambda item: (
            item.provider_game_id,
            item.team_provider_id,
            item.provider_player_id,
            item.record_kind,
            item.position_code or "",
        )
    )
    return lines, excluded


def _normalize_plays(
    rows: Iterable[Mapping[str, str]],
    game_ids: set[str],
) -> tuple[list[RetrosheetPlay], list[RetrosheetExcludedRow]]:
    plays: list[RetrosheetPlay] = []
    excluded: list[RetrosheetExcludedRow] = []
    for row in rows:
        game_id = _required(row, "gid", "plays.csv")
        if game_id not in game_ids:
            excluded.append(
                RetrosheetExcludedRow(
                    "plays.csv", game_id, "game_not_in_regular_scope", row
                )
            )
            continue
        inning_value = _stat_value(row.get("inning"))
        if inning_value is not None and not isinstance(inning_value, int):
            raise RetrosheetNormalizationError(
                "plays.csv inning must be an integer when supplied"
            )
        plays.append(
            RetrosheetPlay(
                provider_game_id=game_id,
                event=_optional(row.get("event")),
                inning=inning_value,
                half_inning=_optional(
                    row.get("half_inning", row.get("inning_topbot"))
                ),
                details=_stats(row),
            )
        )
    return plays, excluded


def normalize_retrosheet_game_row(
    row: Mapping[str, str], *, through_season: int
) -> RetrosheetGame | RetrosheetExcludedRow:
    games, excluded = _normalize_games((row,), through_season)
    return games[0] if games else excluded[0]


def normalize_retrosheet_player_mapping_row(
    row: Mapping[str, str], *, through_season: int
) -> RetrosheetPlayerMapping | RetrosheetExcludedRow:
    mappings, excluded = _normalize_player_mappings((row,), through_season)
    return mappings[0] if mappings else excluded[0]


def normalize_retrosheet_team_game_row(
    row: Mapping[str, str], *, regular_game_ids: set[str]
) -> tuple[
    RetrosheetTeamGameLine | RetrosheetExcludedRow,
    tuple[RetrosheetLineupEntry, ...],
]:
    lines, lineups, excluded = _normalize_team_lines((row,), regular_game_ids)
    if lines:
        return lines[0], tuple(lineups)
    return excluded[0], ()


def normalize_retrosheet_player_game_row(
    member_name: str,
    row: Mapping[str, str],
    *,
    regular_game_ids: set[str],
) -> RetrosheetPlayerGameLine | RetrosheetExcludedRow:
    lines, excluded = _normalize_player_lines(
        member_name, (row,), regular_game_ids
    )
    return lines[0] if lines else excluded[0]


def normalize_retrosheet_play_row(
    row: Mapping[str, str], *, regular_game_ids: set[str]
) -> RetrosheetPlay | RetrosheetExcludedRow:
    plays, excluded = _normalize_plays((row,), regular_game_ids)
    return plays[0] if plays else excluded[0]


def normalize_retrosheet_members(
    members: Mapping[str, Iterable[Mapping[str, str]]],
    *,
    through_season: int,
) -> RetrosheetNormalizedDataset:
    if isinstance(through_season, bool) or not 1871 <= through_season <= 9999:
        raise ValueError("through_season must be a valid major-league season")
    required = {
        "allplayers.csv",
        "gameinfo.csv",
        "teamstats.csv",
        "plays.csv",
        *_PLAYER_STAT_MEMBERS,
    }
    missing = sorted(required - set(members))
    if missing:
        raise RetrosheetNormalizationError(
            f"Retrosheet normalization is missing members: {missing}"
        )

    games, excluded = _normalize_games(members["gameinfo.csv"], through_season)
    game_ids = {game.provider_game_id for game in games}
    mappings, mapping_excluded = _normalize_player_mappings(
        members["allplayers.csv"], through_season
    )
    team_lines, lineups, team_excluded = _normalize_team_lines(
        members["teamstats.csv"], game_ids
    )
    player_lines: list[RetrosheetPlayerGameLine] = []
    player_excluded: list[RetrosheetExcludedRow] = []
    for member_name in _PLAYER_STAT_MEMBERS:
        lines, rejected = _normalize_player_lines(
            member_name, members[member_name], game_ids
        )
        player_lines.extend(lines)
        player_excluded.extend(rejected)
    player_lines.sort(
        key=lambda item: (
            item.provider_game_id,
            item.record_kind,
            item.position_code or "",
            item.team_provider_id,
            item.provider_player_id,
        )
    )
    plays, play_excluded = _normalize_plays(members["plays.csv"], game_ids)
    all_excluded = [
        *excluded,
        *mapping_excluded,
        *team_excluded,
        *player_excluded,
        *play_excluded,
    ]
    all_excluded.sort(
        key=lambda item: (item.member_name, item.source_key or "", item.reason)
    )
    return RetrosheetNormalizedDataset(
        through_season,
        tuple(games),
        tuple(mappings),
        tuple(lineups),
        tuple(team_lines),
        tuple(player_lines),
        tuple(plays),
        tuple(all_excluded),
    )
