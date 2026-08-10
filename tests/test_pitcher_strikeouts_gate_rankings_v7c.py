from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.odds_weather.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_MARKET,
    PitcherStrikeoutOddsEvidenceV1,
    normalize_pitcher_strikeout_odds,
)
from app.predictions.market_foundation import (
    DiscreteDistributionV1,
    DiscreteOutcomeProbabilityV1,
    PredictionDistributionKind,
)
from app.predictions.pitcher_strikeouts import (
    PitcherStarterBindingState,
    build_pitcher_strikeout_prediction,
)
from app.rankings.pitcher_strikeouts import (
    PitcherStrikeoutRankingEntryV1,
    PitcherStrikeoutRankingsError,
    build_pitcher_strikeout_reference_rankings,
)
from app.recommendation_gate.pitcher_strikeouts import (
    PITCHER_STRIKEOUT_POLICY_PENDING_REASON,
    PITCHER_STRIKEOUT_REFERENCE_GATE_REASON,
    PitcherStrikeoutGateError,
    PitcherStrikeoutGateOutcomeV1,
    evaluate_pitcher_strikeout_reference_gate,
)
from app.value_engine.pitcher_strikeouts import evaluate_pitcher_strikeout_value


def _prediction():
    distribution = DiscreteDistributionV1(
        kind=PredictionDistributionKind.PITCHER_STRIKEOUTS,
        outcomes=(
            DiscreteOutcomeProbabilityV1(3, 0.05),
            DiscreteOutcomeProbabilityV1(4, 0.05),
            DiscreteOutcomeProbabilityV1(5, 0.10),
            DiscreteOutcomeProbabilityV1(6, 0.30),
            DiscreteOutcomeProbabilityV1(7, 0.30),
            DiscreteOutcomeProbabilityV1(8, 0.20),
        ),
    )
    return build_pitcher_strikeout_prediction(
        source_game_id="statcast:12345",
        pitcher_id="mlbam:543210",
        pitcher_name="Example Starter",
        team_id="NYY",
        opponent_team_id="BOS",
        starter_binding_state=PitcherStarterBindingState.CONFIRMED,
        starter_binding_checksum="c" * 64,
        source_model_input_checksum="a" * 64,
        model_manifest_checksum="b" * 64,
        model_id="DSE_PITCHER_K_REFERENCE_V1",
        model_version="1.0.0",
        expected_strikeouts=6.35,
        strikeout_distribution=distribution,
    )


def _odds():
    event = {
        "id": "event-v7-gate",
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
                    }
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
                    }
                ],
            },
        ],
    }
    evidence = PitcherStrikeoutOddsEvidenceV1(
        provider_event_id="event-v7-gate",
        retrieved_at=datetime(2026, 8, 10, 1, 1, tzinfo=timezone.utc),
        raw_capture_checksum="d" * 64,
        event=event,
    )
    return normalize_pitcher_strikeout_odds(evidence)


def _chain():
    prediction = _prediction()
    odds = _odds()
    value = evaluate_pitcher_strikeout_value(prediction, odds)
    gate = evaluate_pitcher_strikeout_reference_gate(value)
    rankings = build_pitcher_strikeout_reference_rankings((gate,))
    return prediction, odds, value, gate, rankings


def test_positive_ev_v7_value_still_passes_and_remains_unranked() -> None:
    _, _, value, gate, rankings = _chain()
    positive = next(row for row in value.outcomes if row.side == "over")
    assert positive.expected_value_per_unit > 0.0
    assert len(gate.outcomes) == len(value.outcomes) == 2
    assert all(row.decision == "pass" for row in gate.outcomes)
    assert gate.recommendation_count == 0
    assert len(rankings.games[0].entries) == len(gate.outcomes)
    assert all(entry.rank_eligible is False for entry in rankings.games[0].entries)
    assert all(entry.recommendation_rank is None for entry in rankings.games[0].entries)
    assert rankings.recommendation_count == 0


def test_v7c_preserves_exact_value_gate_and_starter_lineage() -> None:
    prediction, odds, value, gate, rankings = _chain()
    assert gate.source_value_game_checksum == value.checksum
    assert gate.source_prediction_checksum == prediction.checksum
    assert gate.source_normalized_odds_checksum == odds.checksum
    by_value = {row.checksum: row for row in value.outcomes}
    for outcome in gate.outcomes:
        source = by_value[outcome.source_value_checksum]
        assert outcome.pitcher_id == source.pitcher_id
        assert outcome.starter_binding_state == source.starter_binding_state
        assert outcome.starter_binding_checksum == source.starter_binding_checksum
        assert PITCHER_STRIKEOUT_REFERENCE_GATE_REASON in outcome.reason_codes
        assert PITCHER_STRIKEOUT_POLICY_PENDING_REASON in outcome.reason_codes
    ranking_game = rankings.games[0]
    assert ranking_game.upstream_gate_game_checksum == gate.checksum
    assert ranking_game.source_prediction_checksum == prediction.checksum
    assert ranking_game.source_normalized_odds_checksum == odds.checksum
    by_gate = {row.checksum: row for row in gate.outcomes}
    for entry in ranking_game.entries:
        source = by_gate[entry.upstream_gate_outcome_checksum]
        assert entry.pitcher_id == source.pitcher_id
        assert entry.starter_binding_checksum == source.starter_binding_checksum
        assert entry.exclusion_reasons == source.reason_codes


def test_v7c_gate_and_rankings_do_not_copy_value_metrics_forward() -> None:
    _, _, value, gate, rankings = _chain()
    value_text = str(value.as_dict()).casefold()
    gate_text = str(gate.as_dict()).casefold()
    rankings_text = str(rankings.as_dict()).casefold()
    assert "expected_value_per_unit" in value_text
    assert "no_vig_probability" in value_text
    for forbidden in (
        "american_price",
        "best_price_books",
        "no_vig_probability",
        "raw_probability_edge",
        "expected_value_per_unit",
        "expected_roi_percent",
        "fair_american_odds",
    ):
        assert forbidden not in gate_text
        assert forbidden not in rankings_text


def test_v7c_contracts_reject_manual_recommendation_or_rank() -> None:
    _, _, _, gate, rankings = _chain()
    source = gate.outcomes[0]
    with pytest.raises(PitcherStrikeoutGateError, match="PASS"):
        PitcherStrikeoutGateOutcomeV1(
            source_game_id=source.source_game_id,
            provider_event_id=source.provider_event_id,
            pitcher_id=source.pitcher_id,
            pitcher_name=source.pitcher_name,
            provider_pitcher_key=source.provider_pitcher_key,
            team_id=source.team_id,
            opponent_team_id=source.opponent_team_id,
            starter_binding_state=source.starter_binding_state,
            starter_binding_checksum=source.starter_binding_checksum,
            market_key=source.market_key,
            side=source.side,
            line_key=source.line_key,
            line=source.line,
            decision="recommend",
            source_value_checksum=source.source_value_checksum,
            reason_codes=source.reason_codes,
        )
    entry = rankings.games[0].entries[0]
    with pytest.raises(PitcherStrikeoutRankingsError, match="cannot receive a rank"):
        PitcherStrikeoutRankingEntryV1(
            source_game_id=entry.source_game_id,
            provider_event_id=entry.provider_event_id,
            pitcher_id=entry.pitcher_id,
            pitcher_name=entry.pitcher_name,
            provider_pitcher_key=entry.provider_pitcher_key,
            team_id=entry.team_id,
            opponent_team_id=entry.opponent_team_id,
            starter_binding_state=entry.starter_binding_state,
            starter_binding_checksum=entry.starter_binding_checksum,
            market_key=entry.market_key,
            side=entry.side,
            line_key=entry.line_key,
            line=entry.line,
            decision=entry.decision,
            rank_eligible=True,
            recommendation_rank=1,
            upstream_gate_outcome_checksum=entry.upstream_gate_outcome_checksum,
            exclusion_reasons=entry.exclusion_reasons,
        )
