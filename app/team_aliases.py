from __future__ import annotations

import json
from pathlib import Path
from typing import Final

TEAM_MAPPING_VERSION: Final = 1
TEAM_MAPPING_PATH: Final = Path(__file__).with_name("data") / "mlb_team_aliases.v1.json"
CANONICAL_TEAM_KEYS: Final = frozenset(
    {
        "ARI",
        "ATH",
        "ATL",
        "BAL",
        "BOS",
        "CHC",
        "CIN",
        "CLE",
        "COL",
        "CWS",
        "DET",
        "HOU",
        "KC",
        "LAA",
        "LAD",
        "MIA",
        "MIL",
        "MIN",
        "NYM",
        "NYY",
        "PHI",
        "PIT",
        "SD",
        "SEA",
        "SF",
        "STL",
        "TB",
        "TEX",
        "TOR",
        "WSH",
    }
)


class TeamAliasConfigurationError(ValueError):
    """Raised when the versioned MLB team mapping is invalid."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TeamAliasConfigurationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_team_aliases(path: Path = TEAM_MAPPING_PATH) -> dict[str, str]:
    """Load and validate an exact provider-name to canonical-key mapping."""
    try:
        document: object = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamAliasConfigurationError(f"Unable to load MLB team mapping: {path.name}") from exc

    if not isinstance(document, dict):
        raise TeamAliasConfigurationError("MLB team mapping root must be an object")
    if document.get("version") != TEAM_MAPPING_VERSION:
        raise TeamAliasConfigurationError(
            f"MLB team mapping version must be {TEAM_MAPPING_VERSION}"
        )

    teams = document.get("teams")
    if not isinstance(teams, list):
        raise TeamAliasConfigurationError("MLB team mapping teams must be a list")

    canonical_keys: set[str] = set()
    aliases: dict[str, str] = {}
    for index, team in enumerate(teams):
        if not isinstance(team, dict):
            raise TeamAliasConfigurationError(f"Team entry {index} must be an object")
        if set(team) != {"key", "aliases"}:
            raise TeamAliasConfigurationError(
                f"Team entry {index} must contain only key and aliases"
            )

        key = team["key"]
        raw_aliases = team["aliases"]
        if not isinstance(key, str) or not key:
            raise TeamAliasConfigurationError(f"Team entry {index} has an invalid canonical key")
        if key not in CANONICAL_TEAM_KEYS:
            raise TeamAliasConfigurationError(f"Unknown canonical MLB team key: {key}")
        if key in canonical_keys:
            raise TeamAliasConfigurationError(f"Duplicate canonical MLB team key: {key}")
        if not isinstance(raw_aliases, list) or not raw_aliases:
            raise TeamAliasConfigurationError(f"Team {key} must define at least one alias")

        canonical_keys.add(key)
        for alias in raw_aliases:
            if not isinstance(alias, str) or not alias or alias != alias.strip():
                raise TeamAliasConfigurationError(f"Team {key} has an invalid raw alias")
            if alias in aliases:
                raise TeamAliasConfigurationError(
                    f"Duplicate raw team alias {alias!r} for {aliases[alias]} and {key}"
                )
            aliases[alias] = key

    if canonical_keys != CANONICAL_TEAM_KEYS:
        missing = sorted(CANONICAL_TEAM_KEYS - canonical_keys)
        raise TeamAliasConfigurationError(
            f"MLB team mapping must define every canonical key; missing={missing}"
        )

    return aliases


ALIASES: Final = load_team_aliases()


def team_key(name: str) -> str | None:
    """Return a canonical MLB key for an exact configured provider name."""
    return ALIASES.get(name)
