from __future__ import annotations

from datetime import datetime, timezone

from app.odds_weather.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_ALTERNATE_MARKET,
    PITCHER_STRIKEOUT_MARKET,
    PitcherStrikeoutOddsEvidenceV1,
    normalize_pitcher_strikeout_odds,
    plan_pitcher_strikeout_odds_request,
    provider_pitcher_key,
)


def _event() -> dict:
    return {
        "id": "event-v7-1",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T02:00:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "last_update": "2026-08-10T01:00:00Z",
                "markets": [
                    {
                        "key": PITCHER_STRIKEOUT_MARKET,
                        "last_update": "2026-08-10T01:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": -105, "point": 5.5},
                            {"name": "Under", "description": "Example Starter", "price": -115, "point": 5.5},
                        ],
                    },
                    {
                        "key": PITCHER_STRIKEOUT_ALTERNATE_MARKET,
                        "last_update": "2026-08-10T01:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": 125, "point": 6.5},
                            {"name": "Under", "description": "Example Starter", "price": -155, "point": 6.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "last_update": "2026-08-10T01:00:20Z",
                "markets": [
                    {
                        "key": PITCHER_STRIKEOUT_MARKET,
                        "last_update": "2026-08-10T01:00:20Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": -110, "point": 5.5},
                            {"name": "Under", "description": "Example Starter", "price": -110, "point": 5.5},
                        ],
                    },
                    {
                        "key": PITCHER_STRIKEOUT_ALTERNATE_MARKET,
                        "last_update": "2026-08-10T01:00:20Z",
                        "outcomes": [
                            {"name": "Over", "description": "Example Starter", "price": 130, "point": 6.5},
                            {"name": "Under", "description": "Example Starter", "price": -160, "point": 6.5},
                        ],
                    },
                ],
            },
        ],
    }


def test_v7_request_plan_uses_one_event_call_and_two_market_maximum() -> None:
    plan = plan_pitcher_strikeout_odds_request("event-v7-1", regions="us")
    assert plan.planned_event_odds_requests == 1
    assert plan.market_keys == (PITCHER_STRIKEOUT_MARKET, PITCHER_STRIKEOUT_ALTERNATE_MARKET)
    assert plan.maximum_odds_credits == 2
    assert "discovery" not in str(plan.as_dict()).casefold()


def test_v7_normalization_keeps_multi_book_featured_and_alternate_lines() -> None:
    evidence = PitcherStrikeoutOddsEvidenceV1(
        provider_event_id="event-v7-1",
        retrieved_at=datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event(),
    )
    normalized = normalize_pitcher_strikeout_odds(evidence)
    pitcher_key = provider_pitcher_key("Example Starter")
    pitcher = normalized.summary["pitchers"][pitcher_key]
    assert pitcher["binding_state"] == "provider_description_only"
    assert pitcher["description_variants"] == ["Example Starter"]
    featured = pitcher["markets"][PITCHER_STRIKEOUT_MARKET]["lines"]["5.5"]
    alternate = pitcher["markets"][PITCHER_STRIKEOUT_ALTERNATE_MARKET]["lines"]["6.5"]
    assert featured["outcomes"]["Over"]["best_price"] == -105.0
    assert featured["outcomes"]["Under"]["best_price"] == -110.0
    assert featured["outcomes"]["Over"]["bookmaker_count"] == 2
    assert featured["outcomes"]["Over"]["no_vig_probability"] is not None
    assert alternate["outcomes"]["Over"]["best_price"] == 130.0
    assert alternate["alternate"] is True
    assert normalized.summary["home_team_key"] == "NYY"
    assert normalized.summary["away_team_key"] == "BOS"


def test_v7_normalized_evidence_retains_raw_and_evidence_lineage() -> None:
    evidence = PitcherStrikeoutOddsEvidenceV1(
        provider_event_id="event-v7-1",
        retrieved_at=datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
        raw_capture_checksum="b" * 64,
        event=_event(),
    )
    normalized = normalize_pitcher_strikeout_odds(evidence)
    assert normalized.source_evidence_checksum == evidence.checksum
    assert normalized.source_raw_capture_checksum == "b" * 64
    assert normalized.normalized_market_count == 2
