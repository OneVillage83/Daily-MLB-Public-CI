from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from app.analysis import (
    CANONICAL_GAME_VERSION,
    FEATURE_VERSION,
    DataQualityState,
    assemble_canonical_game,
    assemble_features,
)
from app.stadiums import load_stadiums

NOW = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
COMMENCE = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


def _offer(book: str, price: int, order: int) -> dict[str, object]:
    return {
        "bookmaker_key": book,
        "price": price,
        "calculation_eligible": True,
        "effective_provider_timestamp": "2026-07-15T23:59:00+00:00",
        "provider_retrieved_at": "2026-07-15T23:59:10+00:00",
        "provider_order": order,
    }


def odds_summary() -> dict[str, Any]:
    home = [
        _offer("book-a", 120, 1),
        _offer("book-b", 125, 3),
        _offer("book-c", 118, 5),
        _offer("book-d", 122, 7),
    ]
    away = [
        _offer("book-a", -130, 2),
        _offer("book-b", -135, 4),
        _offer("book-c", -128, 6),
        _offer("book-d", -132, 8),
    ]
    return {
        "contract_version": "odds-consensus-v2",
        "event_id": "event-rc-1",
        "commence_time": COMMENCE.isoformat(),
        "raw_home_team": "Arizona Diamondbacks",
        "raw_away_team": "Los Angeles Dodgers",
        "home_team_key": "ARI",
        "away_team_key": "LAD",
        "retrieval_summary": {"retrieved_at": "2026-07-15T23:59:10+00:00"},
        "markets": {
            "h2h": {
                "lines": {
                    "moneyline": {
                        "complete_two_way_market": True,
                        "outcomes": {
                            "ARI": {"offers": home},
                            "LAD": {"offers": away},
                        },
                    }
                }
            }
        },
    }


def venue(*, roof: str = "open") -> dict[str, Any]:
    verified = {
        "status": "VERIFIED",
        "source_ids": ["deterministic_fixture"],
        "verified_at": "2026-07-12",
        "notes": "Deterministic runtime fixture.",
        "conflicts": [],
    }
    return {
        "physical_venue_key": "fixture-park",
        "base_venue_name": "Fixture Park",
        "current_display_name": "Fixture Park",
        "venue": "Fixture Park",
        "team_key": "ARI",
        "active_club_association": True,
        "timezone": "America/Los_Angeles",
        "roof_type": roof,
        "latitude": 33.4453,
        "longitude": -112.0667,
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


def weather(*, forecast_time: str | None = None) -> dict[str, object]:
    return {
        "stadium": venue(),
        "nws": {
            "forecast_time": forecast_time or COMMENCE.isoformat(),
            "temperature_f": 88.0,
            "wind_speed_mph": 4.0,
            "precipitation_probability_pct": 0.0,
        },
    }


def test_canonical_game_ready_and_checksums_sources() -> None:
    game = assemble_canonical_game(
        run_id="20260715T235900Z-0123456789abcdef0123456789abcdef",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.contract_version == CANONICAL_GAME_VERSION
    assert game.quality_state is DataQualityState.READY
    assert game.weather_gate_clear is True
    assert set(game.source_checksums) == {"odds", "venue", "weather"}
    assert all(len(value) == 64 for value in game.source_checksums.values())
    assert len(game.checksum) == 64
    assert game.as_dict()["commence_time"] == COMMENCE.isoformat()


def test_missing_outdoor_weather_is_degraded_and_fails_weather_gate() -> None:
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=None,
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.DEGRADED
    assert game.weather_gate_clear is False
    assert [issue.code for issue in game.quality_issues] == ["weather_missing"]


def test_fixed_roof_does_not_require_outdoor_weather() -> None:
    fixed = venue(roof="fixed")
    fixed["field_verification"]["latitude"]["status"] = "UNVERIFIED"
    fixed["field_verification"]["longitude"]["status"] = "UNVERIFIED"
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet={"weather_status": "indoor_fixed_roof"},
        venue=fixed,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.READY
    assert game.weather_gate_clear is True
    assert game.roof_context == "closed"


def test_material_identity_and_market_failures_block_game() -> None:
    summary = odds_summary()
    summary["home_team_key"] = ""
    summary["markets"] = {}
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=summary,
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    codes = {issue.code for issue in game.quality_issues if issue.blocking}
    assert codes == {
            "invalid_team_identity",
            "venue_team_mismatch",
            "invalid_h2h_market",
        "h2h_identity_mismatch",
        "insufficient_fresh_h2h_books",
    }


def test_insufficient_fresh_h2h_pairs_block_launch_ready_state() -> None:
    summary = odds_summary()
    outcomes = summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    outcomes["ARI"]["offers"] = outcomes["ARI"]["offers"][:3]
    outcomes["LAD"]["offers"] = outcomes["LAD"]["offers"][:3]

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=summary,
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert "insufficient_fresh_h2h_books" in {issue.code for issue in game.quality_issues}


def test_stale_h2h_pairs_block_launch_ready_state() -> None:
    summary = odds_summary()
    outcomes = summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    for side in ("ARI", "LAD"):
        for offer in outcomes[side]["offers"]:
            offer["effective_provider_timestamp"] = "2026-07-15T23:57:59+00:00"

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=summary,
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert "insufficient_fresh_h2h_books" in {issue.code for issue in game.quality_issues}


def test_incompatible_same_book_capture_timestamps_do_not_form_a_pair() -> None:
    summary = odds_summary()
    outcomes = summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    outcomes["LAD"]["offers"][0]["provider_retrieved_at"] = (
        "2026-07-15T23:59:20+00:00"
    )

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=summary,
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert "insufficient_fresh_h2h_books" in {issue.code for issue in game.quality_issues}


def test_weather_offset_beyond_sixty_minutes_is_degraded() -> None:
    packet = weather(forecast_time="2026-07-16T03:00:01+00:00")
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=packet,
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.weather_gate_clear is False
    assert "weather_offset_exceeded" in {issue.code for issue in game.quality_issues}


def test_requested_date_uses_venue_timezone() -> None:
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 16),
        odds_summary=odds_summary(),
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert "requested_date_mismatch" in {issue.code for issue in game.quality_issues}


def test_feature_prediction_view_is_market_blind() -> None:
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(),
        venue=venue(),
        assembled_at=NOW,
    )
    features = assemble_features(game, generated_at=NOW)

    assert features.contract_version == FEATURE_VERSION
    assert "market_context" in features.as_dict()
    authoring = features.prediction_input_view()
    assert "market_context" not in authoring
    rendered = json.dumps(authoring)
    assert "no_vig" not in rendered
    assert "best_price" not in rendered
    assert "edge_percentage_points" not in rendered
    assert "expected_value" not in rendered
    assert features.checksum == assemble_features(game, generated_at=NOW).checksum


def test_unverified_material_venue_metadata_blocks_release_quality() -> None:
    unverified = venue()
    unverified["field_verification"]["latitude"]["status"] = "UNVERIFIED"

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=unverified,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert game.weather_gate_clear is False
    assert "venue_weather_metadata_invalid" in {
        issue.code for issue in game.quality_issues
    }


def test_invalid_material_coordinate_blocks_release_quality() -> None:
    invalid = venue()
    invalid["latitude"] = 91.0

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=invalid,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert game.weather_gate_clear is False
    assert "venue_weather_metadata_invalid" in {
        issue.code for issue in game.quality_issues
    }


def test_conflicted_material_coordinate_blocks_release_quality() -> None:
    conflicted = venue()
    conflicted["field_verification"]["latitude"].update(
        {
            "status": "UNVERIFIED",
            "conflicts": [
                {
                    "source_id": "conflicting_fixture",
                    "observed_value": 34.0,
                }
            ],
        }
    )

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=conflicted,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert game.weather_gate_clear is False
    issue = next(
        issue
        for issue in game.quality_issues
        if issue.code == "venue_weather_metadata_invalid"
    )
    assert "venue_metadata_conflict:latitude" in issue.message


def test_retractable_roof_remains_operationally_unknown() -> None:
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=venue(roof="retractable"),
        assembled_at=NOW,
    )

    assert game.roof_context == "unknown"
    assert game.weather_gate_clear is False
    assert "retractable_roof_status_unknown" in {
        issue.code for issue in game.quality_issues
    }


def test_open_air_roof_status_is_not_applicable() -> None:
    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=venue(),
        assembled_at=NOW,
    )

    assert game.roof_context == "not_applicable"
    assert game.weather_gate_clear is True


def test_newly_verified_sutter_roof_uses_the_existing_open_weather_gate() -> None:
    athletics_venue = load_stadiums()["ATH"]
    summary = odds_summary()
    summary["home_team_key"] = "ATH"
    summary["raw_home_team"] = "Athletics"
    outcomes = summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    outcomes["ATH"] = outcomes.pop("ARI")
    packet = weather(forecast_time=COMMENCE.isoformat())
    packet["stadium"] = athletics_venue

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=summary,
        weather_packet=packet,
        venue=athletics_venue,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.READY
    assert game.roof_context == "not_applicable"
    assert game.weather_gate_clear is True
    assert "venue_weather_metadata_invalid" not in {
        issue.code for issue in game.quality_issues
    }


def test_unverified_optional_bearing_does_not_block_moneyline_quality() -> None:
    optional_unverified = venue()
    optional_unverified["field_verification"]["outfield_bearing_degrees"][
        "status"
    ] = "UNVERIFIED"

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=optional_unverified,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.READY
    assert game.weather_gate_clear is True
    assert "venue_weather_metadata_invalid" not in {
        issue.code for issue in game.quality_issues
    }


def test_venue_active_club_mismatch_blocks_release_quality() -> None:
    mismatched = venue()
    mismatched["team_key"] = "LAD"

    game = assemble_canonical_game(
        run_id="run-fixture",
        requested_date=date(2026, 7, 15),
        odds_summary=odds_summary(),
        weather_packet=weather(forecast_time=COMMENCE.isoformat()),
        venue=mismatched,
        assembled_at=NOW,
    )

    assert game.quality_state is DataQualityState.BLOCKED
    assert game.weather_gate_clear is False
    assert "venue_team_mismatch" in {issue.code for issue in game.quality_issues}
