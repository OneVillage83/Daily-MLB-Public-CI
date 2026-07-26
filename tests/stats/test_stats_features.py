from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.stats.features import (
    BattingAggregateLine,
    BattingGameLine,
    PitchingAggregateLine,
    PitchingGameLine,
    StatcastBattedBall,
    StatcastPitchMetric,
    StatcastPlateAppearance,
    StatcastPitcherAppearance,
    StatcastPitcherPitch,
    StatcastSwingMetric,
    _pitch_physics_windows,
    _swing_metric_windows,
    build_aggregate_player_feature_payloads,
    build_aggregate_player_feature_payloads_v3,
    build_aggregate_team_feature_payloads,
    build_bullpen_workload,
    build_player_feature_payloads,
    derive_statcast_plate_appearances,
    derive_statcast_pitcher_appearances,
    hitter_rate_stats,
    pitcher_rate_stats,
)


AVAILABLE = datetime(2026, 7, 16, 23, 0, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)


def test_hitter_and_pitcher_rates_use_explicit_denominators() -> None:
    hitter = hitter_rate_stats(
        [
            BattingGameLine(
                date(2026, 7, 12),
                "p1",
                "MIN",
                10,
                8,
                3,
                1,
                0,
                1,
                1,
                0,
                2,
                1,
                available_at=AVAILABLE,
            )
        ]
    )
    assert hitter["1b"] == 1
    assert hitter["avg"] == pytest.approx(3 / 8)
    assert hitter["obp"] == pytest.approx(4 / 10)
    assert hitter["slg"] == pytest.approx(7 / 8)
    assert hitter["k_rate"] == pytest.approx(0.2)

    pitcher = pitcher_rate_stats(
        [
            PitchingGameLine(
                date(2026, 7, 12),
                "p2",
                "MIN",
                27,
                18,
                4,
                2,
                1,
                2,
                0,
                8,
                90,
                True,
                available_at=AVAILABLE,
            )
        ]
    )
    assert pitcher["innings_pitched"] == 6.0
    assert pitcher["era"] == 3.0
    assert pitcher["whip"] == 1.0
    assert pitcher["k_minus_bb_rate"] == pytest.approx(6 / 27)


def test_feature_as_of_excludes_same_day_and_future_games() -> None:
    lines = [
        BattingGameLine(
            date(2026, 7, 12), "p1", "MIN", pa=4, ab=4, hits=1,
            available_at=AVAILABLE,
        ),
        BattingGameLine(
            date(2026, 7, 16), "p1", "MIN", pa=4, ab=4, hits=4,
            available_at=AVAILABLE,
        ),
    ]
    before = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 16), knowledge_cutoff=CUTOFF,
        batting_lines=lines, pitching_lines=[],
    )
    after = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17), knowledge_cutoff=CUTOFF,
        batting_lines=lines, pitching_lines=[],
    )
    assert before["p1"]["hitting"]["season_to_date"]["h"] == 1
    assert after["p1"]["hitting"]["season_to_date"]["h"] == 5
    assert before["p1"]["feature_checksum"] != after["p1"]["feature_checksum"]


def test_v3_swing_metric_windows_enforce_pit_and_preserve_missing_values() -> None:
    late = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    rows = [
        StatcastSwingMetric(
            date(2026, 7, 16),
            "h1",
            bat_speed=70.0,
            swing_length=7.0,
            attack_angle=10.0,
            attack_direction=0.0,
            swing_path_tilt=20.0,
            miss_distance=0.0,
            hyper_speed=0.0,
            available_at=AVAILABLE,
        ),
        StatcastSwingMetric(
            date(2026, 7, 10),
            "h1",
            bat_speed=80.0,
            swing_length=None,
            attack_angle=20.0,
            attack_direction=10.0,
            swing_path_tilt=30.0,
            miss_distance=2.0,
            hyper_speed=100.0,
            available_at=AVAILABLE,
        ),
        StatcastSwingMetric(
            date(2026, 6, 20),
            "h1",
            bat_speed=None,
            swing_length=9.0,
            available_at=AVAILABLE,
        ),
        StatcastSwingMetric(
            date(2026, 7, 17),
            "h1",
            bat_speed=999.0,
            hyper_speed=999.0,
            available_at=AVAILABLE,
        ),
        StatcastSwingMetric(
            date(2026, 7, 15),
            "h1",
            bat_speed=888.0,
            hyper_speed=888.0,
            available_at=late,
        ),
    ]

    windows = _swing_metric_windows(
        rows,
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
    )

    season = windows["season_to_date"]
    assert season["swing_observation_count"] == 3
    assert windows["rolling_7_days"]["swing_observation_count"] == 2
    assert windows["rolling_14_days"]["swing_observation_count"] == 2
    assert windows["rolling_30_days"]["swing_observation_count"] == 3

    assert season["bat_speed_samples"] == 2
    assert season["bat_speed_mean"] == 75.0
    assert season["swing_length_samples"] == 2
    assert season["swing_length_mean"] == 8.0
    assert season["attack_direction_samples"] == 2
    assert season["attack_direction_mean"] == 5.0

    # Zero is observed evidence and must not be treated as missing.
    assert season["miss_distance_samples"] == 2
    assert season["miss_distance_mean"] == 1.0
    assert season["hyper_speed_samples"] == 2
    assert season["hyper_speed_mean"] == 50.0


def test_v3_pitch_physics_windows_use_circular_spin_axis_and_pit() -> None:
    late = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    rows = [
        StatcastPitchMetric(
            date(2026, 7, 16),
            "p1",
            "MIN",
            "FF",
            effective_speed=95.0,
            spin_axis=359,
            release_pos_x=-2.0,
            release_pos_y=54.0,
            release_pos_z=6.0,
            arm_angle=45.0,
            api_break_x_arm=1.0,
            api_break_x_batter_in=2.0,
            api_break_z_with_gravity=3.0,
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 10),
            "p1",
            "MIN",
            "FF",
            effective_speed=97.0,
            spin_axis=1,
            release_pos_x=-2.2,
            release_pos_y=54.2,
            release_pos_z=6.2,
            arm_angle=47.0,
            api_break_x_arm=3.0,
            api_break_x_batter_in=4.0,
            api_break_z_with_gravity=5.0,
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 6, 20),
            "p1",
            "MIN",
            "SL",
            effective_speed=None,
            spin_axis=None,
            release_pos_x=None,
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 17),
            "p1",
            "MIN",
            "FF",
            effective_speed=120.0,
            spin_axis=180,
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 15),
            "p1",
            "MIN",
            "FF",
            effective_speed=110.0,
            spin_axis=180,
            available_at=late,
        ),
    ]

    windows = _pitch_physics_windows(
        rows,
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
    )

    season = windows["season_to_date"]
    assert season["pitch_observation_count"] == 3
    assert windows["rolling_7_days"]["pitch_observation_count"] == 2
    assert windows["rolling_14_days"]["pitch_observation_count"] == 2
    assert windows["rolling_30_days"]["pitch_observation_count"] == 3

    assert season["effective_speed_samples"] == 2
    assert season["effective_speed_mean"] == 96.0
    assert season["release_pos_x_samples"] == 2
    assert season["release_pos_x_mean"] == pytest.approx(-2.1)
    assert season["arm_angle_samples"] == 2
    assert season["arm_angle_mean"] == 46.0

    # Circular data must wrap correctly: 359 degrees + 1 degree -> 0 degrees.
    assert windows["rolling_7_days"]["spin_axis_circular_mean"] == pytest.approx(
        0.0,
        abs=1e-9,
    )


def test_v3_sample_counts_exclude_nonfinite_values() -> None:
    swing_windows = _swing_metric_windows(
        [
            StatcastSwingMetric(
                date(2026, 7, 16),
                "h1",
                bat_speed=70.0,
                swing_length=7.0,
                attack_angle=10.0,
                attack_direction=2.0,
                swing_path_tilt=20.0,
                miss_distance=0.5,
                hyper_speed=90.0,
                available_at=AVAILABLE,
            ),
            StatcastSwingMetric(
                date(2026, 7, 16),
                "h1",
                bat_speed=float("nan"),
                swing_length=float("inf"),
                attack_angle=float("-inf"),
                attack_direction=float("nan"),
                swing_path_tilt=float("inf"),
                miss_distance=float("-inf"),
                hyper_speed=float("nan"),
                available_at=AVAILABLE,
            ),
        ],
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
    )
    swing = swing_windows["season_to_date"]

    # The second row is still an observed swing, but none of its
    # non-finite numeric values may count as usable metric samples.
    assert swing["swing_observation_count"] == 2
    assert swing["bat_speed_samples"] == 1
    assert swing["bat_speed_mean"] == 70.0
    assert swing["swing_length_samples"] == 1
    assert swing["swing_length_mean"] == 7.0
    assert swing["attack_angle_samples"] == 1
    assert swing["attack_angle_mean"] == 10.0
    assert swing["attack_direction_samples"] == 1
    assert swing["attack_direction_mean"] == 2.0
    assert swing["swing_path_tilt_samples"] == 1
    assert swing["swing_path_tilt_mean"] == 20.0
    assert swing["miss_distance_samples"] == 1
    assert swing["miss_distance_mean"] == 0.5
    assert swing["hyper_speed_samples"] == 1
    assert swing["hyper_speed_mean"] == 90.0

    pitch_windows = _pitch_physics_windows(
        [
            StatcastPitchMetric(
                date(2026, 7, 16),
                "p1",
                "MIN",
                "FF",
                effective_speed=96.0,
                spin_axis=45,
                release_pos_x=-2.0,
                release_pos_y=54.0,
                release_pos_z=6.0,
                arm_angle=46.0,
                api_break_x_arm=8.0,
                api_break_x_batter_in=-8.0,
                api_break_z_with_gravity=14.0,
                available_at=AVAILABLE,
            ),
            StatcastPitchMetric(
                date(2026, 7, 16),
                "p1",
                "MIN",
                "FF",
                effective_speed=float("nan"),
                spin_axis=None,
                release_pos_x=float("-inf"),
                release_pos_y=float("nan"),
                release_pos_z=float("inf"),
                arm_angle=float("-inf"),
                api_break_x_arm=float("nan"),
                api_break_x_batter_in=float("inf"),
                api_break_z_with_gravity=float("-inf"),
                available_at=AVAILABLE,
            ),
        ],
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
    )
    pitch = pitch_windows["season_to_date"]

    # The second pitch is an observation, but its non-finite physics
    # values must not inflate usable-sample counts.
    assert pitch["pitch_observation_count"] == 2
    assert pitch["effective_speed_samples"] == 1
    assert pitch["effective_speed_mean"] == 96.0
    assert pitch["spin_axis_samples"] == 1
    assert pitch["spin_axis_circular_mean"] == pytest.approx(45.0)
    assert pitch["release_pos_x_samples"] == 1
    assert pitch["release_pos_x_mean"] == -2.0
    assert pitch["release_pos_y_samples"] == 1
    assert pitch["release_pos_y_mean"] == 54.0
    assert pitch["release_pos_z_samples"] == 1
    assert pitch["release_pos_z_mean"] == 6.0
    assert pitch["arm_angle_samples"] == 1
    assert pitch["arm_angle_mean"] == 46.0
    assert pitch["api_break_x_arm_samples"] == 1
    assert pitch["api_break_x_arm_mean"] == 8.0
    assert pitch["api_break_x_batter_in_samples"] == 1
    assert pitch["api_break_x_batter_in_mean"] == -8.0
    assert pitch["api_break_z_with_gravity_samples"] == 1
    assert pitch["api_break_z_with_gravity_mean"] == 14.0


def test_statcast_features_and_zero_values_are_not_missing() -> None:
    result = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_lines=[],
        pitching_lines=[],
        batted_balls=[
            StatcastBattedBall(
                date(2026, 7, 16), "h1", 0.0, 0.0, 0, 0.0, 0.0, 0.0,
                available_at=AVAILABLE,
            )
        ],
        pitch_metrics=[
            StatcastPitchMetric(
                date(2026, 7, 16), "p1", "MIN", "FF", 95.0, 2200.0,
                6.0, 0.0, 0.0, 5, "called_strike", available_at=AVAILABLE,
            )
        ],
    )
    assert result["h1"]["batted_ball"]["exit_velocity"] == 0.0
    assert result["h1"]["batted_ball"]["xba"] == 0.0
    assert (
        result["h1"]["batted_ball"]["windows"]["rolling_7_days"][
            "exit_velocity"
        ]
        == 0.0
    )
    assert result["p1"]["pitch_traits"]["strike_rate"] == 1.0


def test_statcast_context_splits_are_deterministic_and_preserve_unknowns() -> None:
    batted_balls = [
        StatcastBattedBall(
            date(2026, 7, 16),
            "h1",
            100.0,
            20.0,
            6,
            0.7,
            1.2,
            0.8,
            is_home=True,
            batter_hand="L",
            opposing_pitcher_hand="R",
            available_at=AVAILABLE,
        ),
        StatcastBattedBall(
            date(2026, 7, 15),
            "h1",
            80.0,
            5.0,
            2,
            0.2,
            0.3,
            0.2,
            is_home=False,
            batter_hand="r",
            opposing_pitcher_hand="l",
            available_at=AVAILABLE,
        ),
        StatcastBattedBall(
            date(2026, 7, 14),
            "h1",
            60.0,
            None,
            available_at=AVAILABLE,
        ),
    ]
    pitch_metrics = [
        StatcastPitchMetric(
            date(2026, 7, 16),
            "p1",
            "MIN",
            "FF",
            96.0,
            opposing_batter_hand="L",
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 15),
            "p1",
            "MIN",
            "SL",
            85.0,
            opposing_batter_hand="R",
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 14),
            "p1",
            "MIN",
            "CH",
            87.0,
            opposing_batter_hand=None,
            available_at=AVAILABLE,
        ),
    ]

    result = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_lines=[],
        pitching_lines=[],
        batted_balls=batted_balls,
        pitch_metrics=pitch_metrics,
    )

    ball_splits = result["h1"]["batted_ball"]["splits"]
    assert ball_splits["home_away"]["home"]["batted_ball_count"] == 1
    assert ball_splits["home_away"]["away"]["exit_velocity"] == 80.0
    assert ball_splits["home_away"]["unknown"]["batted_ball_count"] == 1
    assert ball_splits["batter_handedness"]["left"]["exit_velocity"] == 100.0
    assert ball_splits["batter_handedness"]["right"]["exit_velocity"] == 80.0
    assert (
        ball_splits["opposing_pitcher_handedness"]["unknown"][
            "batted_ball_count"
        ]
        == 1
    )
    pitch_splits = result["p1"]["pitch_traits"]["splits"]
    handedness = pitch_splits["opposing_batter_handedness"]
    assert handedness["left"]["pitch_count"] == 1
    assert handedness["right"]["pitch_types"]["SL"]["velocity"] == 85.0
    assert handedness["unknown"]["pitch_types"]["CH"]["count"] == 1
    assert handedness["switch"]["pitch_count"] == 0


def test_statcast_splits_enforce_game_date_and_knowledge_cutoffs() -> None:
    late = datetime(2026, 7, 18, tzinfo=timezone.utc)
    rows = [
        StatcastBattedBall(
            date(2026, 7, 16),
            "h1",
            90.0,
            is_home=True,
            batter_hand="L",
            opposing_pitcher_hand="R",
            available_at=AVAILABLE,
        ),
        StatcastBattedBall(
            date(2026, 7, 17),
            "h1",
            110.0,
            is_home=True,
            batter_hand="L",
            opposing_pitcher_hand="R",
            available_at=AVAILABLE,
        ),
        StatcastBattedBall(
            date(2026, 7, 15),
            "h1",
            105.0,
            is_home=True,
            batter_hand="L",
            opposing_pitcher_hand="R",
            available_at=late,
        ),
    ]

    payload = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_lines=[],
        pitching_lines=[],
        batted_balls=rows,
    )["h1"]

    assert payload["batted_ball"]["batted_ball_count"] == 1
    assert (
        payload["batted_ball"]["splits"]["home_away"]["home"][
            "batted_ball_count"
        ]
        == 1
    )


def test_pitcher_workload_uses_only_known_prior_validated_appearances() -> None:
    def appearance(
        game_pk: int,
        game_date: date,
        *,
        is_start: bool,
        pitches: int,
        available_at: datetime = AVAILABLE,
    ) -> StatcastPitcherAppearance:
        return StatcastPitcherAppearance(
            game_pk=game_pk,
            game_date=game_date,
            pitcher_id="p1",
            team_key="MIN",
            is_start=is_start,
            pitches=pitches,
            batters_faced=4,
            hits=0,
            home_runs=0,
            walks=0,
            hit_by_pitch=0,
            strikeouts=1,
            outs_recorded=3,
            outs_recorded_status="known",
            available_at=available_at,
        )

    payload = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        pitcher_appearances=[
            appearance(1, date(2026, 7, 10), is_start=True, pitches=90),
            appearance(2, date(2026, 7, 15), is_start=False, pitches=20),
            appearance(3, date(2026, 7, 16), is_start=True, pitches=80),
            appearance(4, date(2026, 7, 17), is_start=False, pitches=15),
            appearance(
                5,
                date(2026, 7, 16),
                is_start=False,
                pitches=30,
                available_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
            ),
        ],
    )["p1"]

    workload = payload["pitcher_workload"]
    assert workload["source"] == "statcast_final_game_appearances"
    assert workload["previous_appearance"]["game_pk"] == 3
    assert workload["days_since_previous_appearance"] == 1
    assert workload["days_since_previous_start"] == 1
    assert workload["recent"]["previous_1_days"] == {
        "appearance_count": 1,
        "start_count": 1,
        "relief_appearance_count": 0,
        "pitch_count": 80,
    }
    assert workload["recent"]["previous_3_days"] == {
        "appearance_count": 2,
        "start_count": 1,
        "relief_appearance_count": 1,
        "pitch_count": 100,
    }
    assert payload["previous_start"]["game_pk"] == 3


def test_bullpen_workload_uses_only_prior_days() -> None:
    workload = build_bullpen_workload(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        pitching_lines=[
            PitchingGameLine(
                date(2026, 7, 16), "r1", "MIN", outs_recorded=3, pitches=15,
                available_at=AVAILABLE,
            ),
            PitchingGameLine(
                date(2026, 7, 17), "r2", "MIN", outs_recorded=3, pitches=12,
                available_at=AVAILABLE,
            ),
        ],
    )
    assert workload["MIN"]["bullpen_workload"]["previous_1_days"]["appearances"] == 1
    assert workload["MIN"]["bullpen_workload"]["previous_1_days"]["pitches"] == 15


def test_late_final_data_cannot_leak_past_a_historical_knowledge_cutoff() -> None:
    before_cutoff = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
    finalized_late = datetime(2026, 7, 18, 3, 0, tzinfo=timezone.utc)
    lines = [
        BattingGameLine(
            date(2026, 7, 12), "p1", "MIN", pa=4, ab=4, hits=1,
            available_at=AVAILABLE,
        ),
        BattingGameLine(
            date(2026, 7, 16), "p1", "MIN", pa=4, ab=4, hits=4,
            available_at=finalized_late,
        ),
    ]

    sealed = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=before_cutoff,
        batting_lines=lines,
        pitching_lines=[],
    )
    later_research = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=finalized_late,
        batting_lines=lines,
        pitching_lines=[],
    )

    assert sealed["p1"]["hitting"]["season_to_date"]["h"] == 1
    assert later_research["p1"]["hitting"]["season_to_date"]["h"] == 5
    assert sealed["p1"]["knowledge_cutoff"] == before_cutoff.isoformat()


def test_feature_knowledge_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="available_at must be timezone-aware"):
        BattingGameLine(
            date(2026, 7, 12), "p1", "MIN", available_at=datetime(2026, 7, 13)
        )
    with pytest.raises(ValueError, match="knowledge_cutoff must be timezone-aware"):
        build_player_feature_payloads(
            feature_as_of=date(2026, 7, 17),
            knowledge_cutoff=datetime(2026, 7, 17),
            batting_lines=[],
            pitching_lines=[],
        )


def test_v3_aggregate_player_payload_preserves_v2_and_adds_v3_sections() -> None:
    batting = [
        BattingAggregateLine(
            date(2026, 7, 16),
            "h1",
            "season_to_date",
            pa=100,
            ab=90,
            hits=30,
            home_runs=4,
            walks=8,
            strikeouts=20,
            available_at=AVAILABLE,
        )
    ]
    pitching = [
        PitchingAggregateLine(
            date(2026, 7, 16),
            "p1",
            "season_to_date",
            batters_faced=120,
            outs_recorded=81,
            hits=20,
            earned_runs=9,
            home_runs=3,
            walks=8,
            strikeouts=35,
            pitches=430,
            available_at=AVAILABLE,
        )
    ]
    pitch_metrics = [
        StatcastPitchMetric(
            date(2026, 7, 16),
            "p1",
            "MIN",
            "FF",
            effective_speed=95.5,
            spin_axis=359,
            available_at=AVAILABLE,
        ),
        StatcastPitchMetric(
            date(2026, 7, 15),
            "p1",
            "MIN",
            "FF",
            effective_speed=96.5,
            spin_axis=1,
            available_at=AVAILABLE,
        ),
    ]
    swing_metrics = [
        StatcastSwingMetric(
            date(2026, 7, 16),
            "h1",
            bat_speed=72.0,
            hyper_speed=90.0,
            available_at=AVAILABLE,
        ),
        StatcastSwingMetric(
            date(2026, 7, 15),
            "h1",
            bat_speed=76.0,
            hyper_speed=100.0,
            available_at=AVAILABLE,
        ),
    ]

    v2 = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=batting,
        pitching_aggregates=pitching,
        pitch_metrics=pitch_metrics,
    )
    v3 = build_aggregate_player_feature_payloads_v3(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=batting,
        pitching_aggregates=pitching,
        pitch_metrics=pitch_metrics,
        swing_metrics=swing_metrics,
    )

    assert set(v2) == {"h1", "p1"}
    assert set(v3) == {"h1", "p1"}

    assert v2["h1"]["contract_version"] == "DSE_MLB_STATS_FEATURES_V2"
    assert v2["p1"]["contract_version"] == "DSE_MLB_STATS_FEATURES_V2"
    assert v3["h1"]["contract_version"] == "DSE_MLB_STATS_FEATURES_V3"
    assert v3["p1"]["contract_version"] == "DSE_MLB_STATS_FEATURES_V3"

    excluded = {
        "contract_version",
        "feature_checksum",
        "swing_metrics",
        "pitch_physics",
    }

    for player_id in ("h1", "p1"):
        v2_common = {
            key: value
            for key, value in v2[player_id].items()
            if key not in excluded
        }
        v3_common = {
            key: value
            for key, value in v3[player_id].items()
            if key not in excluded
        }
        assert v3_common == v2_common

    assert "swing_metrics" not in v2["h1"]
    assert "pitch_physics" not in v2["p1"]

    hitter_swing = v3["h1"]["swing_metrics"]
    assert hitter_swing["swing_observation_count"] == 2
    assert hitter_swing["bat_speed_mean"] == 74.0
    assert hitter_swing["hyper_speed_mean"] == 95.0

    pitcher_physics = v3["p1"]["pitch_physics"]
    assert pitcher_physics["pitch_observation_count"] == 2
    assert pitcher_physics["effective_speed_mean"] == 96.0
    assert pitcher_physics["spin_axis_circular_mean"] == pytest.approx(
        0.0,
        abs=1e-9,
    )

    assert v3["h1"]["pitch_physics"]["pitch_observation_count"] == 0
    assert v3["h1"]["pitch_physics"]["effective_speed_mean"] is None

    assert v3["p1"]["swing_metrics"]["swing_observation_count"] == 0
    assert v3["p1"]["swing_metrics"]["bat_speed_mean"] is None

    assert v3["h1"]["feature_checksum"] != v2["h1"]["feature_checksum"]
    assert v3["p1"]["feature_checksum"] != v2["p1"]["feature_checksum"]


def test_v3_aggregate_player_payload_fails_closed_on_orphan_swing_evidence() -> None:
    with pytest.raises(
        ValueError,
        match="swing evidence references player absent",
    ):
        build_aggregate_player_feature_payloads_v3(
            feature_as_of=date(2026, 7, 17),
            knowledge_cutoff=CUTOFF,
            batting_aggregates=[],
            pitching_aggregates=[],
            swing_metrics=[
                StatcastSwingMetric(
                    date(2026, 7, 16),
                    "swing_only_player",
                    bat_speed=75.0,
                    available_at=AVAILABLE,
                )
            ],
        )


def test_v3_aggregate_player_payload_ignores_same_day_orphan_swing_evidence() -> None:
    result = build_aggregate_player_feature_payloads_v3(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        swing_metrics=[
            StatcastSwingMetric(
                date(2026, 7, 17),
                "swing_only_player",
                bat_speed=75.0,
                available_at=AVAILABLE,
            )
        ],
    )

    assert result == {}


def test_explicit_provider_aggregates_preserve_season_and_rolling_windows() -> None:
    result = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[
            BattingAggregateLine(
                date(2026, 7, 16),
                "p1",
                "season_to_date",
                pa=100,
                ab=90,
                hits=30,
                doubles=5,
                triples=1,
                home_runs=4,
                walks=8,
                hit_by_pitch=1,
                strikeouts=20,
                sacrifice_flies=1,
                available_at=AVAILABLE,
            ),
            BattingAggregateLine(
                date(2026, 7, 16),
                "p1",
                "rolling_7_days",
                pa=20,
                ab=18,
                hits=8,
                home_runs=2,
                walks=2,
                available_at=AVAILABLE,
            ),
        ],
        pitching_aggregates=[
            PitchingAggregateLine(
                date(2026, 7, 16),
                "p2",
                "season_to_date",
                batters_faced=120,
                outs_recorded=81,
                hits=20,
                earned_runs=9,
                home_runs=3,
                walks=8,
                strikeouts=35,
                pitches=430,
                available_at=AVAILABLE,
            )
        ],
    )

    assert result["p1"]["hitting"]["season_to_date"]["pa"] == 100
    assert result["p1"]["hitting"]["rolling_7_days"]["h"] == 8
    assert result["p1"]["hitting"]["rolling_14_days"]["pa"] is None
    assert (
        result["p1"]["hitting"]["rolling_14_days"]["data_status"]
        == "unavailable"
    )
    assert result["p2"]["pitching"]["season_to_date"]["era"] == 3.0


def test_provider_aggregate_cannot_cross_feature_as_of_boundary() -> None:
    with pytest.raises(ValueError, match="D-1 feature boundary"):
        build_aggregate_player_feature_payloads(
            feature_as_of=date(2026, 7, 16),
            knowledge_cutoff=CUTOFF,
            batting_aggregates=[
                BattingAggregateLine(
                    date(2026, 7, 16),
                    "p1",
                    "season_to_date",
                    pa=4,
                    available_at=AVAILABLE,
                )
            ],
            pitching_aggregates=[],
        )


def test_duplicate_provider_aggregate_window_fails_closed() -> None:
    row = BattingAggregateLine(
        date(2026, 7, 15),
        "p1",
        "season_to_date",
        pa=4,
        available_at=AVAILABLE,
    )
    with pytest.raises(ValueError, match="duplicate batting aggregate"):
        build_aggregate_player_feature_payloads(
            feature_as_of=date(2026, 7, 16),
            knowledge_cutoff=CUTOFF,
            batting_aggregates=[row, row],
            pitching_aggregates=[],
        )


def test_traded_player_total_is_selected_and_club_stints_build_team_totals() -> None:
    def row(team: str, stint: str, pa: int, hits: int) -> BattingAggregateLine:
        return BattingAggregateLine(
            date(2026, 7, 15),
            "p1",
            "season_to_date",
            pa=pa,
            ab=pa,
            hits=hits,
            source_team_id=team,
            source_stint_key=stint,
            team_key={"LAD": "LAD", "SF": "SF"}.get(team),
            available_at=AVAILABLE,
        )

    rows = [
        row("TOT", "TOT:1", 20, 7),
        row("LAD", "LAD:1", 12, 5),
        row("SF", "SF:1", 8, 2),
    ]
    players = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 16),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=rows,
        pitching_aggregates=[],
    )
    teams = build_aggregate_team_feature_payloads(
        feature_as_of=date(2026, 7, 16),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=rows,
        pitching_aggregates=[],
    )

    assert players["p1"]["hitting"]["season_to_date"]["pa"] == 20
    assert players["p1"]["hitting"]["season_to_date"]["source_team_id"] == "TOT"
    assert teams["LAD"]["hitting"]["season_to_date"]["pa"] == 12
    assert teams["SF"]["hitting"]["season_to_date"]["h"] == 2


def _statcast_pitch(
    *,
    game_pk: int = 1,
    game_date: date = date(2026, 7, 16),
    pitcher_id: str = "p1",
    team_key: str = "MIN",
    at_bat_number: int,
    pitch_number: int = 1,
    inning: int = 1,
    inning_half: str = "top",
    outs_when_up: int | None = 0,
    event: str | None = None,
    plate_appearance_classification: str | None = None,
    batter_id: str | None = "h1",
    batter_team_key: str | None = "LAD",
    batter_hand: str | None = "R",
    pitcher_hand: str | None = "L",
    game_is_final: bool = True,
    available_at: datetime = AVAILABLE,
) -> StatcastPitcherPitch:
    return StatcastPitcherPitch(
        game_pk=game_pk,
        game_date=game_date,
        pitcher_id=pitcher_id,
        team_key=team_key,
        at_bat_number=at_bat_number,
        pitch_number=pitch_number,
        inning=inning,
        inning_half=inning_half,
        outs_when_up=outs_when_up,
        event=event,
        plate_appearance_classification=plate_appearance_classification,
        batter_id=batter_id,
        batter_team_key=batter_team_key,
        batter_is_home=inning_half == "bottom",
        batter_hand=batter_hand,
        pitcher_hand=pitcher_hand,
        game_is_final=game_is_final,
        available_at=available_at,
    )


def test_statcast_appearances_use_factual_out_progression_and_events() -> None:
    appearances = derive_statcast_pitcher_appearances(
        [
            _statcast_pitch(at_bat_number=1, pitch_number=1),
            _statcast_pitch(
                at_bat_number=1,
                pitch_number=2,
                outs_when_up=0,
                event="field_out",
            ),
            _statcast_pitch(
                at_bat_number=2, outs_when_up=1, event="single"
            ),
            _statcast_pitch(
                at_bat_number=3,
                outs_when_up=1,
                event="grounded_into_double_play",
            ),
            _statcast_pitch(
                pitcher_id="p2",
                at_bat_number=4,
                inning=2,
                outs_when_up=2,
                event="field_out",
            ),
        ]
    )

    starter = next(row for row in appearances if row.pitcher_id == "p1")
    reliever = next(row for row in appearances if row.pitcher_id == "p2")
    assert starter.is_start is True
    assert starter.pitches == 4
    assert starter.batters_faced == 3
    assert starter.hits == 1
    assert starter.outs_recorded == 3
    assert starter.outs_recorded_status == "known"
    assert reliever.is_start is False
    assert reliever.outs_recorded == 1


def test_statcast_unknown_or_ambiguous_outs_remain_null() -> None:
    unknown = derive_statcast_pitcher_appearances(
        [
            _statcast_pitch(
                at_bat_number=1,
                outs_when_up=None,
                event="provider_event_not_in_contract",
            )
        ]
    )[0]
    assert unknown.outs_recorded is None
    assert unknown.outs_recorded_status == "unknown"

    mixed = derive_statcast_pitcher_appearances(
        [
            _statcast_pitch(at_bat_number=1, pitcher_id="p1"),
            _statcast_pitch(
                at_bat_number=1,
                pitch_number=2,
                pitcher_id="p2",
                event="field_out",
            ),
        ]
    )
    assert {row.outs_recorded for row in mixed} == {None}


def test_nonfinal_statcast_pitches_do_not_enter_appearances() -> None:
    assert (
        derive_statcast_pitcher_appearances(
            [
                _statcast_pitch(
                    at_bat_number=1,
                    event="field_out",
                    game_is_final=False,
                )
            ]
        )
        == ()
    )


def test_truncated_and_shift_violation_semantics_reach_feature_reducers() -> None:
    pitches = [
        _statcast_pitch(at_bat_number=1, event="truncated_pa"),
        _statcast_pitch(
            at_bat_number=2,
            event="field_error",
            plate_appearance_classification=(
                "defensive_shift_violation_non_at_bat"
            ),
        ),
    ]

    appearances = derive_statcast_pitcher_appearances(pitches)
    plate_appearances = derive_statcast_plate_appearances(pitches)
    payloads = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )

    assert appearances[0].batters_faced == 1
    assert len(plate_appearances) == 1
    split = payloads["h1"]["hitting"]["statcast_standard_splits"][
        "home_away"
    ]["away"]
    assert split["pa"] == 1
    assert split["ab"] == 0


def test_duplicate_statcast_pitch_identity_fails_closed() -> None:
    pitch = _statcast_pitch(at_bat_number=1, event="field_out")
    with pytest.raises(ValueError, match="duplicate Statcast pitch identity"):
        derive_statcast_pitcher_appearances([pitch, pitch])


def test_standard_statcast_splits_use_factual_terminal_events_and_outs() -> None:
    plate_appearances = derive_statcast_plate_appearances(
        [
            _statcast_pitch(
                at_bat_number=1,
                outs_when_up=0,
                event="single",
                batter_hand="R",
                pitcher_hand="L",
            ),
            _statcast_pitch(
                at_bat_number=2,
                outs_when_up=0,
                event="strikeout",
                batter_hand="R",
                pitcher_hand="L",
            ),
            _statcast_pitch(
                at_bat_number=3,
                outs_when_up=1,
                event="walk",
                batter_hand="R",
                pitcher_hand="L",
            ),
        ]
    )
    payloads = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )

    hitter = payloads["h1"]["hitting"]["statcast_standard_splits"]
    away = hitter["home_away"]["away"]
    assert away["pa"] == 3
    assert away["avg"] == pytest.approx(0.5)
    assert away["obp"] == pytest.approx(2 / 3)
    assert away["slg"] == pytest.approx(0.5)
    assert away["ops"] == pytest.approx(7 / 6)
    assert away["iso"] == 0.0
    assert away["babip"] == 1.0
    assert away["k_rate"] == pytest.approx(1 / 3)
    assert away["bb_rate"] == pytest.approx(1 / 3)
    assert hitter["opposing_pitcher_handedness"]["left"]["pa"] == 3

    pitcher = payloads["p1"]["pitching"]["statcast_standard_splits"]
    home = pitcher["home_away"]["home"]
    assert home["bf"] == 3
    assert home["outs_recorded"] == 1
    assert home["whip"] == 6.0
    assert home["k_rate"] == pytest.approx(1 / 3)
    assert home["bb_rate"] == pytest.approx(1 / 3)
    assert home["k_minus_bb_rate"] == 0.0
    assert home["k_per_9"] == 27.0
    assert home["bb_per_9"] == 27.0
    assert home["hr_per_9"] == 0.0
    assert home["era"] is None
    assert "era" in home["unavailable_fields"]
    assert pitcher["opposing_batter_handedness"]["right"]["bf"] == 3


def test_standard_statcast_splits_fail_closed_on_unsupported_events() -> None:
    plate_appearances = derive_statcast_plate_appearances(
        [
            _statcast_pitch(
                at_bat_number=1,
                event="provider_event_not_in_contract",
            )
        ]
    )
    payloads = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )

    hitting = payloads["h1"]["hitting"]["statcast_standard_splits"]
    away = hitting["home_away"]["away"]
    assert away["data_status"] == "partial"
    assert away["avg"] is None
    assert away["unsupported_events"] == ["provider_event_not_in_contract"]
    pitching = payloads["p1"]["pitching"]["statcast_standard_splits"]
    assert pitching["home_away"]["home"]["whip"] is None


def test_statcast_mid_plate_appearance_batter_change_is_retained_but_not_attributed() -> None:
    """A valid provider pinch-hitter change must not fail the entire feature run."""

    plate_appearances = derive_statcast_plate_appearances(
        [
            _statcast_pitch(
                at_bat_number=46,
                pitch_number=1,
                batter_id="h1",
                event=None,
            ),
            _statcast_pitch(
                at_bat_number=46,
                pitch_number=2,
                batter_id="h2",
                event="single",
            ),
        ]
    )

    assert len(plate_appearances) == 1
    assert plate_appearances[0].batter_id == "h2"
    assert plate_appearances[0].event == "single"
    assert plate_appearances[0].attribution_status == "ambiguous_batter_change"

    payloads = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )
    hitter = payloads["h2"]["hitting"]["statcast_standard_splits"]["home_away"]["away"]
    assert hitter["data_status"] == "partial"
    assert hitter["avg"] is None


def test_standard_pitcher_split_does_not_assign_non_pa_out_to_batter_bucket() -> None:
    plate_appearances = derive_statcast_plate_appearances(
        [
            _statcast_pitch(
                at_bat_number=1,
                outs_when_up=0,
                event="single",
            ),
            _statcast_pitch(
                at_bat_number=2,
                outs_when_up=1,
                event="field_out",
            ),
        ]
    )
    assert plate_appearances[0].outs_recorded_status == "unknown"

    payload = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )["p1"]
    home = payload["pitching"]["statcast_standard_splits"]["home_away"]["home"]
    assert home["whip"] is None
    assert home["k_per_9"] is None
    assert "whip" in home["unavailable_fields"]


def test_standard_statcast_splits_enforce_date_and_knowledge_boundaries() -> None:
    late = datetime(2026, 7, 18, tzinfo=timezone.utc)
    plate_appearances = (
        StatcastPlateAppearance(
            game_pk=1,
            game_date=date(2026, 7, 15),
            batter_id="h1",
            pitcher_id="p1",
            batter_team_key="LAD",
            pitcher_team_key="MIN",
            batter_is_home=False,
            batter_hand="R",
            pitcher_hand="L",
            event="single",
            outs_recorded=0,
            outs_recorded_status="known",
            attribution_status="supported",
            available_at=AVAILABLE,
        ),
        StatcastPlateAppearance(
            game_pk=2,
            game_date=date(2026, 7, 16),
            batter_id="h1",
            pitcher_id="p1",
            batter_team_key="LAD",
            pitcher_team_key="MIN",
            batter_is_home=False,
            batter_hand="R",
            pitcher_hand="L",
            event="home_run",
            outs_recorded=0,
            outs_recorded_status="known",
            attribution_status="supported",
            available_at=late,
        ),
        StatcastPlateAppearance(
            game_pk=3,
            game_date=date(2026, 7, 17),
            batter_id="h1",
            pitcher_id="p1",
            batter_team_key="LAD",
            pitcher_team_key="MIN",
            batter_is_home=False,
            batter_hand="R",
            pitcher_hand="L",
            event="double",
            outs_recorded=0,
            outs_recorded_status="known",
            attribution_status="supported",
            available_at=AVAILABLE,
        ),
    )
    payload = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        statcast_plate_appearances=plate_appearances,
    )["h1"]

    away = payload["hitting"]["statcast_standard_splits"]["home_away"]["away"]
    assert away["observation_count"] == 1
    assert away["h"] == 1
    assert away["hr"] == 0


def test_statcast_previous_starts_use_only_known_completed_prior_dates() -> None:
    appearances = [
        StatcastPitcherAppearance(
            game_pk=game_pk,
            game_date=game_date,
            pitcher_id="p1",
            team_key="MIN",
            is_start=True,
            pitches=90,
            batters_faced=24,
            hits=4,
            home_runs=0,
            walks=2,
            hit_by_pitch=0,
            strikeouts=7,
            outs_recorded=18,
            outs_recorded_status="known",
            available_at=available_at,
        )
        for game_pk, game_date, available_at in (
            (1, date(2026, 6, 28), AVAILABLE),
            (2, date(2026, 7, 4), AVAILABLE),
            (3, date(2026, 7, 10), AVAILABLE),
            (4, date(2026, 7, 16), AVAILABLE),
            (5, date(2026, 7, 17), AVAILABLE),
            (6, date(2026, 7, 15), datetime(2026, 7, 18, tzinfo=timezone.utc)),
        )
    ]
    result = build_aggregate_player_feature_payloads(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        batting_aggregates=[],
        pitching_aggregates=[],
        pitcher_appearances=appearances,
    )["p1"]

    assert result["previous_start"]["game_pk"] == 4
    assert [row["game_pk"] for row in result["previous_three_starts"]] == [2, 3, 4]
    assert result["pitcher_appearance_history_source"] == (
        "statcast_final_game_appearances"
    )
    assert result["pitching"]["season_to_date"]["bf"] is None


def test_statcast_bullpen_workload_has_point_in_time_windows_and_unknown_outs() -> None:
    def appearance(
        game_pk: int,
        game_date: date,
        pitcher_id: str,
        outs: int | None,
    ) -> StatcastPitcherAppearance:
        return StatcastPitcherAppearance(
            game_pk=game_pk,
            game_date=game_date,
            pitcher_id=pitcher_id,
            team_key="MIN",
            is_start=False,
            pitches=15,
            batters_faced=4,
            hits=1,
            home_runs=0,
            walks=0,
            hit_by_pitch=0,
            strikeouts=1,
            outs_recorded=outs,
            outs_recorded_status="known" if outs is not None else "unknown",
            available_at=AVAILABLE,
        )

    result = build_bullpen_workload(
        feature_as_of=date(2026, 7, 17),
        knowledge_cutoff=CUTOFF,
        pitcher_appearances=[
            appearance(1, date(2026, 7, 16), "r1", 3),
            appearance(2, date(2026, 7, 15), "r2", None),
            appearance(3, date(2026, 7, 11), "r3", 2),
            appearance(4, date(2026, 7, 17), "r4", 3),
        ],
    )["MIN"]

    one_day = result["bullpen_workload"]["previous_1_days"]
    three_days = result["bullpen_workload"]["previous_3_days"]
    seven_days = result["bullpen_workload"]["previous_7_days"]
    assert one_day["appearances"] == 1
    assert one_day["outs_recorded"] == 3
    assert three_days["appearances"] == 2
    assert three_days["outs_recorded"] is None
    assert three_days["outs_recorded_known"] == 3
    assert three_days["outs_recorded_unknown_appearances"] == 1
    assert seven_days["appearances"] == 3
    assert result["bullpen_workload_source"] == (
        "statcast_final_game_appearances"
    )


def test_later_completed_game_is_usable_despite_an_earlier_watermark_gap() -> None:
    """The feature builder consumes validated games, not a contiguous watermark."""

    result = build_player_feature_payloads(
        feature_as_of=date(2026, 7, 18),
        knowledge_cutoff=datetime(2026, 7, 18, 12, tzinfo=timezone.utc),
        batting_lines=[
            BattingGameLine(
                date(2026, 7, 16),
                "p1",
                "MIN",
                pa=4,
                ab=4,
                hits=2,
                available_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
            )
        ],
        pitching_lines=[],
    )
    assert result["p1"]["hitting"]["season_to_date"]["h"] == 2
