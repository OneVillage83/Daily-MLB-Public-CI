from __future__ import annotations

from typing import Any

import pytest

from app.processors.odds_processor import (
    american_to_probability,
    best_price,
    calculate_hold,
    no_vig_probabilities,
    summarize_game,
)


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (100, 0.5),
        (-100, 0.5),
        (200, 1 / 3),
        (-200, 2 / 3),
    ],
)
def test_american_implied_probability(price: int, expected: float) -> None:
    assert american_to_probability(price) == pytest.approx(expected)


@pytest.mark.parametrize(
    "price",
    [None, "", "not-a-price", 0, 99, -99, float("nan"), float("inf"), True],
)
def test_malformed_or_missing_prices_return_none(price: object) -> None:
    assert american_to_probability(price) is None


def test_two_way_no_vig_probabilities_sum_to_one() -> None:
    first = american_to_probability(-110)
    second = american_to_probability(-110)
    result = no_vig_probabilities(first, second)

    assert result is not None
    assert sum(result) == pytest.approx(1.0)
    assert result == pytest.approx((0.5, 0.5))


def test_hold_calculation() -> None:
    first = american_to_probability(-110)
    second = american_to_probability(-110)

    assert calculate_hold(first, second) == pytest.approx((220 / 210) - 1)


def test_positive_odds_best_price_selection() -> None:
    assert best_price([100, 120, 110]) == 120


def test_negative_odds_best_price_selection() -> None:
    assert best_price([-120, -105, -115]) == -105


def _game(markets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "event-1",
        "home_team": "Team A",
        "away_team": "Team B",
        "bookmakers": [
            {
                "key": "book-1",
                "title": "Book 1",
                "last_update": "2026-07-10T12:00:00Z",
                "markets": markets,
            }
        ],
    }


def test_totals_at_different_points_remain_separate() -> None:
    game = _game(
        [
            {
                "key": "totals",
                "outcomes": [
                    {"name": "Over", "point": point, "price": -110},
                    {"name": "Under", "point": point, "price": -110},
                ],
            }
            for point in (8.0, 8.5, 9.0)
        ]
    )

    lines = summarize_game(game)["markets"]["totals"]["lines"]

    assert set(lines) == {"8", "8.5", "9"}
    assert lines["8"]["outcomes"]["Over"]["point"] == 8.0
    assert lines["8.5"]["outcomes"]["Over"]["point"] == 8.5
    assert lines["9"]["outcomes"]["Over"]["point"] == 9.0


def test_spreads_at_different_points_remain_separate() -> None:
    game = _game(
        [
            {
                "key": "spreads",
                "outcomes": [
                    {"name": "Team A", "point": home_point, "price": -110},
                    {"name": "Team B", "point": -home_point, "price": -110},
                ],
            }
            for home_point in (-1.5, -2.5)
        ]
    )

    lines = summarize_game(game)["markets"]["spreads"]["lines"]

    assert set(lines) == {"-1.5", "-2.5"}
    assert lines["-1.5"]["outcomes"]["Team A"]["point"] == -1.5
    assert lines["-2.5"]["outcomes"]["Team A"]["point"] == -2.5


def test_incomplete_two_way_market_has_no_no_vig_or_hold() -> None:
    game = _game(
        [
            {
                "key": "totals",
                "outcomes": [{"name": "Over", "point": 8.5, "price": -110}],
            }
        ]
    )

    line = summarize_game(game)["markets"]["totals"]["lines"]["8.5"]

    assert line["complete_two_way_market"] is False
    assert line["market_hold"] is None
    assert line["outcomes"]["Over"]["no_vig_probability"] is None


def test_malformed_prices_are_preserved_but_excluded_from_calculations() -> None:
    game = _game(
        [
            {
                "key": "h2h",
                "outcomes": [
                    {"name": "Team A", "price": "not-a-price"},
                    {"name": "Team B"},
                ],
            }
        ]
    )

    line = summarize_game(game)["markets"]["h2h"]["lines"]["moneyline"]

    assert line["complete_two_way_market"] is False
    assert line["market_hold"] is None
    assert line["outcomes"]["Team A"]["best_price"] is None
    assert line["outcomes"]["Team A"]["offers"][0]["price"] == "not-a-price"
    assert line["outcomes"]["Team B"]["best_price"] is None


def test_cross_sign_median_price_does_not_create_invalid_american_odds() -> None:
    game = _game([{"key": "h2h", "outcomes": [{"name": "Team A", "price": -110}]}])
    game["bookmakers"].append(
        {
            "key": "book-2",
            "title": "Book 2",
            "markets": [{"key": "h2h", "outcomes": [{"name": "Team A", "price": 100}]}],
        }
    )

    outcome = summarize_game(game)["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["Team A"]

    assert outcome["median_price"] is None
    assert outcome["consensus_implied_probability"] is not None


def test_book_count_uses_distinct_bookmakers() -> None:
    game = _game(
        [
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": -110}]},
            {"key": "h2h", "outcomes": [{"name": "Team A", "price": -105}]},
        ]
    )

    outcome = summarize_game(game)["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]["Team A"]

    assert outcome["book_count"] == 1
    assert outcome["best_price"] == -105
