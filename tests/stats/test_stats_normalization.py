from __future__ import annotations

import pytest

from app.stats.identities import (
    TEAM_SOURCE_ALIASES,
    require_active_team_identity,
    resolve_baseball_reference_aggregate_team,
    resolve_team_identity,
)
from app.stats.normalization import (
    DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
    STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT,
    GameStatus,
    normalize_baseball_reference_schedule_row,
    normalize_statcast_rows,
    optional_int,
)
from app.team_aliases import CANONICAL_TEAM_KEYS


def test_every_stats_provider_maps_all_active_teams() -> None:
    for aliases in TEAM_SOURCE_ALIASES.values():
        assert set(aliases.values()) == set(CANONICAL_TEAM_KEYS)


def test_unknown_team_identity_never_fuzzy_matches() -> None:
    assert resolve_team_identity("statcast", "New York Yankees").canonical_team_key is None
    with pytest.raises(ValueError, match="Unknown statcast"):
        require_active_team_identity("statcast", "New York Yankees")


@pytest.mark.parametrize(
    ("team_name", "level", "expected"),
    [
        ("Arizona", "Maj-NL", "ARI"),
        ("Chicago", "Maj-AL", "CWS"),
        ("Chicago", "Maj-NL", "CHC"),
        ("Los Angeles", "Maj-AL", "LAA"),
        ("Los Angeles", "Maj-NL", "LAD"),
        ("New York", "Maj-AL", "NYY"),
        ("New York", "Maj-NL", "NYM"),
    ],
)
def test_baseball_reference_daily_city_requires_exact_league_context(
    team_name: str,
    level: str,
    expected: str,
) -> None:
    identity = resolve_baseball_reference_aggregate_team(team_name, level)
    assert identity.canonical_team_key == expected
    assert identity.is_multi_team is False
    assert identity.source_scope_id == f"{team_name}|{level}"


def test_baseball_reference_multi_team_total_has_no_invented_team_identity() -> None:
    identity = resolve_baseball_reference_aggregate_team(
        "Chicago,Los Angeles",
        "Maj-AL,Maj-NL",
    )
    assert identity.canonical_team_key is None
    assert identity.is_multi_team is True
    assert identity.source_scope_id == "Chicago,Los Angeles|Maj-AL,Maj-NL"


@pytest.mark.parametrize(
    ("team_name", "level"),
    [
        ("Chicago", ""),
        ("Chicago", "Maj-AL,Maj-NL"),
        ("Unknown City", "Maj-AL"),
        ("Chicago,Unknown City", "Maj-AL"),
    ],
)
def test_baseball_reference_daily_unknown_or_ambiguous_scope_fails_closed(
    team_name: str,
    level: str,
) -> None:
    with pytest.raises(ValueError, match="Baseball-Reference"):
        resolve_baseball_reference_aggregate_team(team_name, level)


def test_july_16_schedule_is_incomplete_until_result_and_scores_exist() -> None:
    scheduled = normalize_baseball_reference_schedule_row(
        {"date_game": "2026-07-16", "opp_ID": "CHW", "homeORvis": "", "W/L": "", "R": "", "RA": ""},
        season=2026,
        subject_team_source_id="MIN",
        row_index=1,
    )
    assert scheduled.status is GameStatus.SCHEDULED

    final = normalize_baseball_reference_schedule_row(
        {"date_game": "2026-07-16", "opp_ID": "CHW", "homeORvis": "", "W/L": "W", "R": "4", "RA": "2"},
        season=2026,
        subject_team_source_id="MIN",
        row_index=1,
    )
    assert final.status is GameStatus.FINAL
    assert final.home_score == 4
    assert final.away_score == 2


@pytest.mark.parametrize(
    ("status_text", "expected"),
    [
        ("Postponed; rescheduled", GameStatus.POSTPONED_RESCHEDULED),
        ("Game suspended", GameStatus.SUSPENDED_PENDING),
        ("Cancelled - no game", GameStatus.CANCELLED_NO_GAME),
        ("Game in progress", GameStatus.IN_PROGRESS),
    ],
)
def test_schedule_preserves_nonfinal_statuses(status_text: str, expected: GameStatus) -> None:
    game = normalize_baseball_reference_schedule_row(
        {"date_game": "2026-07-16", "opp_ID": "CHW", "status_text": status_text},
        season=2026,
        subject_team_source_id="MIN",
        row_index=1,
    )
    assert game.status is expected


def _pitch(**overrides: str) -> dict[str, str]:
    row = {
        "game_type": "R",
        "game_pk": "777",
        "game_date": "2026-07-16",
        "at_bat_number": "1",
        "pitch_number": "1",
        "batter": "101",
        "pitcher": "202",
        "home_team": "MIN",
        "away_team": "CWS",
        "release_speed": "95.0",
    }
    row.update(overrides)
    return row


def test_statcast_exact_pitch_identity_detects_duplicates_and_conflicts() -> None:
    result = normalize_statcast_rows(
        [_pitch(), _pitch(), _pitch(release_speed="96.0"), _pitch(game_type="A")]
    )
    assert len(result.pitches) == 1
    assert result.duplicate_count == 1
    assert result.excluded_count == 1
    assert len(result.conflicts) == 1
    assert result.pitches[0].identity == (777, 1, 1)



def test_statcast_retains_model_ready_v3_metrics_without_future_leakage() -> None:
    pitch = normalize_statcast_rows(
        [
            _pitch(
                effective_speed="94.2",
                spin_axis="214",
                release_pos_x="-1.72",
                release_pos_y="54.31",
                release_pos_z="5.88",
                vx0="6.10",
                vy0="-138.2",
                vz0="-4.2",
                ax="-11.3",
                ay="29.1",
                az="-15.8",
                api_break_x_arm="1.2",
                api_break_x_batter_in="-1.2",
                api_break_z_with_gravity="15.7",
                arm_angle="42.0",
                bat_speed="73.4",
                swing_length="7.2",
                attack_angle="11.0",
                attack_direction="2.3",
                swing_path_tilt="34.5",
                miss_distance="0.8",
                hit_distance_sc="392",
                hc_x="124.5",
                hc_y="81.2",
                hit_location="8",
                on_1b="303",
                on_2b="",
                on_3b="",
                bat_score="2",
                fld_score="1",
                post_bat_score="2",
                post_fld_score="1",
                n_thruorder_pitcher="2",
                n_priorpa_thisgame_player_at_bat="1",
                pitcher_days_since_prev_game="5",
                batter_days_since_prev_game="1",
                if_fielding_alignment="Standard",
                of_fielding_alignment="Standard",
                fielder_2="9002",
                umpire="Example Umpire",
                sv_id="example-sv-id",
                pitcher_days_until_next_game="5",
                batter_days_until_next_game="1",
                break_angle_deprecated="99",
                spin_rate_deprecated="9999",
                player_name="Raw Display Name",
            )
        ]
    ).pitches[0]

    retained = {
        "effective_speed",
        "spin_axis",
        "release_pos_x",
        "release_pos_y",
        "release_pos_z",
        "vx0",
        "vy0",
        "vz0",
        "ax",
        "ay",
        "az",
        "api_break_x_arm",
        "api_break_x_batter_in",
        "api_break_z_with_gravity",
        "arm_angle",
        "bat_speed",
        "swing_length",
        "attack_angle",
        "attack_direction",
        "swing_path_tilt",
        "miss_distance",
        "hit_distance_sc",
        "hc_x",
        "hc_y",
        "hit_location",
        "on_1b",
        "on_2b",
        "on_3b",
        "bat_score",
        "fld_score",
        "post_bat_score",
        "post_fld_score",
        "n_thruorder_pitcher",
        "n_priorpa_thisgame_player_at_bat",
        "pitcher_days_since_prev_game",
        "batter_days_since_prev_game",
        "if_fielding_alignment",
        "of_fielding_alignment",
        "fielder_2",
        "umpire",
        "sv_id",
    }

    assert retained <= pitch.metrics.keys()

    # These fields encode future information relative to a historical
    # prediction point and must remain raw-only.
    assert "pitcher_days_until_next_game" not in pitch.metrics
    assert "batter_days_until_next_game" not in pitch.metrics

    # Deprecated / display-only fields remain raw-only.
    assert "break_angle_deprecated" not in pitch.metrics
    assert "spin_rate_deprecated" not in pitch.metrics
    assert "player_name" not in pitch.metrics

    # Existing V2 fields must remain intact.
    assert pitch.metrics["release_speed"] == "95.0"


def test_spin_axis_materialization_boundary_requires_finite_integral_value() -> None:
    spin_axis = optional_int("214")

    assert spin_axis == 214
    assert isinstance(spin_axis, int)
    assert optional_int(float("inf")) is None
    assert optional_int("214.5") is None


def test_statcast_partial_missing_identity_fails_closed() -> None:
    with pytest.raises(ValueError, match="identity"):
        normalize_statcast_rows([_pitch(pitch_number="")])


def test_statcast_normalizes_only_proven_defensive_shift_violation_error() -> None:
    ordinary = normalize_statcast_rows(
        [_pitch(events="field_error", des="Throwing error by the third baseman.")]
    ).pitches[0]
    shift = normalize_statcast_rows(
        [
            _pitch(
                events="field_error",
                des=(
                    "Call overturned: batter reaches on a   DEFENSIVE shift "
                    "violation error by the second baseman."
                ),
            )
        ]
    ).pitches[0]

    assert "_dse_plate_appearance_classification" not in ordinary.metrics
    assert shift.metrics["_dse_plate_appearance_classification"] == {
        "contract_version": STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT,
        "classification": DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
        "source_field": "des",
    }
    assert "des" not in shift.metrics
    assert ordinary.checksum_sha256 != shift.checksum_sha256


@pytest.mark.parametrize(
    ("event", "description"),
    [
        ("field_error", "Batter reaches on an error."),
        ("field_error", "Batter reaches on a pitch clock violation."),
        ("field_error", "Defensive shift violation overturned the call."),
        ("walk", "Batter reaches on a defensive shift violation error."),
    ],
)
def test_statcast_shift_violation_classification_is_narrow(
    event: str, description: str
) -> None:
    pitch = normalize_statcast_rows(
        [_pitch(events=event, des=description)]
    ).pitches[0]

    assert "_dse_plate_appearance_classification" not in pitch.metrics


def test_nonsemantic_statcast_description_does_not_churn_pitch_checksum() -> None:
    without_description = normalize_statcast_rows(
        [_pitch(events="field_error")]
    ).pitches[0]
    with_description = normalize_statcast_rows(
        [_pitch(events="field_error", des="Ordinary throwing error.")]
    ).pitches[0]

    assert without_description.metrics == with_description.metrics
    assert without_description.checksum_sha256 == with_description.checksum_sha256
