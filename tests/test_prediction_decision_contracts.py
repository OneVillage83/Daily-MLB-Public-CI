from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.daily_slate.contracts import canonical_sha256
from app.data_quality.contracts import (
    DataQualityDisposition,
    DataQualityGameV1,
    QualityDomain,
    QualityIssueSeverity,
    QualityIssueV1,
)
from app.predictions.production import (
    MoneylinePredictionV1,
    PredictionProviderPolicyV1,
    PredictionsContractError,
    PredictionsV1,
    ReviewedPredictionInputV1,
)
from app.rankings.production import (
    RANKING_V1_COMPARATOR,
    RankingPolicyV1,
    RankingsProductionError,
    build_rankings,
)
from app.recommendation_gate.production import (
    RecommendationGateProductionError,
    RecommendationPolicyV1,
    evaluate_recommendation_gate,
)
from app.value_engine.production import ValuePolicyV1, build_value_engine


NOW = datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc)
START = NOW + timedelta(hours=3)
SHA = "a" * 64


def _prediction(*, probability: float = 0.60) -> PredictionsV1:
    policy = PredictionProviderPolicyV1()
    authored = ReviewedPredictionInputV1(
        run_id="run_20260803_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        source_game_id="777001",
        upstream_model_feature_set_snapshot_id="mfs:snapshot",
        upstream_model_feature_set_checksum=SHA,
        upstream_model_feature_game_checksum="b" * 64,
        predictive_feature_checksum="c" * 64,
        provider_policy=policy,
        home_probability=probability,
        home_lower=probability - 0.06,
        home_upper=probability + 0.06,
        generated_at=NOW - timedelta(minutes=2),
        sealed_at=NOW - timedelta(minutes=1),
        authoring_evidence={"method": "market-blind reviewed baseball evidence"},
        market_independence_attested=True,
    )
    game = MoneylinePredictionV1(
        source_game_id=authored.source_game_id,
        ordinal=1,
        away_team_id="ARI",
        home_team_id="ATL",
        scheduled_start_time=START,
        predictive_feature_checksum=authored.predictive_feature_checksum,
        upstream_model_feature_game_checksum=authored.upstream_model_feature_game_checksum,
        provider_policy=policy,
        home_probability=authored.home_probability,
        home_lower=authored.home_lower,
        home_upper=authored.home_upper,
        generated_at=authored.generated_at,
        sealed_at=authored.sealed_at,
        evidence_checksum=authored.evidence_checksum,
        market_independence_attested=authored.market_independence_attested,
    )
    return PredictionsV1(
        run_id=authored.run_id,
        requested_date="2026-08-03",
        as_of_time=NOW - timedelta(hours=1),
        observed_at=NOW,
        provider_policy=policy,
        upstream_model_feature_set_snapshot_id=authored.upstream_model_feature_set_snapshot_id,
        upstream_model_feature_set_checksum=authored.upstream_model_feature_set_checksum,
        upstream_data_quality_snapshot_id="dq:snapshot",
        upstream_data_quality_checksum="d" * 64,
        input_inventory_checksum=canonical_sha256([authored.as_dict()]),
        games=(game,),
    )


def _market_context() -> dict[str, Any]:
    def offer(price: float, book: str) -> dict[str, object]:
        return {
            "bookmaker_key": book,
            "effective_provider_timestamp": (NOW - timedelta(seconds=10)).isoformat(),
            "price": price,
            "provider_order": 1,
            "provider_retrieved_at": (NOW - timedelta(seconds=5)).isoformat(),
        }

    books = ("alpha", "bravo", "charlie", "delta")
    return {
        "markets": {
            "h2h": {
                "lines": {
                    "moneyline": {
                        "outcomes": {
                            "ATL": {"offers": [offer(110, book) for book in books]},
                            "ARI": {"offers": [offer(-110, book) for book in books]},
                        }
                    }
                }
            }
        }
    }


def _value(predictions: PredictionsV1):
    context = _market_context()
    return build_value_engine(
        predictions,
        {"777001": context},
        {"777001": canonical_sha256(context)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )


def _quality_game(*, degraded: bool = False) -> DataQualityGameV1:
    issues = (
        QualityIssueV1(
            "missing_weather",
            QualityDomain.WEATHER,
            QualityIssueSeverity.WARNING,
            "Applicable weather evidence is missing",
        ),
    ) if degraded else ()
    return DataQualityGameV1(
        edge_event_id="edge:777001",
        daily_mlb_game_id="daily:777001",
        source_game_id="777001",
        away_team_id="ARI",
        home_team_id="ATL",
        scheduled_start_time=START,
        upstream_daily_slate_game_checksum="1" * 64,
        upstream_game_state_game_checksum="2" * 64,
        upstream_baseball_intelligence_game_checksum="3" * 64,
        upstream_odds_weather_game_checksum="4" * 64,
        disposition=DataQualityDisposition.DEGRADED if degraded else DataQualityDisposition.READY,
        issues=issues,
    )


def _gate(value, *, predictions: PredictionsV1 | None = None, degraded: bool = False, policy=None):
    prediction_snapshot = _prediction() if predictions is None else predictions
    return evaluate_recommendation_gate(
        value,
        predictions_by_game={"777001": prediction_snapshot.games[0]},
        quality_games_by_id={"777001": _quality_game(degraded=degraded)},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1() if policy is None else policy,
        evaluated_at=NOW,
    )


def test_reviewed_prediction_is_complementary_uncalibrated_and_market_blind() -> None:
    predictions = _prediction()
    game = predictions.games[0]
    assert game.home_probability + game.away_probability == pytest.approx(1.0)
    assert game.away_lower == pytest.approx(1.0 - game.home_upper)
    assert game.away_upper == pytest.approx(1.0 - game.home_lower)
    assert game.provider_policy.calibration_state == "uncalibrated"
    assert game.as_dict()["market_independence_attestation"] is True
    with pytest.raises(PredictionsContractError, match="market-derived"):
        replace(
            ReviewedPredictionInputV1(
                run_id=predictions.run_id,
                source_game_id="777001",
                upstream_model_feature_set_snapshot_id="mfs:snapshot",
                upstream_model_feature_set_checksum=SHA,
                upstream_model_feature_game_checksum="b" * 64,
                predictive_feature_checksum="c" * 64,
                provider_policy=predictions.provider_policy,
                home_probability=0.6,
                home_lower=0.54,
                home_upper=0.66,
                generated_at=NOW - timedelta(minutes=2),
                sealed_at=NOW - timedelta(minutes=1),
                authoring_evidence={"method": "blind"},
                market_independence_attested=True,
            ),
            authoring_evidence={"best_price": 110},
        )


def test_value_engine_pairs_same_books_retains_both_sides_and_has_no_decision_fields() -> None:
    value = _value(_prediction())
    game = value.games[0]
    assert tuple(outcome.side for outcome in game.outcomes) == ("home", "away")
    assert all(len(outcome.eligible_pairs) == 4 for outcome in game.outcomes)
    assert all(
        pair.home_no_vig_probability + pair.away_no_vig_probability == pytest.approx(1.0)
        for pair in game.outcomes[0].eligible_pairs
    )
    payload = value.as_dict()
    assert not ({"recommendation", "decision", "rank", "pass", "avoid"} & set(payload))


def test_gate_recommends_at_most_one_side_and_rankings_are_contiguous() -> None:
    value = _value(_prediction())
    gate = _gate(value)
    game = gate.games[0]
    assert sum(side.decision == "recommend" for side in game.sides) <= 1
    assert game.decision in {"recommend", "pass", "avoid"}
    assert len({result.ordinal for side in game.sides for result in side.results}) == len(game.sides[0].results)
    rankings = build_rankings(
        gate,
        value_metrics_by_game={
            "777001": {
                "bookmaker_count": 4,
                "edge": value.games[0].outcomes[0].edge or 0.0,
                "ev": value.games[0].outcomes[0].expected_value_per_unit or 0.0,
                "interval_width": 0.12,
                "lower_bound_clearance": value.games[0].outcomes[0].lower_bound_clearance or 0.0,
            }
        },
        scheduled_start_by_game={"777001": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    eligible = [entry.recommendation_rank for entry in rankings.entries if entry.rank_eligible]
    assert eligible == list(range(1, len(eligible) + 1))
    assert rankings.entries[0].decision == game.decision


def test_value_excludes_cross_book_future_and_stale_pairs_and_selects_latest_revision() -> None:
    predictions = _prediction()
    base = _market_context()
    line = base["markets"]["h2h"]["lines"]["moneyline"]
    outcomes = line["outcomes"]
    home = outcomes["ATL"]
    away = outcomes["ARI"]
    home["offers"] = [home["offers"][0]]
    away["offers"] = [away["offers"][1]]
    cross_book = build_value_engine(
        predictions,
        {"777001": base},
        {"777001": canonical_sha256(base)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )
    assert all(outcome.availability == "unavailable" for outcome in cross_book.games[0].outcomes)

    for offset, reason in ((timedelta(minutes=5), "future"), (-timedelta(minutes=5), "stale")):
        context = _market_context()
        retained = context["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
        for side in ("ATL", "ARI"):
            for offer in retained[side]["offers"]:
                offer["provider_retrieved_at"] = (NOW + offset).isoformat()
                offer["effective_provider_timestamp"] = (NOW + offset).isoformat()
        value = build_value_engine(
            predictions,
            {"777001": context},
            {"777001": canonical_sha256(context)},
            evaluated_at=NOW,
            policy=ValuePolicyV1(),
            model_feature_set_snapshot_id="mfs:snapshot",
            model_feature_set_checksum=SHA,
            data_quality_snapshot_id="dq:snapshot",
            data_quality_checksum="d" * 64,
        )
        assert value.games[0].outcomes[0].exclusion_counts[reason] == 4
        assert value.games[0].outcomes[0].availability == "unavailable"


def test_value_selects_latest_compatible_same_book_combination_without_array_order_dependence() -> None:
    predictions = _prediction()

    def offer(price: float, seconds: int, order: int) -> dict[str, object]:
        timestamp = (NOW - timedelta(seconds=seconds)).isoformat()
        return {
            "bookmaker_key": "alpha",
            "effective_provider_timestamp": timestamp,
            "price": price,
            "provider_order": order,
            "provider_retrieved_at": timestamp,
        }

    old_home = offer(110, 20, 1)
    latest_home = offer(115, 1, 2)
    old_away = offer(-110, 18, 1)
    latest_away = offer(-105, 10, 2)
    context = _market_context()
    outcomes = context["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    outcomes["ATL"]["offers"] = [latest_home, old_home]
    outcomes["ARI"]["offers"] = [latest_away, old_away]

    first = build_value_engine(
        predictions,
        {"777001": context},
        {"777001": canonical_sha256(context)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )
    pair = first.games[0].outcomes[0].eligible_pairs[0]
    assert pair.home_price == 110
    assert pair.away_price == -110
    assert sum(first.games[0].outcomes[0].exclusion_counts.values()) == 0

    outcomes["ATL"]["offers"].reverse()
    outcomes["ARI"]["offers"].reverse()
    second = build_value_engine(
        predictions,
        {"777001": context},
        {"777001": canonical_sha256(context)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )
    assert second.games[0].outcomes[0].eligible_pairs[0].evidence_checksum == pair.evidence_checksum


def test_value_pair_selection_prefers_newest_compatible_pair_and_fully_breaks_timestamp_ties() -> None:
    predictions = _prediction()
    context = _market_context()
    outcomes = context["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]

    def offer(price: float, seconds: int, order: int) -> dict[str, object]:
        timestamp = (NOW - timedelta(seconds=seconds)).isoformat()
        return {
            "bookmaker_key": "alpha",
            "effective_provider_timestamp": timestamp,
            "price": price,
            "provider_order": order,
            "provider_retrieved_at": timestamp,
        }

    outcomes["ATL"]["offers"] = [offer(105, 20, 1), offer(115, 5, 2)]
    outcomes["ARI"]["offers"] = [offer(-105, 18, 1), offer(-115, 4, 2)]
    newest = build_value_engine(
        predictions,
        {"777001": context},
        {"777001": canonical_sha256(context)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    ).games[0].outcomes[0].eligible_pairs[0]
    assert (newest.home_price, newest.away_price) == (115, -115)

    tied = _market_context()
    tied_outcomes = tied["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    tied_outcomes["ATL"]["offers"] = [offer(108, 5, 2), offer(112, 5, 2)]
    tied_outcomes["ARI"]["offers"] = [offer(-108, 5, 2), offer(-112, 5, 2)]
    first = build_value_engine(
        predictions,
        {"777001": tied},
        {"777001": canonical_sha256(tied)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )
    tied_outcomes["ATL"]["offers"].reverse()
    tied_outcomes["ARI"]["offers"].reverse()
    second = build_value_engine(
        predictions,
        {"777001": tied},
        {"777001": canonical_sha256(tied)},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:snapshot",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:snapshot",
        data_quality_checksum="d" * 64,
    )
    assert (
        first.games[0].outcomes[0].eligible_pairs[0].evidence_checksum
        == second.games[0].outcomes[0].eligible_pairs[0].evidence_checksum
    )


def test_value_newer_bad_offer_does_not_hide_earlier_valid_pair() -> None:
    predictions = _prediction()
    for bad in (
        {"price": 50, "seconds": 1},
        {"price": 115, "seconds": 300},
        {"price": 115, "seconds": -300},
    ):
        context = _market_context()
        outcomes = context["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
        bad_timestamp = (NOW - timedelta(seconds=bad["seconds"])).isoformat()
        outcomes["ATL"]["offers"].append(
            {
                "bookmaker_key": "alpha",
                "effective_provider_timestamp": bad_timestamp,
                "price": bad["price"],
                "provider_order": 99,
                "provider_retrieved_at": bad_timestamp,
            }
        )
        result = build_value_engine(
            predictions,
            {"777001": context},
            {"777001": canonical_sha256(context)},
            evaluated_at=NOW,
            policy=ValuePolicyV1(),
            model_feature_set_snapshot_id="mfs:snapshot",
            model_feature_set_checksum=SHA,
            data_quality_snapshot_id="dq:snapshot",
            data_quality_checksum="d" * 64,
        )
        assert result.games[0].outcomes[0].availability == "available"
        assert len(result.games[0].outcomes[0].eligible_pairs) == 4


def test_gate_distinguishes_recommend_pass_and_avoid_without_changing_value() -> None:
    value = _value(_prediction())
    recommend = _gate(value)
    assert recommend.games[0].decision == "recommend"
    passed = _gate(value, policy=RecommendationPolicyV1(minimum_edge=0.5))
    assert passed.games[0].decision == "pass"
    avoided = _gate(value, degraded=True)
    assert avoided.games[0].decision == "avoid"
    avoided_side = avoided.games[0].sides[0]
    retained_quality_checksum = _quality_game(degraded=True).checksum
    weather_gate = next(result for result in avoided_side.results if result.code == "weather_evidence_acceptable")
    lineup_gate = next(result for result in avoided_side.results if result.code == "lineup_and_starter_risk")
    assert weather_gate.passed is False
    assert weather_gate.source_checksum == retained_quality_checksum
    assert lineup_gate.passed is True
    assert lineup_gate.source_checksum == retained_quality_checksum
    assert value.canonical_json_bytes() == _value(_prediction()).canonical_json_bytes()


def test_two_sided_gate_selection_rebuilds_losing_side_evidence() -> None:
    predictions = _prediction(probability=0.5)
    base = _value(predictions)
    home, away = base.games[0].outcomes
    home = replace(home, edge=0.10, expected_value_per_unit=0.20, lower_bound_clearance=0.05)
    away = replace(away, edge=0.10, expected_value_per_unit=0.15, lower_bound_clearance=0.05)
    value = replace(base, games=(replace(base.games[0], outcomes=(home, away)),))
    gate = _gate(value, predictions=predictions)
    selected, losing = gate.games[0].sides
    assert selected.decision == "recommend"
    assert losing.decision == "pass"
    selected_gate = next(result for result in selected.results if result.code == "opposing_side_not_selected")
    losing_gate = next(result for result in losing.results if result.code == "opposing_side_not_selected")
    assert selected_gate.passed is True
    assert losing_gate.passed is False
    assert losing.reason_codes == tuple(result.code for result in losing.results if not result.passed)

    with pytest.raises(RecommendationGateProductionError, match="pass requires"):
        replace(selected, decision="pass")
    with pytest.raises(RecommendationGateProductionError, match="recommended side"):
        replace(selected, results=tuple(replace(result, passed=False) if result.code == "minimum_ev" else result for result in selected.results), reason_codes=("minimum_ev",))
    with pytest.raises(RecommendationGateProductionError, match="avoid requires"):
        replace(losing, decision="avoid")


def test_ranking_tie_breakers_are_deterministic_and_pass_rows_remain_unranked() -> None:
    value = _value(_prediction())
    gate = _gate(value)
    first = gate.games[0]
    second = replace(first, ordinal=2, source_game_id="777002")
    two_game_gate = replace(gate, games=(first, second))
    metrics = {
        source_game_id: {
            "bookmaker_count": 4,
            "edge": 0.1,
            "ev": 0.2,
            "interval_width": 0.12,
            "lower_bound_clearance": 0.04,
        }
        for source_game_id in ("777001", "777002")
    }
    rankings = build_rankings(
        two_game_gate,
        value_metrics_by_game=metrics,
        scheduled_start_by_game={"777001": START, "777002": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert [entry.recommendation_rank for entry in rankings.entries] == [1, 2]
    assert (
        rankings.canonical_json_bytes()
        == build_rankings(
            two_game_gate,
            value_metrics_by_game=metrics,
            scheduled_start_by_game={"777001": START, "777002": START},
            policy=RankingPolicyV1(),
            ranked_at=NOW,
        ).canonical_json_bytes()
    )

    pass_gate = _gate(value, policy=RecommendationPolicyV1(minimum_edge=0.5))
    pass_rankings = build_rankings(
        pass_gate,
        value_metrics_by_game={"777001": metrics["777001"]},
        scheduled_start_by_game={"777001": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert pass_rankings.entries[0].rank_eligible is False
    assert pass_rankings.entries[0].recommendation_rank is None


def test_rankings_retain_slate_order_while_allowing_rank_inversion() -> None:
    value = _value(_prediction())
    first_gate = _gate(value).games[0]
    gates = tuple(replace(first_gate, ordinal=ordinal, source_game_id=f"77700{ordinal}") for ordinal in range(1, 4))
    gate = replace(_gate(value), games=gates)
    metrics = {
        "777001": {"bookmaker_count": 4, "edge": 0.1, "ev": 0.2, "interval_width": 0.12, "lower_bound_clearance": 0.04},
        "777002": {"bookmaker_count": 4, "edge": 0.1, "ev": 0.1, "interval_width": 0.12, "lower_bound_clearance": 0.04},
        "777003": {"bookmaker_count": 4, "edge": 0.1, "ev": 0.3, "interval_width": 0.12, "lower_bound_clearance": 0.04},
    }
    rankings = build_rankings(
        gate,
        value_metrics_by_game=metrics,
        scheduled_start_by_game={game.source_game_id: START for game in gates},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert [entry.recommendation_rank for entry in rankings.entries] == [2, 3, 1]

    with pytest.raises(RankingsProductionError, match="contiguous"):
        replace(rankings, entries=(replace(rankings.entries[0], recommendation_rank=1), *rankings.entries[1:]))
    with pytest.raises(RankingsProductionError, match="contiguous"):
        replace(rankings, entries=(rankings.entries[0], replace(rankings.entries[1], recommendation_rank=4), rankings.entries[2]))
    with pytest.raises(RankingsProductionError, match="positive integer"):
        replace(rankings.entries[0], recommendation_rank=True)
    with pytest.raises(RankingsProductionError, match="positive integer"):
        replace(rankings.entries[0], recommendation_rank=0)
    with pytest.raises(RankingsProductionError, match="identity mismatch"):
        replace(rankings.entries[0], recommendation_rank=None)

    noneligible = replace(rankings.entries[0], decision="pass", rank_eligible=False, recommendation_rank=None)
    with pytest.raises(RankingsProductionError, match="identity mismatch"):
        replace(noneligible, recommendation_rank=1)


def test_ranking_policy_is_exactly_frozen_and_metrics_are_required() -> None:
    assert RankingPolicyV1().comparator == RANKING_V1_COMPARATOR
    with pytest.raises(RankingsProductionError, match="frozen"):
        RankingPolicyV1(comparator=tuple(reversed(RANKING_V1_COMPARATOR)))
    with pytest.raises(RankingsProductionError, match="frozen"):
        RankingPolicyV1(comparator=RANKING_V1_COMPARATOR[:-1])
    value = _value(_prediction())
    gate = _gate(value)
    with pytest.raises(RankingsProductionError, match="metric ev"):
        build_rankings(
            gate,
            value_metrics_by_game={"777001": {"bookmaker_count": 4, "edge": 0.1, "interval_width": 0.12, "lower_bound_clearance": 0.04}},
            scheduled_start_by_game={"777001": START},
            policy=RankingPolicyV1(),
            ranked_at=NOW,
        )


@pytest.mark.parametrize(
    ("first_metrics", "second_metrics"),
    (
        ({"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}, {"ev": 0.2, "edge": 0.11, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}),
        ({"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}, {"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.05, "interval_width": 0.12, "bookmaker_count": 4}),
        ({"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.13, "bookmaker_count": 4}, {"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}),
        ({"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}, {"ev": 0.2, "edge": 0.10, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 5}),
    ),
)
def test_ranking_comparator_dimensions_control_behavior(first_metrics, second_metrics) -> None:
    value = _value(_prediction())
    first = _gate(value).games[0]
    second = replace(first, ordinal=2, source_game_id="777002")
    rankings = build_rankings(
        replace(_gate(value), games=(first, second)),
        value_metrics_by_game={"777001": first_metrics, "777002": second_metrics},
        scheduled_start_by_game={"777001": START, "777002": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert [entry.recommendation_rank for entry in rankings.entries] == [2, 1]


def test_ranking_uses_explicit_data_quality_order_not_alphabetical_order() -> None:
    value = _value(_prediction())
    base = _gate(value)
    first = base.games[0]
    second = replace(first, ordinal=2, source_game_id="777002", quality_disposition="degraded")
    metrics = {
        source_game_id: {"ev": 0.2, "edge": 0.1, "lower_bound_clearance": 0.04, "interval_width": 0.12, "bookmaker_count": 4}
        for source_game_id in ("777001", "777002")
    }
    rankings = build_rankings(
        replace(base, games=(first, second)),
        value_metrics_by_game=metrics,
        scheduled_start_by_game={"777001": START, "777002": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert [entry.recommendation_rank for entry in rankings.entries] == [1, 2]


def test_explicit_market_independence_attestation_and_alias_boundary() -> None:
    predictions = _prediction()
    authored = ReviewedPredictionInputV1(
        run_id=predictions.run_id,
        source_game_id="777001",
        upstream_model_feature_set_snapshot_id="mfs:snapshot",
        upstream_model_feature_set_checksum=SHA,
        upstream_model_feature_game_checksum="b" * 64,
        predictive_feature_checksum="c" * 64,
        provider_policy=predictions.provider_policy,
        home_probability=0.6,
        home_lower=0.54,
        home_upper=0.66,
        generated_at=NOW - timedelta(minutes=2),
        sealed_at=NOW - timedelta(minutes=1),
        authoring_evidence={"method": "blind"},
        market_independence_attested=True,
    )
    assert authored.as_dict()["market_independence_attested"] is True
    for invalid in (False, 0, 1):
        with pytest.raises(PredictionsContractError, match="explicitly attest"):
            replace(authored, market_independence_attested=invalid)
    for alias in ("odds", "sportsbook_price", "implied_prob", "no_vig", "market_context", "bookmaker_offer"):
        with pytest.raises(PredictionsContractError, match="market-derived"):
            replace(authored, authoring_evidence={alias: "forbidden"})


def test_zero_game_prediction_value_gate_and_ranking_chain_is_valid() -> None:
    predictions = PredictionsV1(
        run_id="run_20260803_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        requested_date="2026-08-03",
        as_of_time=NOW - timedelta(hours=1),
        observed_at=NOW,
        provider_policy=PredictionProviderPolicyV1(),
        upstream_model_feature_set_snapshot_id="mfs:empty",
        upstream_model_feature_set_checksum=SHA,
        upstream_data_quality_snapshot_id="dq:empty",
        upstream_data_quality_checksum="d" * 64,
        input_inventory_checksum=canonical_sha256([]),
        games=(),
    )
    value = build_value_engine(
        predictions,
        {},
        {},
        evaluated_at=NOW,
        policy=ValuePolicyV1(),
        model_feature_set_snapshot_id="mfs:empty",
        model_feature_set_checksum=SHA,
        data_quality_snapshot_id="dq:empty",
        data_quality_checksum="d" * 64,
    )
    gate = evaluate_recommendation_gate(
        value,
        predictions_by_game={},
        quality_games_by_id={},
        scheduled_start_by_game={},
        policy=RecommendationPolicyV1(),
        evaluated_at=NOW,
    )
    rankings = build_rankings(
        gate,
        value_metrics_by_game={},
        scheduled_start_by_game={},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert not predictions.games and not value.games and not gate.games and not rankings.entries
