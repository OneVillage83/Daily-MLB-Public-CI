from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.player_props import (
    PlayerPropsOddsError,
    PlayerPropsOddsEvidenceV1,
    normalize_player_prop_odds,
    plan_player_prop_odds_requests,
)


def _event() -> dict[str, object]:
    return {
        "id": "event123",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T00:05:00Z",
        "home_team": "New York Yankees",
        "away_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "batter_hits",
                        "last_update": "2026-08-09T19:59:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": -105, "point": 1.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -115, "point": 1.5},
                        ],
                    },
                    {
                        "key": "batter_hits_alternate",
                        "last_update": "2026-08-09T19:59:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": 180, "point": 2.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -230, "point": 2.5},
                        ],
                    },
                    {
                        "key": "pitcher_outs",
                        "last_update": "2026-08-09T19:59:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "Gerrit Cole", "price": -110, "point": 17.5},
                            {"name": "Under", "description": "Gerrit Cole", "price": -120, "point": 17.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "batter_hits",
                        "last_update": "2026-08-09T19:59:10Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": -110, "point": 1.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -110, "point": 1.5},
                        ],
                    },
                    {
                        "key": "batter_hits_alternate",
                        "last_update": "2026-08-09T19:59:10Z",
                        "outcomes": [
                            {"name": "Over", "description": "Aaron Judge", "price": 190, "point": 2.5},
                            {"name": "Under", "description": "Aaron Judge", "price": -250, "point": 2.5},
                        ],
                    },
                    {
                        "key": "pitcher_outs",
                        "last_update": "2026-08-09T19:59:10Z",
                        "outcomes": [
                            {"name": "Over", "description": "Gerrit Cole", "price": -105, "point": 17.5},
                            {"name": "Under", "description": "Gerrit Cole", "price": -125, "point": 17.5},
                        ],
                    },
                ],
            },
        ],
    }


def _evidence(event: dict[str, object] | None = None) -> PlayerPropsOddsEvidenceV1:
    return PlayerPropsOddsEvidenceV1(
        provider_event_id="event123",
        retrieved_at=datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event() if event is None else event,
    )


def test_discovery_plan_selects_only_supported_v8_markets_and_accounts_for_quota() -> None:
    plan = plan_player_prop_odds_requests(
        "event123",
        [
            "batter_hits",
            "batter_hits_alternate",
            "pitcher_outs",
            "pitcher_strikeouts",
            "batter_first_home_run",
            "unknown_market",
        ],
        regions="us",
    )
    assert plan.selected_market_keys == (
        "batter_hits",
        "batter_hits_alternate",
        "pitcher_outs",
    )
    assert plan.discovery_request_credits == 1
    assert plan.planned_event_odds_requests == 1
    assert plan.maximum_odds_credits == 3
    assert plan.maximum_total_credits == 4


def test_normalization_groups_players_and_preserves_featured_and_alternate_lines() -> None:
    normalized = normalize_player_prop_odds(_evidence())
    assert normalized.summary["player_binding"] == "provider_description_only"
    players = normalized.summary["players"]
    assert set(players) == {"aaron judge", "gerrit cole"}

    judge = players["aaron judge"]
    assert judge["provider_descriptions"] == ["Aaron Judge"]
    assert set(judge["markets"]) == {"batter_hits", "batter_hits_alternate"}
    featured = judge["markets"]["batter_hits"]
    assert featured["statistic"] == "hits"
    assert featured["alternate"] is False
    line = featured["lines"]["1.5"]
    assert line["complete_two_way_market"] is True
    assert line["outcomes"]["Over"]["bookmaker_count"] == 2
    assert line["outcomes"]["Over"]["best_price"] == -105.0
    assert line["outcomes"]["Over"]["best_price_book"] == "draftkings"
    assert line["outcomes"]["Over"]["no_vig_probability"] is not None

    alternate = judge["markets"]["batter_hits_alternate"]
    assert alternate["alternate"] is True
    assert "2.5" in alternate["lines"]

    cole = players["gerrit cole"]["markets"]["pitcher_outs"]
    assert cole["statistic"] == "outs"
    assert cole["lines"]["17.5"]["outcomes"]["Over"]["best_price"] == -105.0


def test_missing_player_description_is_rejected() -> None:
    event = _event()
    event["bookmakers"][0]["markets"][0]["outcomes"][0].pop("description")
    with pytest.raises(PlayerPropsOddsError, match="player description"):
        _evidence(event)
