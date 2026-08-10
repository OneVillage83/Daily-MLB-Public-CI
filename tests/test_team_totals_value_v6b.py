from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.team_totals import (
    TeamTotalsOddsEvidenceV1,
    normalize_team_totals_odds,
)
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    ModelRolloutState,
    PredictionDistributionKind,
    PredictionMarketFamily,
    PredictionPeriod,
    PredictionSubjectKind,
    PredictionTargetV1,
)
from app.predictions.team_total import TeamTotalPredictionV1
from app.value_engine.team_totals import (
    TEAM_TOTAL_BINDING_PENDING_REASON,
    TEAM_TOTAL_REFERENCE_REASON,
    TeamTotalsValueError,
    evaluate_team_totals_value,
)


def _prediction(
    *,
    team_id: str,
    opponent_team_id: str,
    probabilities: tuple[float, ...],
    tail: float,
    expected: float,
) -> TeamTotalPredictionV1:
    return TeamTotalPredictionV1(
        source_game_id="2026-08-10_NYY_BOS_1",
        away_team_id="NYY",
        home_team_id="BOS",
        team_id=team_id,
        opponent_team_id=opponent_team_id,
        target=PredictionTargetV1(
            source_game_id="2026-08-10_NYY_BOS_1",
            family=PredictionMarketFamily.TEAM_TOTAL,
            period=PredictionPeriod.FULL_GAME,
            subject_kind=PredictionSubjectKind.TEAM,
            subject_id=team_id,
            statistic="runs",
        ),
        expected_team_runs=expected,
        team_runs_distribution=DiscreteDistributionV1(
            kind=PredictionDistributionKind.TEAM_RUNS,
            outcomes=tuple(
                DiscreteOutcomeProbabilityV1(runs, probability)
                for runs, probability in enumerate(probabilities)
            ),
            unresolved_probability=tail,
        ),
        upstream_game_prediction_checksum="1" * 64,
        upstream_model_manifest_checksum="2" * 64,
        model_id="full-game-scoring",
        model_version="evaluation-v1",
        rollout_state=ModelRolloutState.REFERENCE,
        recommendation_eligible=False,
    )


def _predictions() -> tuple[TeamTotalPredictionV1, TeamTotalPredictionV1]:
    away = _prediction(
        team_id="NYY",
        opponent_team_id="BOS",
        probabilities=(0.05, 0.05, 0.05, 0.10, 0.10, 0.50),
        tail=0.15,
        expected=4.8,
    )
    home = _prediction(
        team_id="BOS",
        opponent_team_id="NYY",
        probabilities=(0.10, 0.15, 0.15, 0.20, 0.15, 0.15),
        tail=0.10,
        expected=4.2,
    )
    return away, home


def _event(*, away_team: str = "New York Yankees") -> dict[str, object]:
    return {
        "id": "event-1",
        "sport_key": "baseball_mlb",
        "commence_time": "2026-08-10T23:10:00Z",
        "away_team": away_team,
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
                            {"name": "Over", "description": away_team, "price": -105, "point": 4.5},
                            {"name": "Under", "description": away_team, "price": -115, "point": 4.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -110, "point": 4.5},
                            {"name": "Under", "description": "Boston Red Sox", "price": -110, "point": 4.5},
                        ],
                    }
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
                            {"name": "Over", "description": away_team, "price": -110, "point": 4.5},
                            {"name": "Under", "description": away_team, "price": -110, "point": 4.5},
                            {"name": "Over", "description": "Boston Red Sox", "price": -105, "point": 4.5},
                            {"name": "Under", "description": "Boston Red Sox", "price": -115, "point": 4.5},
                        ],
                    }
                ],
            },
        ],
    }


def _odds(*, away_team: str = "New York Yankees"):
    evidence = TeamTotalsOddsEvidenceV1(
        provider_event_id="event-1",
        retrieved_at=datetime(2026, 8, 10, 22, 2, tzinfo=timezone.utc),
        raw_capture_checksum="a" * 64,
        event=_event(away_team=away_team),
    )
    return normalize_team_totals_odds(evidence)


def test_v6b_evaluates_both_teams_and_both_sides() -> None:
    predictions = _predictions()
    odds = _odds()
    value = evaluate_team_totals_value(predictions, odds)

    assert value.source_game_id == "2026-08-10_NYY_BOS_1"
    assert value.provider_event_id == "event-1"
    assert value.source_prediction_checksums == tuple(item.checksum for item in predictions)
    assert value.source_normalized_odds_checksum == odds.checksum
    assert [(row.team_id, row.side) for row in value.outcomes] == [
        ("NYY", "over"),
        ("NYY", "under"),
        ("BOS", "over"),
        ("BOS", "under"),
    ]
    assert all(row.total_line == pytest.approx(4.5) for row in value.outcomes)


def test_v6b_preserves_best_book_no_vig_and_projection_lineage() -> None:
    predictions = _predictions()
    odds = _odds()
    value = evaluate_team_totals_value(predictions, odds)

    nyy_over = next(row for row in value.outcomes if row.team_id == "NYY" and row.side == "over")
    bos_over = next(row for row in value.outcomes if row.team_id == "BOS" and row.side == "over")

    assert nyy_over.american_price == -105.0
    assert nyy_over.best_price_books == ("draftkings",)
    assert nyy_over.no_vig_probability is not None
    assert nyy_over.source_prediction_checksum == predictions[0].checksum
    assert nyy_over.source_projection_checksum == predictions[0].project(4.5).checksum
    assert nyy_over.source_normalized_team_odds_checksum == odds.for_team("NYY").checksum
    assert nyy_over.source_normalized_odds_checksum == odds.checksum

    assert bos_over.american_price == -105.0
    assert bos_over.best_price_books == ("fanduel",)
    assert bos_over.source_prediction_checksum == predictions[1].checksum


def test_positive_ev_cannot_bypass_v6_reference_and_binding_blockers() -> None:
    value = evaluate_team_totals_value(_predictions(), _odds())
    nyy_over = next(row for row in value.outcomes if row.team_id == "NYY" and row.side == "over")

    assert nyy_over.expected_value_per_unit > 0.0
    assert nyy_over.recommendation_gate_input_eligible is False
    assert TEAM_TOTAL_REFERENCE_REASON in nyy_over.ineligibility_reasons
    assert TEAM_TOTAL_BINDING_PENDING_REASON in nyy_over.ineligibility_reasons
    assert "unresolved_tail_exceeds_value_tolerance" in nyy_over.ineligibility_reasons
    assert value.recommendation_gate_input_count == 0


def test_v6b_rejects_provider_event_team_mismatch() -> None:
    mismatched_odds = _odds(away_team="Los Angeles Dodgers")

    with pytest.raises(TeamTotalsValueError, match="disagree on team identity"):
        evaluate_team_totals_value(_predictions(), mismatched_odds)


def test_v6b_value_does_not_emit_recommendation_or_rank_fields() -> None:
    serialized = str(evaluate_team_totals_value(_predictions(), _odds()).as_dict())

    assert "recommendation_gate_input_eligible" in serialized
    assert "decision" not in serialized
    assert "recommendation_rank" not in serialized
