from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.first_inning import (
    FIRST_INNING_TOTALS_MARKET,
    FirstInningOddsError,
    FirstInningOddsEvidenceV1,
    normalize_first_inning_odds,
    plan_first_inning_odds_requests,
)


def _event() -> dict[str, object]:
    return {
        "id": "eventabc123",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T02:10:00+00:00",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "bookmakers": [
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": FIRST_INNING_TOTALS_MARKET,
                        "last_update": "2026-08-09T21:00:30+00:00",
                        "outcomes": [
                            {"name": "Over", "price": -115, "point": 0.5},
                            {"name": "Under", "price": -105, "point": 0.5},
                        ],
                    }
                ],
            },
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": FIRST_INNING_TOTALS_MARKET,
                        "last_update": "2026-08-09T21:00:30+00:00",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 0.5},
                            {"name": "Under", "price": -110, "point": 0.5},
                        ],
                    }
                ],
            },
        ],
    }


def _evidence() -> FirstInningOddsEvidenceV1:
    return FirstInningOddsEvidenceV1(
        provider_event_id="eventabc123",
        retrieved_at=datetime(2026, 8, 9, 21, 1, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event(),
    )


def test_first_inning_request_plan_is_event_scoped_and_quota_bounded() -> None:
    plan = plan_first_inning_odds_requests(
        ["eventabc123", "eventdef456"],
        regions="us,us2",
    )

    assert plan.market_key == FIRST_INNING_TOTALS_MARKET
    assert plan.planned_event_requests == 2
    assert plan.maximum_quota_credits == 4


def test_first_inning_evidence_rejects_wrong_period_market() -> None:
    event = _event()
    event["bookmakers"][0]["markets"][0]["key"] = "totals"  # type: ignore[index]

    with pytest.raises(FirstInningOddsError, match="unsupported first-inning"):
        FirstInningOddsEvidenceV1(
            provider_event_id="eventabc123",
            retrieved_at=datetime(2026, 8, 9, 21, 1, tzinfo=timezone.utc),
            raw_capture_checksum="a" * 64,
            event=event,
        )


def test_first_inning_normalization_restores_period_namespace_and_best_prices() -> None:
    normalized = normalize_first_inning_odds(_evidence(), run_id="run_v5o")
    summary = normalized.summary

    assert summary["market_period"] == "first_inning"
    assert set(summary["markets"]) == {FIRST_INNING_TOTALS_MARKET}
    assert "totals" not in summary["markets"]
    assert normalized.normalized_market_count == 1
    assert normalized.raw_snapshot_count == 4

    line = summary["markets"][FIRST_INNING_TOTALS_MARKET]["lines"]["0.5"]
    assert line["line"] == 0.5
    assert line["market_key"] == FIRST_INNING_TOTALS_MARKET
    assert line["complete_two_way_market"] is True
    assert line["bookmaker_count"] == 2
    assert line["outcomes"]["Over"]["best_price"] == -110.0
    assert line["outcomes"]["Over"]["best_price_books"] == ["fanduel"]
    assert line["outcomes"]["Under"]["best_price"] == -105.0
    assert line["outcomes"]["Under"]["best_price_books"] == ["draftkings"]
    assert line["outcomes"]["Over"]["no_vig_probability"] is not None
    assert line["outcomes"]["Under"]["no_vig_probability"] is not None


def test_first_inning_normalized_lineage_is_deterministic() -> None:
    evidence = _evidence()
    first = normalize_first_inning_odds(evidence)
    second = normalize_first_inning_odds(evidence)

    assert first.source_evidence_checksum == evidence.checksum
    assert first.source_raw_capture_checksum == "a" * 64
    assert first.checksum == second.checksum
