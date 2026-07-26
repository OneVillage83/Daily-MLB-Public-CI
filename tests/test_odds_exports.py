from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from app.processors.odds_processor import (
    ConsensusThresholds,
    FreshnessThresholds,
    OddsWarningCode,
    calculate_line_movement,
    process_game,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _iso(seconds_ago: int) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _book(
    key: str,
    markets: list[dict[str, Any]],
    *,
    updated: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"key": key, "title": key.title(), "markets": markets}
    if updated is not None:
        result["last_update"] = updated
    return result


def _game(bookmakers: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "id": "event-odds-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-10T23:00:00Z",
        "home_team": "Arizona Diamondbacks",
        "away_team": "Los Angeles Dodgers",
        "bookmakers": bookmakers,
        **extra,
    }


def _h2h(home: int, away: int, *, updated: str | None = None) -> dict[str, Any]:
    market: dict[str, Any] = {
        "key": "h2h",
        "outcomes": [
            {"name": "Arizona Diamondbacks", "price": home},
            {"name": "Los Angeles Dodgers", "price": away},
        ],
    }
    if updated is not None:
        market["last_update"] = updated
    return market


def _spread(point: float, home: int = -110, away: int = -110) -> dict[str, Any]:
    return {
        "key": "spreads",
        "outcomes": [
            {"name": "Arizona Diamondbacks", "point": point, "price": home},
            {"name": "Los Angeles Dodgers", "point": -point, "price": away},
        ],
    }


def _total(point: float, over: int = -110, under: int = -110) -> dict[str, Any]:
    return {
        "key": "totals",
        "outcomes": [
            {"name": "Over", "point": point, "price": over},
            {"name": "Under", "point": point, "price": under},
        ],
    }


@pytest.mark.parametrize(
    ("age", "expected"),
    [(60, "fresh"), (180, "aging"), (360, "stale")],
)
def test_freshness_statuses_use_market_then_bookmaker(age: int, expected: str) -> None:
    market = _h2h(-110, -110, updated=_iso(age))
    result = process_game(
        _game([_book("book", [market], updated=_iso(600))]),
        run_id="run-test",
        retrieved_at=NOW.isoformat(),
    )

    annotated_market = result.annotated_game["bookmakers"][0]["markets"][0]
    assert annotated_market["freshness_status"] == expected
    assert annotated_market["market_age_seconds"] == pytest.approx(age)
    assert annotated_market["bookmaker_age_seconds"] == pytest.approx(600)


def test_missing_timestamps_are_unknown_and_provider_age_is_null() -> None:
    result = process_game(
        _game([_book("book", [_h2h(-110, -110)])]),
        run_id="run-test",
        retrieved_at=NOW.isoformat(),
    )

    market = result.annotated_game["bookmakers"][0]["markets"][0]
    assert market["provider_age_seconds"] is None
    assert market["bookmaker_age_seconds"] is None
    assert market["market_age_seconds"] is None
    assert market["freshness_status"] == "unknown"
    assert "missing_provider_timestamp" in {warning["code"] for warning in result.warnings}


@pytest.mark.parametrize(
    ("timestamp", "code"),
    [
        ("not-a-timestamp", OddsWarningCode.INVALID_PROVIDER_TIMESTAMP.value),
        ((NOW + timedelta(seconds=31)).isoformat(), OddsWarningCode.FUTURE_PROVIDER_TIMESTAMP.value),
    ],
)
def test_invalid_and_future_provider_timestamp_warnings(timestamp: str, code: str) -> None:
    result = process_game(
        _game([_book("book", [_h2h(-110, -110, updated=timestamp)])]),
        run_id="run-test",
        retrieved_at=NOW.isoformat(),
    )

    assert code in {warning["code"] for warning in result.warnings}
    assert result.annotated_game["bookmakers"][0]["markets"][0]["freshness_status"] == "unknown"


def test_small_future_skew_is_clamped_to_fresh() -> None:
    market = _h2h(-110, -110, updated=(NOW + timedelta(seconds=20)).isoformat())
    result = process_game(
        _game([_book("book", [market])]),
        retrieved_at=NOW.isoformat(),
        freshness_thresholds=FreshnessThresholds(future_tolerance_seconds=30),
    )

    annotated = result.annotated_game["bookmakers"][0]["markets"][0]
    assert annotated["market_age_seconds"] == 0
    assert annotated["freshness_status"] == "fresh"


def test_best_price_supports_mixed_signs_even_money_and_ties() -> None:
    books = [
        _book("a", [_h2h(-105, -110)], updated=_iso(10)),
        _book("b", [_h2h(100, -110)], updated=_iso(10)),
        _book("c", [_h2h(120, -110)], updated=_iso(10)),
        _book("d", [_h2h(120, -110)], updated=_iso(10)),
    ]
    outcome = process_game(_game(books), retrieved_at=NOW.isoformat()).summary["markets"][
        "h2h"
    ]["lines"]["moneyline"]["outcomes"]["ARI"]

    assert outcome["best_price"] == 120
    assert outcome["best_price_book"] is None
    assert outcome["best_price_books"] == ["c", "d"]
    assert outcome["prices_by_book"]["b"] == 100


def test_h2h_no_vig_is_paired_and_aggregated_per_book() -> None:
    result = process_game(
        _game(
            [
                _book("a", [_h2h(-110, -110)], updated=_iso(10)),
                _book("b", [_h2h(120, -140)], updated=_iso(10)),
            ]
        ),
        retrieved_at=NOW.isoformat(),
    ).summary
    line = result["markets"]["h2h"]["lines"]["moneyline"]

    assert set(line["market_hold_by_book"]) == {"a", "b"}
    assert set(line["outcomes"]["ARI"]["no_vig_probabilities"]) == {"a", "b"}
    assert line["outcomes"]["ARI"]["mean_no_vig_probability"] is not None
    disagreement = line["outcomes"]["ARI"]["no_vig_probability_disagreement"]
    assert disagreement["range"] is not None
    assert disagreement["standard_deviation"] is not None


def test_malformed_three_way_market_is_excluded_from_consensus() -> None:
    market = _h2h(-110, -110)
    market["outcomes"].append({"name": "Draw", "price": 500})
    result = process_game(
        _game([_book("a", [market], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )
    line = result.summary["markets"]["h2h"]["lines"]["moneyline"]

    assert line["outcomes"]["ARI"]["bookmaker_count"] == 0
    assert line["complete_two_way_market"] is False
    assert "malformed_outcome" in {warning["code"] for warning in result.warnings}


def test_malformed_total_is_not_primary_modal_or_alternate() -> None:
    market = _total(8.5)
    market["outcomes"].append({"name": "Draw", "point": 8.5, "price": 500})
    result = process_game(
        _game([_book("a", [market], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )
    totals = result.summary["markets"]["totals"]

    assert totals["primary_market_point"] is None
    assert totals["alternate_market_points"] == []
    assert totals["point_disagreement"]["unique_point_count"] == 0


def test_h2h_point_on_either_side_excludes_book_from_two_way_math() -> None:
    market = _h2h(-110, -110)
    market["outcomes"][1]["point"] = 0
    result = process_game(
        _game([_book("a", [market], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )
    line = result.summary["markets"]["h2h"]["lines"]["moneyline"]

    assert line["complete_two_way_market"] is False
    assert line["outcomes"]["ARI"]["bookmaker_count"] == 0
    assert "malformed_outcome" in {warning["code"] for warning in result.warnings}


def test_valid_spread_pair_and_invalid_spread_pair() -> None:
    valid = process_game(
        _game([_book("a", [_spread(-1.5)], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )
    assert valid.summary["markets"]["spreads"]["lines"]["-1.5"][
        "complete_two_way_market"
    ]

    invalid_market = {
        "key": "spreads",
        "outcomes": [
            {"name": "Arizona Diamondbacks", "point": -1.5, "price": -110},
            {"name": "Los Angeles Dodgers", "point": 2.5, "price": -110},
        ],
    }
    invalid = process_game(
        _game([_book("a", [invalid_market], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )
    assert "invalid_spread_pair" in {warning["code"] for warning in invalid.warnings}


def test_total_pair_requires_identical_point() -> None:
    mismatched = {
        "key": "totals",
        "outcomes": [
            {"name": "Over", "point": 8.0, "price": -110},
            {"name": "Under", "point": 8.5, "price": -110},
        ],
    }
    result = process_game(
        _game([_book("a", [mismatched], updated=_iso(10))]),
        retrieved_at=NOW.isoformat(),
    )

    assert "mismatched_total_pair" in {warning["code"] for warning in result.warnings}
    assert all(
        not line["complete_two_way_market"]
        for line in result.summary["markets"]["totals"]["lines"].values()
    )


def test_consensus_uses_probability_space_and_thresholds() -> None:
    result = process_game(
        _game(
            [
                _book("a", [_h2h(100, -110)], updated=_iso(10)),
                _book("b", [_h2h(200, -110)], updated=_iso(10)),
                _book("c", [_h2h(-200, -110)], updated=_iso(10)),
            ]
        ),
        retrieved_at=NOW.isoformat(),
        consensus_thresholds=ConsensusThresholds(2, 4, 7),
    )
    outcome = result.summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["ARI"]

    assert outcome["mean_implied_probability"] == pytest.approx(0.5)
    assert outcome["median_implied_probability"] == pytest.approx(0.5)
    assert outcome["consensus_confidence"] == "low"
    assert outcome["median_american_price_display_only"] == 100


def test_duplicate_offer_warns_and_does_not_double_weight_book() -> None:
    result = process_game(
        _game(
            [
                _book(
                    "a",
                    [_h2h(-110, -110, updated=_iso(20)), _h2h(120, -110, updated=_iso(10))],
                )
            ]
        ),
        run_id="run-test",
        retrieved_at=NOW.isoformat(),
    )
    outcome = result.summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["ARI"]

    assert outcome["bookmaker_count"] == 1
    assert outcome["prices_by_book"] == {"a": 120.0}
    assert "duplicate_normalized_offer" in {warning["code"] for warning in result.warnings}


def test_primary_line_uses_modal_book_count() -> None:
    books = [
        _book("a", [_total(8.5), _total(9.0)], updated=_iso(10)),
        _book("b", [_total(8.5)], updated=_iso(10)),
        _book("c", [_total(9.0)], updated=_iso(10)),
        _book("d", [_total(8.5)], updated=_iso(10)),
    ]
    market = process_game(_game(books), retrieved_at=NOW.isoformat()).summary["markets"][
        "totals"
    ]

    assert market["primary_market_point"] == 8.5
    assert market["alternate_market_points"] == [9.0]
    assert market["point_disagreement"]["unique_point_count"] == 2
    assert market["point_disagreement"]["point_range"] == 0.5


def test_primary_line_tie_uses_median_recency_not_single_newest_book() -> None:
    books = [
        _book("a", [{**_total(8.5), "last_update": _iso(100)}]),
        _book("b", [{**_total(8.5), "last_update": _iso(100)}]),
        _book("c", [{**_total(9.0), "last_update": _iso(1)}]),
        _book("d", [{**_total(9.0), "last_update": _iso(500)}]),
    ]
    result = process_game(_game(books), retrieved_at=NOW.isoformat())

    assert result.summary["markets"]["totals"]["primary_market_point"] == 8.5
    assert "primary_line_tie" in {warning["code"] for warning in result.warnings}


def test_primary_line_equal_recency_uses_lowest_numeric_point() -> None:
    result = process_game(
        _game(
            [
                _book("a", [_spread(-1.5)], updated=_iso(10)),
                _book("b", [_spread(-2.5)], updated=_iso(10)),
            ]
        ),
        retrieved_at=NOW.isoformat(),
    )

    assert result.summary["markets"]["spreads"]["primary_market_point"] == -2.5


def test_probability_disagreement_metrics() -> None:
    result = process_game(
        _game(
            [
                _book("a", [_h2h(100, -110)], updated=_iso(10)),
                _book("b", [_h2h(200, -110)], updated=_iso(10)),
            ]
        ),
        retrieved_at=NOW.isoformat(),
    ).summary
    disagreement = result["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["ARI"][
        "implied_probability_disagreement"
    ]

    assert disagreement["min"] == pytest.approx(1 / 3)
    assert disagreement["max"] == pytest.approx(0.5)
    assert disagreement["range"] == pytest.approx(1 / 6)
    assert disagreement["standard_deviation"] == pytest.approx(1 / 12)


def _history_row(
    row_id: int,
    retrieved: str,
    *,
    price: int,
    point: float | None = None,
    outcome: str = "Over",
    market: str = "totals",
    run_id: str = "run-a",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "run_id": run_id,
        "event_id": "event-odds-1",
        "bookmaker_key": "book",
        "market_key": market,
        "outcome_name": outcome,
        "point": point,
        "price": price,
        "retrieved_at": retrieved,
        "home_team": "Arizona Diamondbacks",
        "away_team": "Los Angeles Dodgers",
        "bookmaker_last_update": retrieved,
        "market_last_update": None,
    }


def test_one_observation_has_no_movement() -> None:
    result = calculate_line_movement([_history_row(1, _iso(60), price=-110, point=8.5)])
    offer = result["offers"][0]

    assert offer["observation_count"] == 1
    assert offer["american_price_change"] is None
    assert offer["implied_probability_change"] is None


def test_repeated_identical_observations_are_counted() -> None:
    result = calculate_line_movement(
        [
            _history_row(1, _iso(60), price=-110, point=8.5),
            _history_row(2, _iso(30), price=-110, point=8.5, run_id="run-b"),
        ]
    )
    offer = result["offers"][0]

    assert offer["observation_count"] == 2
    assert offer["american_price_change"] == 0
    assert offer["implied_probability_change"] == 0


def test_duplicate_offers_in_one_capture_do_not_create_movement() -> None:
    result = calculate_line_movement(
        [
            _history_row(1, _iso(60), price=-110, point=8.5),
            _history_row(2, _iso(60), price=120, point=8.5),
        ]
    )
    offer = result["offers"][0]

    assert offer["observation_count"] == 1
    assert offer["first_observed_price"] == 120
    assert offer["american_price_change"] is None


def test_primary_history_duplicate_uses_newest_effective_timestamp() -> None:
    newer = {
        **_history_row(1, _iso(60), price=100, point=8.5, outcome="Over"),
        "market_last_update": _iso(10),
    }
    stale_but_later_row = {
        **_history_row(2, _iso(60), price=-200, point=8.5, outcome="Over"),
        "market_last_update": _iso(100),
    }
    under = _history_row(3, _iso(60), price=-110, point=8.5, outcome="Under")

    movement = calculate_line_movement(
        [newer, stale_but_later_row, under]
    )["market_primary_line_movement"][0]

    assert movement["probability_movement_by_side"]["Over"][
        "first_observed_probability"
    ] == pytest.approx(0.5)


def test_malformed_and_invalid_rows_are_raw_only_for_offer_movement() -> None:
    rows = [
        _history_row(1, _iso(60), price=500, point=8.5, outcome="Draw"),
        _history_row(2, _iso(30), price=0, point=9.0, outcome="Over", run_id="run-b"),
        _history_row(
            3,
            _iso(20),
            price=-110,
            point=0,
            outcome="Arizona Diamondbacks",
            market="h2h",
            run_id="run-c",
        ),
    ]

    assert calculate_line_movement(rows)["offers"] == []


def test_price_movement_emits_implied_probability_change() -> None:
    result = calculate_line_movement(
        [
            _history_row(1, _iso(60), price=100, point=8.5),
            _history_row(2, _iso(30), price=200, point=8.5, run_id="run-b"),
        ]
    )
    offer = result["offers"][0]

    assert offer["american_price_change"] == 100
    assert offer["first_implied_probability"] == pytest.approx(0.5)
    assert offer["latest_implied_probability"] == pytest.approx(1 / 3)
    assert offer["implied_probability_change"] == pytest.approx(-1 / 6)


def test_primary_point_and_probability_movement_are_distinct() -> None:
    rows = [
        _history_row(1, _iso(60), price=-110, point=8.5, run_id="run-a"),
        _history_row(2, _iso(60), price=-110, point=8.5, outcome="Under", run_id="run-a"),
        _history_row(3, _iso(30), price=100, point=9.0, run_id="run-b"),
        _history_row(4, _iso(30), price=-120, point=9.0, outcome="Under", run_id="run-b"),
    ]
    movement = calculate_line_movement(rows)["market_primary_line_movement"][0]

    assert movement["first_observed_primary_point"] == 8.5
    assert movement["latest_observed_primary_point"] == 9.0
    assert movement["point_movement"] == 0.5
    assert movement["probability_movement_by_side"]["Over"]["probability_movement"] is not None


def test_primary_point_can_move_without_probability_movement() -> None:
    rows = [
        _history_row(1, _iso(60), price=-110, point=8.5, run_id="run-a"),
        _history_row(2, _iso(60), price=-110, point=8.5, outcome="Under", run_id="run-a"),
        _history_row(3, _iso(30), price=-110, point=9.0, run_id="run-b"),
        _history_row(4, _iso(30), price=-110, point=9.0, outcome="Under", run_id="run-b"),
    ]
    movement = calculate_line_movement(rows)["market_primary_line_movement"][0]

    assert movement["point_movement"] == 0.5
    assert movement["probability_movement_by_side"]["Over"]["probability_movement"] == 0


def test_movement_normalizes_team_aliases_across_captures() -> None:
    rows = [
        {
            **_history_row(
                1,
                _iso(60),
                price=-110,
                point=-1.5,
                outcome="Oakland Athletics",
                market="spreads",
                run_id="run-a",
            ),
            "home_team": "Athletics",
            "away_team": "Los Angeles Dodgers",
            "home_team_key": "ATH",
            "away_team_key": "LAD",
        },
        {
            **_history_row(
                2,
                _iso(30),
                price=100,
                point=-1.5,
                outcome="Athletics",
                market="spreads",
                run_id="run-b",
            ),
            "home_team": "Athletics",
            "away_team": "Los Angeles Dodgers",
            "home_team_key": "ATH",
            "away_team_key": "LAD",
        },
    ]
    result = calculate_line_movement(rows)
    offers = result["offers"]

    assert len(offers) == 1
    assert offers[0]["side"] == "ATH"
    assert offers[0]["observation_count"] == 2
    assert result["market_primary_line_movement"][0]["observation_count"] == 2


def test_invalid_historical_prices_and_three_way_rows_do_not_select_primary() -> None:
    rows = [
        _history_row(1, _iso(60), price=0, point=8.0, outcome="Over"),
        _history_row(2, _iso(60), price=-110, point=8.0, outcome="Under"),
        _history_row(3, _iso(60), price=500, point=8.0, outcome="Draw"),
        {
            **_history_row(4, _iso(60), price=-110, point=8.5, outcome="Over"),
            "bookmaker_key": "valid-book",
        },
        {
            **_history_row(5, _iso(60), price=-110, point=8.5, outcome="Under"),
            "bookmaker_key": "valid-book",
        },
    ]
    movement = calculate_line_movement(rows)["market_primary_line_movement"]

    assert movement[0]["first_observed_primary_point"] == 8.5


def test_export_contract_has_numeric_values_and_required_sections() -> None:
    summary = process_game(
        _game([_book("a", [_h2h(-110, -110), _spread(-1.5), _total(8.5)], updated=_iso(10))]),
        run_id="run-test",
        retrieved_at=NOW.isoformat(),
    ).summary

    assert summary["contract_version"] == "odds-consensus-v2"
    assert summary["event_id"] == "event-odds-1"
    assert summary["raw_home_team"] == "Arizona Diamondbacks"
    assert summary["home_team_key"] == "ARI"
    assert isinstance(summary["markets"]["totals"]["primary_market_point"], float)
    assert isinstance(
        summary["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["ARI"][
            "best_price"
        ],
        float,
    )
    assert "provider_freshness_summary" in summary
    assert "line_movement" in summary
    assert isinstance(summary["warnings"], list)


def test_warning_enum_matches_schema_constraint() -> None:
    from app.migrations import ODDS_WARNING_CODES

    assert {warning.value for warning in OddsWarningCode} == set(ODDS_WARNING_CODES)


def test_pipeline_writes_hardened_odds_export_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.artifacts import ArtifactPaths
    from app.collectors.odds_collector import OddsCollectionResult, OddsCollectorWarning
    from app.config import Settings
    from app.database import Database
    from app.http import HttpRequestDiagnostics
    from app.identifiers import generate_run_id
    from app.pipeline import run_collection
    from app.raw_payloads import RawPayloadCapture
    from app.run_state import RunStatus

    requested_date = NOW.date()
    run_id = generate_run_id(requested_date)
    game = {
        "id": "event-pipeline-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-07-10T23:00:00Z",
        "home_team": "Tampa Bay Rays",
        "away_team": "New York Yankees",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": _iso(30),
                "markets": [
                    {
                        "key": "h2h",
                        "last_update": _iso(20),
                        "outcomes": [
                            {"name": "Tampa Bay Rays", "price": 120},
                            {"name": "New York Yankees", "price": -140},
                        ],
                    },
                    {
                        "key": "spreads",
                        "last_update": _iso(20),
                        "outcomes": [
                            {"name": "Tampa Bay Rays", "price": -110, "point": 1.5},
                            {"name": "New York Yankees", "price": -110, "point": -1.5},
                        ],
                    },
                    {
                        "key": "totals",
                        "last_update": _iso(20),
                        "outcomes": [
                            {"name": "Over", "price": -105, "point": 8.5},
                            {"name": "Under", "price": -115, "point": 8.5},
                        ],
                    },
                ],
            }
        ],
    }
    unknown_home_game = {
        **game,
        "id": "event-unknown-home",
        "home_team": "Unknown MLB Club",
    }
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload=[game, unknown_home_game],
        retrieved_at=NOW.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
    )
    odds_result = OddsCollectionResult(
        games=[game, unknown_home_game],
        quota={"requests_remaining": "99", "requests_used": "1", "requests_last": "1"},
        capture=capture,
        request=HttpRequestDiagnostics(
            request_status="success",
            status_code=200,
            attempts=2,
            retries_performed=1,
            duration_seconds=1.25,
            response_date_utc="2026-07-10T12:00:01+00:00",
        ),
        warnings=(
            OddsCollectorWarning(
                code="malformed_outcome",
                message="Malformed provider outcome was excluded",
                created_at=NOW.isoformat(),
                event_id="event-pipeline-1",
                bookmaker="book-a",
                market="totals",
            ),
        ),
    )

    class FakeOddsCollector:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def collect(self) -> OddsCollectionResult:
            return odds_result

    monkeypatch.setattr("app.pipeline.OddsCollector", FakeOddsCollector)
    verified = {
        "status": "VERIFIED",
        "source_ids": ["deterministic_fixture"],
        "verified_at": "2026-07-12",
        "notes": "Deterministic pipeline fixture.",
        "conflicts": [],
    }
    fixture_stadium: dict[str, Any] = {
        "physical_venue_key": "fixture-fixed-park",
        "venue": "Fixture Fixed Park",
        "team_key": "TB",
        "active_club_association": True,
        "latitude": 27.7682,
        "longitude": -82.6534,
        "timezone": "America/New_York",
        "roof_type": "fixed",
        "outfield_bearing_degrees": None,
        "field_verification": {
            field: dict(verified)
            for field in (
                "physical_venue_key",
                "active_club_association",
                "latitude",
                "longitude",
                "timezone",
                "roof_type",
            )
        },
    }
    for field in (
        "latitude",
        "longitude",
        "outfield_bearing_degrees",
    ):
        fixture_stadium["field_verification"][field] = {
            **verified,
            "status": "UNVERIFIED",
        }
    monkeypatch.setattr(
        "app.pipeline.stadium_for_team",
        lambda key: fixture_stadium if key == "TB" else None,
    )
    nws_calls: list[object] = []

    def unexpected_nws_call(*args: object, **kwargs: object) -> None:
        nws_calls.append((args, kwargs))
        raise AssertionError("NWS must not run with unverified coordinates")

    monkeypatch.setattr(
        "app.pipeline.NwsWeatherCollector.collect", unexpected_nws_call
    )
    settings = Settings(
        database_path=tmp_path / "daily.db",
        artifact_dir=tmp_path / "artifacts",
        odds_api_key="pipeline-secret-not-real",
        service_auth_token="service-secret-not-real",
        openweather_enabled=False,
    )
    database = Database(settings.database_path)
    database.create_run(run_id, requested_date)
    database.transition_run(run_id, RunStatus.RUNNING, transitioned_at=NOW.isoformat())
    artifacts = ArtifactPaths(settings.artifact_dir, requested_date, run_id)

    result = run_collection(run_id, requested_date, settings, database, artifacts)

    consensus = json.loads(artifacts.json_path("odds_consensus.json").read_text(encoding="utf-8"))
    raw_export = json.loads(
        artifacts.json_path("odds_raw_all_upcoming.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        artifacts.json_path("collection_manifest.json").read_text(encoding="utf-8")
    )
    weather_export = json.loads(
        artifacts.json_path("weather.json").read_text(encoding="utf-8")
    )
    source = manifest["sources"]["the_odds_api"]
    assert result.status is RunStatus.COMPLETED_WITH_WARNINGS
    assert consensus[0]["contract_version"] == "odds-consensus-v2"
    assert raw_export[0]["bookmakers"][0]["key"] == "book-a"
    assert [market["key"] for market in raw_export[0]["bookmakers"][0]["markets"]] == [
        "h2h",
        "spreads",
        "totals",
    ]
    assert consensus[0]["markets"]["totals"]["primary_market_point"] == 8.5
    assert any(
        warning["code"] == "malformed_outcome" for warning in consensus[0]["warnings"]
    )
    assert consensus[0]["line_movement"]["offers"][0]["observation_count"] == 1
    assert source["raw_snapshot_count"] == 12
    assert source["provider_event_count"] == 2
    assert source["event_count"] == 1
    assert source["normalized_market_count"] == 6
    assert source["bookmaker_count"] == 1
    assert source["freshness_counts"]["fresh"] == 6
    assert source["quota_headers"]["requests_remaining"] == "99"
    assert source["retries_performed"] == 1
    assert source["request_duration_seconds"] == 1.25
    assert source["response_date_utc"] == "2026-07-10T12:00:01+00:00"
    assert nws_calls == []
    assert weather_export[0]["event_id"] == "event-pipeline-1"
    assert (
        weather_export[0]["weather_status"] == "indoor_fixed_roof"
    )
    assert "pipeline-secret-not-real" not in json.dumps(manifest)
    errors = json.loads(
        artifacts.json_path("collection_errors.json").read_text(encoding="utf-8")
    )
    assert any(error.get("code") == "unknown_team" for error in errors)
    with database.connect() as connection:
        snapshot_count = connection.execute(
            "SELECT COUNT(*) FROM odds_snapshots"
        ).fetchone()[0]
        snapshot = connection.execute(
            "SELECT provider_age_seconds, market_age_seconds, freshness_status "
            "FROM odds_snapshots LIMIT 1"
        ).fetchone()
    assert snapshot_count == 12
    assert snapshot["provider_age_seconds"] is None
    assert snapshot["market_age_seconds"] == pytest.approx(20)
    assert snapshot["freshness_status"] == "fresh"


def test_failed_payload_validation_retains_request_diagnostics_in_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.collectors.odds_collector import OddsCollectorWarning, OddsPayloadError
    from app.config import Settings
    from app.database import Database
    from app.http import HttpRequestDiagnostics
    from app.identifiers import generate_run_id
    from app.jobs import JobRunner
    from app.raw_payloads import RawPayloadCapture

    requested_date = NOW.date()
    run_id = generate_run_id(requested_date)
    secret = "failed-manifest-secret-not-real"
    capture = RawPayloadCapture(
        provider="the_odds_api",
        endpoint_category="mlb_odds",
        payload={"message": "malformed"},
        retrieved_at=NOW.isoformat(),
        provider_timestamp=None,
        content_type="application/json",
    )
    diagnostics = HttpRequestDiagnostics(
        request_status="success",
        status_code=200,
        attempts=2,
        retries_performed=1,
        duration_seconds=2.5,
        response_date_utc="2026-07-10T12:00:00+00:00",
    )

    class FailingOddsCollector:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def collect(self) -> object:
            raise OddsPayloadError(
                f"Invalid payload?apiKey={secret}",
                capture=capture,
                request=diagnostics,
                quota={"requests_remaining": "98", "requests_used": secret},
                warnings=(
                    OddsCollectorWarning(
                        code="malformed_event",
                        message="Malformed event excluded",
                        created_at=NOW.isoformat(),
                        event_id="bad-event",
                    ),
                ),
            )

    monkeypatch.setattr("app.pipeline.OddsCollector", FailingOddsCollector)
    settings = Settings(
        database_path=tmp_path / "failed.db",
        artifact_dir=tmp_path / "artifacts",
        odds_api_key=secret,
        service_auth_token="service-secret-not-real",
        openweather_enabled=False,
    )
    Database(settings.database_path).create_run(run_id, requested_date)
    runner = JobRunner(settings)
    try:
        runner.execute_now(run_id, requested_date)
    finally:
        runner.shutdown()

    manifest_path = settings.artifact_dir / requested_date.isoformat() / run_id / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source = manifest["sources"]["the_odds_api"]
    assert manifest["status"] == "failed"
    assert source["request_status"] == "success"
    assert source["status_code"] == 200
    assert source["retries_performed"] == 1
    assert source["quota_headers"]["requests_remaining"] == "98"
    assert source["warning_count"] == 1
    assert manifest["warning_count"] == 1
    assert secret not in json.dumps(manifest)
    stored_diagnostic = Database(settings.database_path).get_latest_error(
        run_id,
        stage="odds_request",
    )
    assert stored_diagnostic is not None
    assert secret not in str(stored_diagnostic["details"])
