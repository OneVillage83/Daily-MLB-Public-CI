from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.team_totals import (
    TEAM_TOTAL_NORMALIZED_MARKET_KEY,
    TEAM_TOTAL_ODDS_MARKETS,
    TeamTotalsOddsError,
    TeamTotalsOddsEvidenceV1,
    normalize_team_totals_odds,
    plan_team_totals_odds_requests,
)


def _event() -> dict[str, object]:
    return {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-08-10T23:10:00Z",
        "away_team": "New York Yankees",
        "home_team": "Boston Red Sox",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "team_totals",
                        "last_update": "2026-08-10T22:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "New York Yankees", "price": -105, "point": 4.5},
                            {"name": "Under", "description": "New York Yankees", "price": -115, "point": 4.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -110, "point": 5.0},
                            {"name": "Under", "description": "Boston Red Sox", "price": -110, "point": 5.0},
                        ],
                    },
                    {
                        "key": "alternate_team_totals",
                        "last_update": "2026-08-10T22:00:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "New York Yankees", "price": -160, "point": 3.5},
                            {"name": "Under", "description": "New York Yankees", "price": 130, "point": 3.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -135, "point": 4.5},
                            {"name": "Under", "description": "Boston Red Sox", "price": 105, "point": 4.5},
                        ],
                    },
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "team_totals",
                        "last_update": "2026-08-10T22:01:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "New York Yankees", "price": -110, "point": 4.5},
                            {"name": "Under", "description": "New York Yankees", "price": -110, "point": 4.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -105, "point": 5.0},
                            {"name": "Under", "description": "Boston Red Sox", "price": -115, "point": 5.0},
                        ],
                    },
                    {
                        "key": "alternate_team_totals",
                        "last_update": "2026-08-10T22:01:00Z",
                        "outcomes": [
                            {"name": "Over", "description": "New York Yankees", "price": -155, "point": 3.5},
                            {"name": "Under", "description": "New York Yankees", "price": 125, "point": 3.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -130, "point": 4.5},
                            {"name": "Under", "description": "Boston Red Sox", "price": 100, "point": 4.5},
                        ],
                    },
                ],
            },
        ],
    }


def _evidence(event: dict[str, object] | None = None) -> TeamTotalsOddsEvidenceV1:
    return TeamTotalsOddsEvidenceV1(
        provider_event_id="event-1",
        retrieved_at=datetime(2026, 8, 10, 22, 2, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event() if event is None else event,
    )


def test_team_total_request_plan_uses_two_event_markets_and_one_credit_per_event() -> None:
    plan = plan_team_totals_odds_requests(["event-1", "event-2"], regions="us,us2")

    assert plan.market_keys == TEAM_TOTAL_ODDS_MARKETS
    assert plan.planned_event_requests == 2
    assert plan.maximum_quota_credits == 2


def test_team_total_evidence_rejects_non_event_team_description() -> None:
    event = _event()
    bookmakers = event["bookmakers"]
    assert isinstance(bookmakers, list)
    first_book = bookmakers[0]
    assert isinstance(first_book, dict)
    markets = first_book["markets"]
    assert isinstance(markets, list)
    first_market = markets[0]
    assert isinstance(first_market, dict)
    outcomes = first_market["outcomes"]
    assert isinstance(outcomes, list)
    first_outcome = outcomes[0]
    assert isinstance(first_outcome, dict)
    first_outcome["description"] = "Los Angeles Dodgers"

    with pytest.raises(TeamTotalsOddsError, match="identify one event team"):
        _evidence(event)


def test_normalization_splits_away_and_home_team_markets_losslessly() -> None:
    evidence = _evidence()
    normalized = normalize_team_totals_odds(evidence)

    assert normalized.provider_event_id == "event-1"
    assert (normalized.away_team_id, normalized.home_team_id) == ("NYY", "BOS")
    assert tuple(team.team_id for team in normalized.teams) == ("NYY", "BOS")
    assert normalized.source_evidence_checksum == evidence.checksum
    assert normalized.source_raw_capture_checksum == evidence.raw_capture_checksum
    assert normalized.raw_snapshot_count == 16
    assert normalized.normalized_market_count == 4


def test_normalization_reuses_multi_book_best_price_and_no_vig_math() -> None:
    normalized = normalize_team_totals_odds(_evidence())
    yankees = normalized.for_team("NYY")
    red_sox = normalized.for_team("BOS")

    yankees_market = yankees.summary["markets"][TEAM_TOTAL_NORMALIZED_MARKET_KEY]
    yankees_line = yankees_market["lines"]["4.5"]
    assert yankees_line["bookmaker_count"] == 2
    assert yankees_line["complete_two_way_market"] is True
    assert yankees_line["outcomes"]["Over"]["best_price"] == -105.0
    assert yankees_line["outcomes"]["Over"]["best_price_book"] == "draftkings"
    assert yankees_line["outcomes"]["Under"]["best_price"] == -110.0
    assert yankees_line["outcomes"]["Under"]["best_price_book"] == "fanduel"
    assert yankees_line["outcomes"]["Over"]["no_vig_probability"] is not None
    assert yankees_line["outcomes"]["Under"]["no_vig_probability"] is not None

    red_sox_market = red_sox.summary["markets"][TEAM_TOTAL_NORMALIZED_MARKET_KEY]
    red_sox_line = red_sox_market["lines"]["5"]
    assert red_sox_line["bookmaker_count"] == 2
    assert red_sox_line["outcomes"]["Over"]["best_price"] == -105.0
    assert red_sox_line["outcomes"]["Over"]["best_price_book"] == "fanduel"


def test_alternate_team_total_lines_remain_separate_subject_lines() -> None:
    normalized = normalize_team_totals_odds(_evidence())
    yankees = normalized.for_team("NYY")
    red_sox = normalized.for_team("BOS")

    yankees_lines = yankees.summary["markets"][TEAM_TOTAL_NORMALIZED_MARKET_KEY]["lines"]
    red_sox_lines = red_sox.summary["markets"][TEAM_TOTAL_NORMALIZED_MARKET_KEY]["lines"]

    assert set(yankees_lines) == {"3.5", "4.5"}
    assert set(red_sox_lines) == {"4.5", "5"}
    assert yankees.summary["subject_team_id"] == "NYY"
    assert red_sox.summary["subject_team_id"] == "BOS"
