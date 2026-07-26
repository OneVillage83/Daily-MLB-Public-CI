from __future__ import annotations

from typing import Any

from app.pipeline import (
    _date_association_errors,
    _fixed_indoor_suppression_allowed,
    _weather_lookup_errors,
)
from app.processors.weather_processor import wind_impact
from app.stadiums import field_verification, verified_outfield_bearing


def _stadium() -> dict[str, Any]:
    verified = {
        "status": "VERIFIED",
        "source_ids": ["deterministic_fixture"],
        "verified_at": "2026-07-12",
        "notes": "Deterministic runtime fixture.",
        "conflicts": [],
    }
    return {
        "physical_venue_key": "fixture-park",
        "team_key": "ARI",
        "active_club_association": True,
        "latitude": 33.4453,
        "longitude": -112.0667,
        "timezone": "America/Phoenix",
        "roof_type": "open",
        "outfield_bearing_degrees": 0.0,
        "field_verification": {
            field: dict(verified)
            for field in (
                "physical_venue_key",
                "active_club_association",
                "latitude",
                "longitude",
                "timezone",
                "roof_type",
                "outfield_bearing_degrees",
            )
        },
    }


def test_unverified_weather_fields_do_not_erase_date_association() -> None:
    stadium = _stadium()
    for field in (
        "latitude",
        "longitude",
        "roof_type",
        "outfield_bearing_degrees",
    ):
        stadium["field_verification"][field]["status"] = "UNVERIFIED"

    assert _date_association_errors(stadium, home_team_key="ARI") == []
    assert _weather_lookup_errors(stadium, home_team_key="ARI") == [
        "venue_metadata_unverified:latitude",
        "venue_metadata_unverified:longitude",
    ]


def test_unverified_roof_alone_does_not_block_contextual_weather_lookup() -> None:
    stadium = _stadium()
    stadium["field_verification"]["roof_type"]["status"] = "UNVERIFIED"

    assert _date_association_errors(stadium, home_team_key="ARI") == []
    assert _weather_lookup_errors(stadium, home_team_key="ARI") == []


def test_unverified_timezone_blocks_safe_date_association() -> None:
    stadium = _stadium()
    stadium["field_verification"]["timezone"]["status"] = "UNVERIFIED"

    assert _date_association_errors(stadium, home_team_key="ARI") == [
        "venue_metadata_unverified:timezone"
    ]


def test_active_club_mismatch_blocks_date_and_weather_association() -> None:
    stadium = _stadium()

    expected = ["venue_metadata_invalid:active_club_team_key"]
    assert _date_association_errors(stadium, home_team_key="LAD") == expected
    assert _weather_lookup_errors(stadium, home_team_key="LAD") == expected


def test_fixed_indoor_suppression_requires_verified_fixed_roof() -> None:
    stadium = _stadium()
    stadium["roof_type"] = "fixed"
    stadium["field_verification"]["latitude"]["status"] = "UNVERIFIED"
    stadium["field_verification"]["longitude"]["status"] = "UNVERIFIED"
    assert _fixed_indoor_suppression_allowed(stadium, home_team_key="ARI") is True

    stadium["field_verification"]["roof_type"]["status"] = "UNVERIFIED"
    assert _fixed_indoor_suppression_allowed(stadium, home_team_key="ARI") is False


def test_retractable_roof_never_uses_fixed_indoor_suppression() -> None:
    stadium = _stadium()
    stadium["roof_type"] = "retractable"

    assert _fixed_indoor_suppression_allowed(stadium, home_team_key="ARI") is False


def test_fixed_indoor_suppression_rejects_wrong_team_association() -> None:
    stadium = _stadium()
    stadium["roof_type"] = "fixed"

    assert _fixed_indoor_suppression_allowed(stadium, home_team_key="LAD") is False


def test_conflicted_bearing_never_reaches_field_relative_wind_math() -> None:
    stadium = _stadium()
    stadium["field_verification"]["outfield_bearing_degrees"].update(
        {
            "status": "UNVERIFIED",
            "conflicts": [
                {
                    "source_id": "conflicting_fixture",
                    "observed_value": 180.0,
                }
            ],
        }
    )
    metadata = field_verification(stadium, "outfield_bearing_degrees")

    result = wind_impact(
        180.0,
        10.0,
        verified_outfield_bearing(stadium),
        bearing_verification_state=str(metadata["status"]),
    )

    assert result["outfield_component_mph"] is None
    assert result["crosswind_component_mph"] is None
    assert result["reason_code"] == "outfield_bearing_unverified"
