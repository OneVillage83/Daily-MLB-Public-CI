from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.first_five import (
    FIRST_FIVE_ODDS_MARKETS,
    FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION,
    FirstFiveOddsError,
    FirstFiveOddsEvidenceV1,
    normalize_first_five_odds,
    plan_first_five_odds_requests,
)


def _market(key: str, outcomes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "key": key,
        "last_update": "2026-08-09T21:00:30+00:00",
        "outcomes": outcomes,
    }


def _book(
    key: str,
    title: str,
    *,
    lad_ml: int,
    sf_ml: int,
    lad_spread: int,
    sf_spread: int,
    over: int,
    under: int,
) -> dict[str, object]:
    return {
        "key": key,
        "title": title,
        "markets": [
            _market(
                "h2h_1st_5_innings",
                [
                    {"name": "Los Angeles Dodgers", "price": lad_ml},
                    {"name": "San Francisco Giants", "price": sf_ml},
                ],
            ),
            _market(
                "spreads_1st_5_innings",
                [
                    {
                        "name": "Los Angeles Dodgers",
                        "price": lad_spread,
                        "point": -0.5,
                    },
                    {
                        "name": "San Francisco Giants",
                        "price": sf_spread,
                        "point": 0.5,
                    },
                ],
            ),
            _market(
                "totals_1st_5_innings",
                [
                    {"name": "Over", "price": over, "point": 4.5},
                    {"name": "Under", "price": under, "point": 4.5},
                ],
            ),
        ],
    }


def _event() -> dict[str, object]:
    return {
        "id": "eventabc123",
        "sport_key": "baseball_mlb",
        "sport_title": "MLB",
        "commence_time": "2026-08-10T02:10:00+00:00",
        "home_team": "Los Angeles Dodgers",
        "away_team": "San Francisco Giants",
        "bookmakers": [
            _book(
                "draftkings",
                "DraftKings",
                lad_ml=-120,
                sf_ml=110,
                lad_spread=105,
                sf_spread=-125,
                over=-105,
                under=-115,
            ),
            _book(
                "fanduel",
                "FanDuel",
                lad_ml=-115,
                sf_ml=105,
                lad_spread=110,
                sf_spread=-130,
                over=-110,
                under=-110,
            ),
        ],
    }


def _evidence() -> FirstFiveOddsEvidenceV1:
    return FirstFiveOddsEvidenceV1(
        provider_event_id="eventabc123",
        retrieved_at=datetime(2026, 8, 9, 21, 1, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event(),
    )


def test_request_plan_is_event_scoped_multi_market_and_quota_bounded() -> None:
    plan = plan_first_five_odds_requests(
        ["eventabc123", "eventdef456"],
        regions="us,us2",
    )

    assert plan.market_keys == FIRST_FIVE_ODDS_MARKETS
    assert plan.planned_event_requests == 2
    assert plan.maximum_quota_credits == 12
    assert plan.as_dict()["provider_event_ids"] == ["eventabc123", "eventdef456"]


def test_request_plan_rejects_duplicate_events() -> None:
    with pytest.raises(FirstFiveOddsError, match="duplicate"):
        plan_first_five_odds_requests(
            ["eventabc123", "eventabc123"],
            regions="us",
        )


def test_f5_evidence_rejects_full_game_market_keys() -> None:
    event = _event()
    event["bookmakers"][0]["markets"][0]["key"] = "h2h"  # type: ignore[index]

    with pytest.raises(FirstFiveOddsError, match="supported F5 market"):
        FirstFiveOddsEvidenceV1(
            provider_event_id="eventabc123",
            retrieved_at=datetime(2026, 8, 9, 21, 1, tzinfo=timezone.utc),
            raw_capture_checksum="a" * 64,
            event=event,
        )


def test_normalization_reuses_multi_book_math_and_restores_f5_namespace() -> None:
    normalized = normalize_first_five_odds(_evidence(), run_id="run_test")
    summary = normalized.summary

    assert summary["contract_version"] == FIRST_FIVE_ODDS_NORMALIZATION_CONTRACT_VERSION
    assert summary["market_period"] == "first_five"
    assert set(summary["markets"]) == set(FIRST_FIVE_ODDS_MARKETS)
    assert "h2h" not in summary["markets"]
    assert "spreads" not in summary["markets"]
    assert "totals" not in summary["markets"]
    assert normalized.normalized_market_count == 3
    assert normalized.raw_snapshot_count == 12

    moneyline = summary["markets"]["h2h_1st_5_innings"]["lines"]["moneyline"]
    lad = moneyline["outcomes"]["LAD"]
    sf = moneyline["outcomes"]["SF"]

    assert moneyline["complete_two_way_market"] is True
    assert moneyline["bookmaker_count"] == 2
    assert lad["best_price"] == -115.0
    assert lad["best_price_books"] == ["fanduel"]
    assert sf["best_price"] == 110.0
    assert lad["no_vig_probability"] is not None
    assert sf["no_vig_probability"] is not None


def test_spread_and_total_primary_lines_keep_first_five_market_key() -> None:
    summary = normalize_first_five_odds(_evidence()).summary

    spread = summary["markets"]["spreads_1st_5_innings"]["primary_line"]
    total = summary["markets"]["totals_1st_5_innings"]["primary_line"]

    assert spread["market_key"] == "spreads_1st_5_innings"
    assert spread["line"] == -0.5
    assert spread["complete_two_way_market"] is True
    assert total["market_key"] == "totals_1st_5_innings"
    assert total["line"] == 4.5
    assert total["complete_two_way_market"] is True


def test_normalized_evidence_preserves_exact_source_lineage() -> None:
    evidence = _evidence()
    normalized = normalize_first_five_odds(evidence)

    assert normalized.provider_event_id == evidence.provider_event_id
    assert normalized.source_evidence_checksum == evidence.checksum
    assert normalized.source_raw_capture_checksum == "a" * 64
    assert normalized.checksum == normalize_first_five_odds(evidence).checksum
