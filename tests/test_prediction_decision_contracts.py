from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.daily_slate.contracts import canonical_sha256
from app.predictions.production import (
    MoneylinePredictionV1,
    PredictionProviderPolicyV1,
    PredictionsContractError,
    PredictionsV1,
    ReviewedPredictionInputV1,
)
from app.rankings.production import RankingPolicyV1, build_rankings
from app.recommendation_gate.production import RecommendationPolicyV1, evaluate_recommendation_gate
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
    gate = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "clear"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(),
        evaluated_at=NOW,
    )
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


def test_gate_distinguishes_recommend_pass_and_avoid_without_changing_value() -> None:
    value = _value(_prediction())
    recommend = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "clear"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(),
        evaluated_at=NOW,
    )
    assert recommend.games[0].decision == "recommend"
    passed = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "clear"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(minimum_edge=0.5),
        evaluated_at=NOW,
    )
    assert passed.games[0].decision == "pass"
    avoided = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "degraded"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(),
        evaluated_at=NOW,
    )
    assert avoided.games[0].decision == "avoid"
    assert value.canonical_json_bytes() == _value(_prediction()).canonical_json_bytes()


def test_ranking_tie_breakers_are_deterministic_and_pass_rows_remain_unranked() -> None:
    value = _value(_prediction())
    gate = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "clear"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(),
        evaluated_at=NOW,
    )
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

    pass_gate = evaluate_recommendation_gate(
        value,
        quality_by_game={"777001": "clear"},
        scheduled_start_by_game={"777001": START},
        policy=RecommendationPolicyV1(minimum_edge=0.5),
        evaluated_at=NOW,
    )
    pass_rankings = build_rankings(
        pass_gate,
        value_metrics_by_game={"777001": metrics["777001"]},
        scheduled_start_by_game={"777001": START},
        policy=RankingPolicyV1(),
        ranked_at=NOW,
    )
    assert pass_rankings.entries[0].rank_eligible is False
    assert pass_rankings.entries[0].recommendation_rank is None


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
        quality_by_game={},
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
