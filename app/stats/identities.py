from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping

from app.team_aliases import CANONICAL_TEAM_KEYS


TEAM_SOURCE_MAPPING_VERSION: Final = 1
TEAM_SOURCE_MAPPING_PATH: Final = (
    Path(__file__).with_name("data") / "team_source_aliases.v1.json"
)


class StatsIdentityConfigurationError(ValueError):
    """Raised when an exact source identity map is ambiguous or incomplete."""


@dataclass(frozen=True, slots=True)
class TeamIdentity:
    provider: str
    source_team_id: str
    canonical_team_key: str | None


@dataclass(frozen=True, slots=True)
class BaseballReferenceAggregateTeamIdentity:
    source_team_name: str
    source_level: str
    source_scope_id: str
    canonical_team_key: str | None
    is_multi_team: bool


def load_team_source_aliases(
    path: Path = TEAM_SOURCE_MAPPING_PATH,
) -> Mapping[str, Mapping[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatsIdentityConfigurationError(
            f"Unable to load stats team identity mapping: {path.name}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != TEAM_SOURCE_MAPPING_VERSION:
        raise StatsIdentityConfigurationError(
            f"Stats team identity mapping version must be {TEAM_SOURCE_MAPPING_VERSION}"
        )
    providers = payload.get("providers")
    if not isinstance(providers, dict):
        raise StatsIdentityConfigurationError("Stats team identity providers must be an object")

    result: dict[str, Mapping[str, str]] = {}
    for raw_provider, raw_aliases in providers.items():
        provider = str(raw_provider).strip().lower()
        if not provider or not isinstance(raw_aliases, dict):
            raise StatsIdentityConfigurationError("Each stats provider requires an alias object")
        aliases: dict[str, str] = {}
        for raw_source_id, raw_team_key in raw_aliases.items():
            source_id = str(raw_source_id).strip().upper()
            team_key = str(raw_team_key).strip().upper()
            if not source_id or source_id in aliases:
                raise StatsIdentityConfigurationError(
                    f"Duplicate or empty {provider} team source identity"
                )
            if team_key not in CANONICAL_TEAM_KEYS:
                raise StatsIdentityConfigurationError(
                    f"Unknown canonical MLB team key {team_key!r} for {provider}"
                )
            aliases[source_id] = team_key
        if set(aliases.values()) != set(CANONICAL_TEAM_KEYS):
            missing = sorted(set(CANONICAL_TEAM_KEYS) - set(aliases.values()))
            raise StatsIdentityConfigurationError(
                f"Provider {provider} must map every active MLB team; missing={missing}"
            )
        result[provider] = MappingProxyType(aliases)
    return MappingProxyType(result)


TEAM_SOURCE_ALIASES: Final = load_team_source_aliases()


def resolve_team_identity(provider: str, source_team_id: str) -> TeamIdentity:
    normalized_provider = str(provider).strip().lower()
    normalized_source_id = str(source_team_id).strip().upper()
    aliases = TEAM_SOURCE_ALIASES.get(normalized_provider)
    return TeamIdentity(
        provider=normalized_provider,
        source_team_id=normalized_source_id,
        canonical_team_key=(aliases or {}).get(normalized_source_id),
    )


def require_active_team_identity(provider: str, source_team_id: str) -> TeamIdentity:
    identity = resolve_team_identity(provider, source_team_id)
    if identity.canonical_team_key is None:
        raise ValueError(
            f"Unknown {identity.provider} active MLB team identity: "
            f"{identity.source_team_id}"
        )
    return identity


def resolve_baseball_reference_aggregate_team(
    source_team_name: str,
    source_level: str,
) -> BaseballReferenceAggregateTeamIdentity:
    """Resolve exact daily-table identity without guessing ambiguous city names."""

    team_name = str(source_team_name).strip()
    level = str(source_level).strip()
    if not team_name or not level or any(character in team_name + level for character in "\r\n"):
        raise ValueError("Baseball-Reference aggregate team scope is missing")

    # Sanitized legacy fixtures and older provider shapes expose a proper team code.
    legacy = resolve_team_identity("baseball_reference", team_name)
    if legacy.canonical_team_key is not None:
        return BaseballReferenceAggregateTeamIdentity(
            source_team_name=team_name,
            source_level=level,
            source_scope_id=team_name,
            canonical_team_key=legacy.canonical_team_key,
            is_multi_team=False,
        )

    scope_id = f"{team_name}|{level}"
    exact = resolve_team_identity("baseball_reference_daily", scope_id)
    if exact.canonical_team_key is not None:
        return BaseballReferenceAggregateTeamIdentity(
            source_team_name=team_name,
            source_level=level,
            source_scope_id=scope_id,
            canonical_team_key=exact.canonical_team_key,
            is_multi_team=False,
        )

    names = tuple(part.strip() for part in team_name.split(","))
    levels = tuple(part.strip() for part in level.split(","))
    if (
        len(names) < 2
        or any(not part for part in names)
        or len(names) != len(set(names))
        or not levels
        or any(part not in {"Maj-AL", "Maj-NL"} for part in levels)
        or len(levels) != len(set(levels))
    ):
        raise ValueError("Unknown Baseball-Reference aggregate team scope")

    daily_aliases = TEAM_SOURCE_ALIASES["baseball_reference_daily"]
    known_names = {
        source_id.split("|", 1)[0]
        for source_id in daily_aliases
        if "|" in source_id
    }
    if any(name.upper() not in known_names for name in names):
        raise ValueError("Unknown Baseball-Reference multi-team aggregate scope")
    return BaseballReferenceAggregateTeamIdentity(
        source_team_name=team_name,
        source_level=level,
        source_scope_id=scope_id,
        canonical_team_key=None,
        is_multi_team=True,
    )
