from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import (
    DailySlateContractError,
    VenueMappingStatus,
)
from app.stadiums import (
    VenueResolutionStatus,
    load_stadiums,
    resolve_venue_alias,
)
from app.team_aliases import CANONICAL_TEAM_KEYS, team_key


@dataclass(frozen=True, slots=True)
class VenueIdentityResolutionV1:
    status: VenueMappingStatus
    venue_id: str | None
    source_venue_name: str
    resolution_reason: str | None = None


def resolve_canonical_team_id(source_team_name_or_id: str) -> str:
    if source_team_name_or_id in CANONICAL_TEAM_KEYS:
        return source_team_name_or_id
    resolved = team_key(source_team_name_or_id)
    if resolved is None:
        raise DailySlateContractError(
            "source team identifier does not resolve through the canonical MLB mapping"
        )
    return resolved


def resolve_canonical_venue_id(
    source_venue_name: str,
    *,
    home_team_id: str,
) -> VenueIdentityResolutionV1:
    if home_team_id not in CANONICAL_TEAM_KEYS:
        raise DailySlateContractError("home_team_id is not a canonical MLB team ID")
    resolution = resolve_venue_alias(source_venue_name, team_key=home_team_id)
    status = str(resolution.get("status"))
    venue_id = resolution.get("physical_venue_key")
    known_venue_ids = {
        str(record["physical_venue_key"]) for record in load_stadiums().values()
    }
    if (
        status
        in {
            VenueResolutionStatus.CURRENT.value,
            VenueResolutionStatus.HISTORICAL_SAME_VENUE.value,
            VenueResolutionStatus.FORMER_PHYSICAL_VENUE.value,
        }
        and isinstance(venue_id, str)
        and venue_id in known_venue_ids
    ):
        return VenueIdentityResolutionV1(
            status=VenueMappingStatus.RESOLVED,
            venue_id=venue_id,
            source_venue_name=source_venue_name,
        )
    return VenueIdentityResolutionV1(
        status=VenueMappingStatus.UNRESOLVED,
        venue_id=None,
        source_venue_name=source_venue_name,
        resolution_reason=str(resolution.get("reason") or status),
    )
