from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.team_aliases import (
    ALIASES,
    CANONICAL_TEAM_KEYS,
    TEAM_MAPPING_VERSION,
    TeamAliasConfigurationError,
    load_team_aliases,
    team_key,
)

EXPECTED_ALIASES = {
    "Arizona Diamondbacks": "ARI",
    "Athletics": "ATH",
    "Oakland Athletics": "ATH",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD",
    "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}


def _write_mapping(path: Path, teams: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps({"version": TEAM_MAPPING_VERSION, "teams": teams}),
        encoding="utf-8",
    )


def _valid_teams() -> list[dict[str, object]]:
    teams: dict[str, list[str]] = {key: [] for key in CANONICAL_TEAM_KEYS}
    for alias, key in EXPECTED_ALIASES.items():
        teams[key].append(alias)
    return [{"key": key, "aliases": aliases} for key, aliases in sorted(teams.items())]


def test_mapping_defines_every_canonical_mlb_team() -> None:
    assert TEAM_MAPPING_VERSION == 1
    assert ALIASES == EXPECTED_ALIASES
    assert len(CANONICAL_TEAM_KEYS) == 30
    assert set(ALIASES.values()) == CANONICAL_TEAM_KEYS


@pytest.mark.parametrize(("provider_name", "expected"), EXPECTED_ALIASES.items())
def test_every_exact_provider_name_maps_to_its_canonical_key(
    provider_name: str,
    expected: str,
) -> None:
    assert team_key(provider_name) == expected


def test_athletics_current_and_legacy_provider_names_are_explicit_aliases() -> None:
    assert team_key("Athletics") == "ATH"
    assert team_key("Oakland Athletics") == "ATH"


@pytest.mark.parametrize(
    "unknown",
    ["Unknown Team", "athletics", " Athletics", "Athletics ", "Oakland A's", ""],
)
def test_unknown_or_inexact_names_are_not_fuzzy_matched(unknown: str) -> None:
    assert team_key(unknown) is None


def test_loader_rejects_duplicate_raw_aliases(tmp_path: Path) -> None:
    teams = _valid_teams()
    teams[1]["aliases"] = ["Arizona Diamondbacks"]
    mapping = tmp_path / "duplicate-alias.json"
    _write_mapping(mapping, teams)

    with pytest.raises(TeamAliasConfigurationError, match="Duplicate raw team alias"):
        load_team_aliases(mapping)


def test_loader_rejects_duplicate_canonical_keys(tmp_path: Path) -> None:
    teams = _valid_teams()
    teams.append({"key": "ARI", "aliases": ["Second Arizona Name"]})
    mapping = tmp_path / "duplicate-key.json"
    _write_mapping(mapping, teams)

    with pytest.raises(TeamAliasConfigurationError, match="Duplicate canonical MLB team key"):
        load_team_aliases(mapping)


def test_loader_rejects_missing_canonical_keys(tmp_path: Path) -> None:
    teams = [team for team in _valid_teams() if team["key"] != "WSH"]
    mapping = tmp_path / "missing-key.json"
    _write_mapping(mapping, teams)

    with pytest.raises(TeamAliasConfigurationError, match=r"missing=\['WSH'\]"):
        load_team_aliases(mapping)


def test_loader_rejects_unknown_canonical_key(tmp_path: Path) -> None:
    teams = _valid_teams()
    teams[0]["key"] = "NOT_MLB"
    mapping = tmp_path / "unknown-key.json"
    _write_mapping(mapping, teams)

    with pytest.raises(TeamAliasConfigurationError, match="Unknown canonical MLB team key"):
        load_team_aliases(mapping)


def test_loader_rejects_aliases_with_hidden_whitespace(tmp_path: Path) -> None:
    teams = _valid_teams()
    teams[0]["aliases"] = [" Arizona Diamondbacks"]
    mapping = tmp_path / "whitespace.json"
    _write_mapping(mapping, teams)

    with pytest.raises(TeamAliasConfigurationError, match="invalid raw alias"):
        load_team_aliases(mapping)
