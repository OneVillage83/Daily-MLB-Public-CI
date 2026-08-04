from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from app.data_quality.contracts import DataQualityDisposition
from app.matchup_packet.assembly import assemble_matchup_packet
from app.model_feature_set.builder import (
    ModelFeatureSetBuildError,
    _bullpen_workload_metrics,
    _hitting_rates,
    _pitching_rates,
    _starter_history_metrics,
    _weighted_mean,
    build_model_feature_set,
)
from app.model_feature_set.contracts import (
    ModelFeatureGameV1,
    ModelFeatureSetContractError,
)
from app.model_feature_set.schema import (
    EXPECTED_MODEL_FEATURE_COUNT_V1,
    EXPECTED_MODEL_FEATURE_SCHEMA_CHECKSUM_V1,
    MODEL_FEATURE_NAMES_V1,
    MODEL_FEATURE_SCHEMA_CHECKSUM,
)
from tests.test_matchup_packet import _one_game_chain, _zero_game_chain


def _packet_from_chain(
    *,
    disposition: DataQualityDisposition = DataQualityDisposition.READY,
):
    slate, state, bia, odds_weather, quality = _one_game_chain(
        disposition=disposition
    )
    return assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=odds_weather,
        data_quality=quality,
    )


def test_schema_is_frozen_to_expected_width_and_checksum() -> None:
    assert len(MODEL_FEATURE_NAMES_V1) == EXPECTED_MODEL_FEATURE_COUNT_V1 == 613
    assert MODEL_FEATURE_SCHEMA_CHECKSUM == EXPECTED_MODEL_FEATURE_SCHEMA_CHECKSUM_V1
    assert len(MODEL_FEATURE_NAMES_V1) == len(set(MODEL_FEATURE_NAMES_V1))


def test_v1_predictive_schema_excludes_sportsbook_market_probabilities() -> None:
    forbidden = (
        "odds",
        "bookmaker",
        "implied_probability",
        "no_vig",
        "consensus_probability",
        "moneyline",
        "spread_price",
        "total_price",
    )
    for name in MODEL_FEATURE_NAMES_V1:
        assert not any(token in name for token in forbidden)


def test_zero_game_packet_builds_deterministic_zero_game_feature_set() -> None:
    slate, state, bia, odds_weather, quality = _zero_game_chain()
    packet = assemble_matchup_packet(
        slate=slate,
        game_state=state,
        baseball_intelligence=bia,
        odds_weather=odds_weather,
        data_quality=quality,
    )
    first = build_model_feature_set(packet)
    second = build_model_feature_set(packet)
    assert first.games == ()
    assert first.upstream_matchup_packet_checksum == packet.checksum
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()


def test_structural_features_and_missingness_are_explicit() -> None:
    packet = _packet_from_chain()
    feature_set = build_model_feature_set(packet)
    assert len(feature_set.games) == 1
    game = feature_set.games[0]
    features = game.feature_map()
    assert len(game.feature_values) == 613
    assert features["schedule.game_number"] == 1.0
    assert features["schedule.doubleheader.single"] == 1.0
    assert features["away.starter_certainty.unavailable"] == 1.0
    assert features["home.starter_certainty.unavailable"] == 1.0
    assert features["away.lineup_availability.unavailable"] == 1.0
    assert features["home.lineup_availability.unavailable"] == 1.0
    assert features["away.bullpen.workload.available_player_count"] == 0.0
    assert features["home.bullpen.workload.available_player_count"] == 0.0
    assert features["weather.status.unavailable"] == 1.0
    assert features["weather.temperature_f"] is None
    assert "weather.temperature_f" in game.missing_feature_names
    assert game.upstream_matchup_packet_game_checksum == packet.games[0].checksum


def test_insufficient_quality_is_metadata_not_game_filter() -> None:
    packet = _packet_from_chain(disposition=DataQualityDisposition.INSUFFICIENT)
    feature_set = build_model_feature_set(packet)
    assert len(feature_set.games) == 1
    game = feature_set.games[0]
    assert game.quality_disposition is DataQualityDisposition.INSUFFICIENT
    assert game.quality_issue_codes == tuple(
        sorted(issue.code for issue in packet.games[0].data_quality.issues)
    )


def test_model_feature_set_observed_at_cannot_precede_packet() -> None:
    packet = _packet_from_chain()
    with pytest.raises(ModelFeatureSetBuildError, match="cannot precede"):
        build_model_feature_set(
            packet,
            observed_at=packet.observed_at - timedelta(seconds=1),
        )


def test_empty_market_context_rejects_unrelated_reference_checksum() -> None:
    game = build_model_feature_set(_packet_from_chain()).games[0]
    assert not game.market_context
    assert game.market_reference_checksum is None
    with pytest.raises(ModelFeatureSetContractError, match="empty market context"):
        replace(game, market_reference_checksum="a" * 64)


def test_hitting_aggregation_recalculates_rates_from_summed_counts() -> None:
    rows = [
        {"pa": 4, "ab": 4, "h": 2, "2b": 1, "3b": 0, "hr": 0, "bb": 0, "hbp": 0, "so": 1, "sf": 0},
        {"pa": 5, "ab": 4, "h": 1, "2b": 0, "3b": 0, "hr": 1, "bb": 1, "hbp": 0, "so": 2, "sf": 0},
    ]
    result = _hitting_rates(rows)
    assert result["pa"] == 9.0
    assert result["avg"] == pytest.approx(3 / 8)
    assert result["obp"] == pytest.approx(4 / 9)
    assert result["slg"] == pytest.approx(7 / 8)
    assert result["ops"] == pytest.approx((4 / 9) + (7 / 8))
    assert result["k_rate"] == pytest.approx(3 / 9)
    assert result["bb_rate"] == pytest.approx(1 / 9)


def test_pitching_aggregation_recalculates_rates_from_summed_counts() -> None:
    rows = [
        {"bf": 12, "outs_recorded": 9, "h": 2, "er": 1, "hr": 0, "bb": 1, "hbp": 0, "so": 4, "pitches": 45},
        {"bf": 8, "outs_recorded": 6, "h": 1, "er": 0, "hr": 0, "bb": 1, "hbp": 0, "so": 2, "pitches": 30},
    ]
    result = _pitching_rates(rows)
    assert result["bf"] == 20.0
    assert result["era"] == pytest.approx(9 / 5)
    assert result["whip"] == pytest.approx(5 / 5)
    assert result["k_rate"] == pytest.approx(6 / 20)
    assert result["bb_rate"] == pytest.approx(2 / 20)
    assert result["k_minus_bb_rate"] == pytest.approx(4 / 20)


def test_weighted_mean_uses_sample_counts_and_ignores_missing_values() -> None:
    rows: list[Mapping[str, Any]] = [
        {"metric": 10.0, "samples": 2},
        {"metric": 20.0, "samples": 1},
        {"metric": None, "samples": 100},
    ]
    assert _weighted_mean(rows, "metric", "samples") == pytest.approx(40 / 3)


def test_starter_history_uses_previous_start_and_three_start_totals() -> None:
    payload: Mapping[str, Any] = {
        "pitcher_appearance_history_source": "statcast_final_game_appearances",
        "previous_start": {
            "pitches": 96,
            "batters_faced": 24,
            "hits": 5,
            "home_runs": 1,
            "walks": 2,
            "strikeouts": 7,
            "outs_recorded": 18,
        },
        "previous_three_starts": [
            {"pitches": 96, "batters_faced": 24, "hits": 5, "home_runs": 1, "walks": 2, "strikeouts": 7, "outs_recorded": 18},
            {"pitches": 88, "batters_faced": 22, "hits": 4, "home_runs": 0, "walks": 1, "strikeouts": 6, "outs_recorded": 18},
            {"pitches": 102, "batters_faced": 27, "hits": 7, "home_runs": 2, "walks": 3, "strikeouts": 8, "outs_recorded": 17},
        ],
    }
    result = _starter_history_metrics(payload)
    assert result["previous_start.pitches"] == 96.0
    assert result["previous_three_starts.start_count"] == 3.0
    assert result["previous_three_starts.pitches_sum"] == 286.0
    assert result["previous_three_starts.pitches_mean"] == pytest.approx(286 / 3)
    assert result["previous_three_starts.strikeouts_sum"] == 21.0


def test_unavailable_starter_history_stays_missing() -> None:
    assert _starter_history_metrics(
        {
            "pitcher_appearance_history_source": "unavailable",
            "previous_start": None,
            "previous_three_starts": [],
        }
    ) == {}


def test_bullpen_workload_aggregates_only_retained_history_sources() -> None:
    payloads: list[Mapping[str, Any]] = [
        {
            "pitcher_workload": {
                "source": "statcast_final_game_appearances",
                "days_since_previous_appearance": 1,
                "days_since_previous_start": 20,
                "recent": {
                    "previous_1_days": {"appearance_count": 1, "start_count": 0, "relief_appearance_count": 1, "pitch_count": 24},
                    "previous_3_days": {"appearance_count": 2, "start_count": 0, "relief_appearance_count": 2, "pitch_count": 39},
                    "previous_7_days": {"appearance_count": 3, "start_count": 0, "relief_appearance_count": 3, "pitch_count": 58},
                },
            }
        },
        {
            "pitcher_workload": {
                "source": "statcast_final_game_appearances",
                "days_since_previous_appearance": 2,
                "days_since_previous_start": None,
                "recent": {
                    "previous_1_days": {"appearance_count": 0, "start_count": 0, "relief_appearance_count": 0, "pitch_count": 0},
                    "previous_3_days": {"appearance_count": 1, "start_count": 0, "relief_appearance_count": 1, "pitch_count": 18},
                    "previous_7_days": {"appearance_count": 2, "start_count": 0, "relief_appearance_count": 2, "pitch_count": 34},
                },
            }
        },
        {"pitcher_workload": {"source": "unavailable", "recent": {}}},
    ]
    result = _bullpen_workload_metrics(payloads)
    assert result["available_player_count"] == 2.0
    assert result["days_since_previous_appearance_min"] == 1.0
    assert result["days_since_previous_appearance_mean"] == 1.5
    assert result["days_since_previous_start_min"] == 20.0
    assert result["previous_1_days.pitch_count_sum"] == 24.0
    assert result["previous_3_days.pitch_count_sum"] == 57.0
    assert result["previous_7_days.relief_appearance_count_sum"] == 5.0


def test_game_contract_rejects_wrong_vector_width() -> None:
    with pytest.raises(ModelFeatureSetContractError, match="length"):
        ModelFeatureGameV1(
            edge_event_id="edge:mlb:1",
            daily_mlb_game_id="game:mlb:1",
            source_game_id="1",
            away_team_id="SF",
            home_team_id="LAD",
            upstream_matchup_packet_game_checksum="a" * 64,
            quality_disposition=DataQualityDisposition.READY,
            quality_issue_codes=(),
            market_reference_checksum=None,
            feature_values=(1.0,),
        )


def test_game_contract_rejects_nonfinite_feature_value() -> None:
    values: list[float | None] = [None] * len(MODEL_FEATURE_NAMES_V1)
    values[0] = float("nan")
    with pytest.raises(ModelFeatureSetContractError, match="finite"):
        ModelFeatureGameV1(
            edge_event_id="edge:mlb:1",
            daily_mlb_game_id="game:mlb:1",
            source_game_id="1",
            away_team_id="SF",
            home_team_id="LAD",
            upstream_matchup_packet_game_checksum="a" * 64,
            quality_disposition=DataQualityDisposition.READY,
            quality_issue_codes=(),
            market_reference_checksum=None,
            feature_values=tuple(values),
        )
