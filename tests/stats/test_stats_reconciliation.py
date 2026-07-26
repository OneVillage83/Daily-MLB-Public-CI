from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.stats.features import BattingAggregateLine, PitchingAggregateLine
from app.stats.reconciliation import (
    derive_statcast_counting_stats,
    reconcile_baseball_reference_to_statcast,
    reconcile_counting_stats,
    reconcile_schedule_pair,
    validate_schema_columns,
)
from app.stats.normalization import NormalizedPitch
from app.stats.normalization import (
    DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
    STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT,
)


def _pitch(
    *,
    at_bat: int,
    number: int,
    batter: int,
    pitcher: int,
    event: str | None,
    classification: str | None = None,
) -> NormalizedPitch:
    metrics: dict[str, object] = {"events": event}
    if classification is not None:
        metrics["_dse_plate_appearance_classification"] = {
            "contract_version": STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT,
            "classification": classification,
            "source_field": "des",
        }
    return NormalizedPitch(
        game_pk=1,
        game_date=date(2026, 7, 12),
        at_bat_number=at_bat,
        pitch_number=number,
        batter_id=batter,
        pitcher_id=pitcher,
        home_team_key="LAD",
        away_team_key="SF",
        metrics=metrics,
        checksum_sha256=f"{at_bat:032x}{number:032x}",
    )


def test_schema_drift_is_a_failure() -> None:
    result = validate_schema_columns(
        dataset="statcast",
        observed=["game_pk", "game_date", "game_pk"],
        required={"game_pk", "game_date", "pitch_number"},
    )
    assert result.status == "failed"
    assert result.failure_count == 2


def test_noncritical_count_difference_is_preserved_as_warning() -> None:
    result = reconcile_counting_stats(
        entity_key="player:1",
        source={"H": 10, "PA": 40},
        derived={"H": 9, "PA": 40},
    )
    assert result.status == "completed_with_warnings"
    assert result.warning_count == 1


def test_critical_count_difference_fails() -> None:
    result = reconcile_counting_stats(
        entity_key="game-count",
        source={"games": 10},
        derived={"games": 9},
        critical_fields=frozenset({"games"}),
    )
    assert result.status == "failed"


def test_explained_count_difference_remains_visible_without_warning() -> None:
    result = reconcile_counting_stats(
        entity_key="player:1",
        source={"PA": 10},
        derived={"PA": 9},
        explanations={"PA": "provider scoring correction not present in raw pitch feed"},
    )

    assert result.status == "passed"
    assert result.items[0].code == "counting_stat_mismatch_explained"
    assert result.items[0].explanation is not None


def test_statcast_counting_stats_use_terminal_plate_appearance_event() -> None:
    pitches = [
        _pitch(at_bat=1, number=1, batter=10, pitcher=20, event=None),
        _pitch(at_bat=1, number=2, batter=10, pitcher=20, event="home_run"),
        _pitch(at_bat=2, number=1, batter=11, pitcher=20, event="walk"),
        _pitch(at_bat=3, number=1, batter=10, pitcher=20, event="strikeout"),
        _pitch(at_bat=4, number=1, batter=10, pitcher=20, event="sac_fly"),
    ]

    batting, pitching = derive_statcast_counting_stats(pitches)

    assert batting[10] == {"PA": 3, "AB": 2, "H": 1, "HR": 1, "BB": 0, "SO": 1}
    assert batting[11] == {"PA": 1, "AB": 0, "H": 0, "HR": 0, "BB": 1, "SO": 0}
    assert pitching[20] == {"BF": 4, "H": 1, "HR": 1, "BB": 1, "SO": 1}


def test_truncated_plate_appearance_contributes_no_counting_stats() -> None:
    pitch = _pitch(
        at_bat=1,
        number=1,
        batter=10,
        pitcher=20,
        event="truncated_pa",
    )

    batting, pitching = derive_statcast_counting_stats([pitch])

    assert batting == {}
    assert pitching == {}
    assert pitch.metrics["events"] == "truncated_pa"


def test_ordinary_field_error_counts_as_at_bat() -> None:
    batting, pitching = derive_statcast_counting_stats(
        [_pitch(at_bat=1, number=1, batter=10, pitcher=20, event="field_error")]
    )

    assert batting[10] == {"PA": 1, "AB": 1, "H": 0, "HR": 0, "BB": 0, "SO": 0}
    assert pitching[20] == {"BF": 1, "H": 0, "HR": 0, "BB": 0, "SO": 0}


def test_defensive_shift_violation_error_is_not_an_at_bat() -> None:
    batting, pitching = derive_statcast_counting_stats(
        [
            _pitch(
                at_bat=1,
                number=1,
                batter=10,
                pitcher=20,
                event="field_error",
                classification=DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
            )
        ]
    )

    assert batting[10] == {"PA": 1, "AB": 0, "H": 0, "HR": 0, "BB": 0, "SO": 0}
    assert pitching[20] == {"BF": 1, "H": 0, "HR": 0, "BB": 0, "SO": 0}


def _batting_aggregate(
    *,
    player_id: str = "mlb-player:mlbam:10",
    team: str = "LAD",
    stint: str = "LAD:1",
    pa: int = 1,
    ab: int = 1,
    hits: int = 1,
    home_runs: int = 1,
    walks: int = 0,
    strikeouts: int = 0,
) -> BattingAggregateLine:
    return BattingAggregateLine(
        date(2026, 7, 12),
        player_id,
        "season_to_date",
        pa=pa,
        ab=ab,
        hits=hits,
        home_runs=home_runs,
        walks=walks,
        strikeouts=strikeouts,
        source_team_id=team,
        source_stint_key=stint,
        team_key=None if team == "TOT" else team,
        available_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )


def _pitching_aggregate(
    *,
    player_id: str = "mlb-player:mlbam:20",
    batters_faced: int = 1,
    hits: int = 1,
    home_runs: int = 1,
    walks: int = 0,
    strikeouts: int = 0,
) -> PitchingAggregateLine:
    return PitchingAggregateLine(
        date(2026, 7, 12),
        player_id,
        "season_to_date",
        batters_faced=batters_faced,
        hits=hits,
        home_runs=home_runs,
        walks=walks,
        strikeouts=strikeouts,
        source_team_id="LAD",
        source_stint_key="LAD:1",
        team_key="LAD",
        available_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )


def test_same_cutoff_baseball_reference_and_statcast_counts_match() -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[_batting_aggregate()],
        pitching_aggregates=[_pitching_aggregate()],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event="home_run")
        ],
    )

    assert result.status == "passed"
    assert result.entities_compared == 2
    assert result.fields_compared == 11
    assert result.fields_matched == 11
    assert result.explained_difference_count == 0
    assert result.unexplained_difference_count == 0


def test_pitcher_transition_difference_is_explicitly_explained() -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[
            _batting_aggregate(pa=1, ab=0, hits=0, home_runs=0, walks=1)
        ],
        pitching_aggregates=[
            _pitching_aggregate(
                player_id="mlb-player:mlbam:20",
                batters_faced=1,
                hits=0,
                home_runs=0,
                walks=1,
            ),
            _pitching_aggregate(
                player_id="mlb-player:mlbam:21",
                batters_faced=0,
                hits=0,
                home_runs=0,
                walks=0,
            ),
        ],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event=None),
            _pitch(at_bat=1, number=2, batter=10, pitcher=21, event="walk"),
        ],
    )

    assert result.status == "passed"
    assert result.explained_difference_count == 4
    assert result.unexplained_difference_count == 0
    assert {item.field for item in result.items} == {"BF", "BB"}
    assert all(item.explanation is not None for item in result.items)


def test_independent_pitcher_transitions_allow_mixed_official_assignments() -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[
            _batting_aggregate(
                player_id="mlb-player:mlbam:10",
                pa=1,
                ab=1,
                hits=0,
                home_runs=0,
            ),
            _batting_aggregate(
                player_id="mlb-player:mlbam:11",
                pa=1,
                ab=0,
                hits=0,
                home_runs=0,
                walks=1,
            ),
        ],
        pitching_aggregates=[
            _pitching_aggregate(
                player_id="mlb-player:mlbam:20",
                batters_faced=0,
                hits=0,
                home_runs=0,
                walks=0,
            ),
            _pitching_aggregate(
                player_id="mlb-player:mlbam:21",
                batters_faced=1,
                hits=0,
                home_runs=0,
                walks=0,
            ),
            _pitching_aggregate(
                player_id="mlb-player:mlbam:22",
                batters_faced=1,
                hits=0,
                home_runs=0,
                walks=1,
            ),
        ],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event=None),
            _pitch(at_bat=1, number=2, batter=10, pitcher=21, event="field_out"),
            _pitch(at_bat=2, number=1, batter=11, pitcher=22, event=None),
            _pitch(at_bat=2, number=2, batter=11, pitcher=20, event="walk"),
        ],
    )

    assert result.status == "passed"
    assert result.explained_difference_count == 4
    assert result.unexplained_difference_count == 0
    assert all(item.explanation is not None for item in result.items)


@pytest.mark.parametrize("field", ["BF", "BB"])
def test_pitcher_mismatch_without_transition_remains_unexplained(field: str) -> None:
    aggregate = _pitching_aggregate(
        batters_faced=2 if field == "BF" else 1,
        hits=0,
        home_runs=0,
        walks=2 if field == "BB" else 1,
    )
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[
            _batting_aggregate(pa=1, ab=0, hits=0, home_runs=0, walks=1)
        ],
        pitching_aggregates=[aggregate],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event="walk")
        ],
    )

    assert result.unexplained_difference_count == 1
    assert result.explained_difference_count == 0
    assert result.items[0].field == field


@pytest.mark.parametrize("field", ["H", "HR", "SO"])
def test_pitcher_transition_never_blanket_explains_other_fields(field: str) -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[_batting_aggregate()],
        pitching_aggregates=[
            _pitching_aggregate(
                hits=2 if field == "H" else 1,
                home_runs=2 if field == "HR" else 0,
                strikeouts=2 if field == "SO" else 0,
            )
        ],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=21, event=None),
            _pitch(at_bat=1, number=2, batter=10, pitcher=20, event="home_run"),
        ],
    )

    assert any(
        item.field == field and item.explanation is None for item in result.items
    )


def test_pitcher_return_to_opening_role_is_not_transition_explanation() -> None:
    """Multiple pitcher ids alone do not prove terminal attribution drift."""
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[
            _batting_aggregate(pa=1, ab=0, hits=0, home_runs=0, walks=1)
        ],
        pitching_aggregates=[
            _pitching_aggregate(
                player_id="mlb-player:mlbam:20",
                batters_faced=2,
                hits=0,
                home_runs=0,
                walks=2,
            )
        ],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event=None),
            _pitch(at_bat=1, number=2, batter=10, pitcher=21, event=None),
            _pitch(at_bat=1, number=3, batter=10, pitcher=20, event="walk"),
        ],
    )

    assert result.explained_difference_count == 0
    assert any(
        item.entity_key == "pitcher:mlb-player:mlbam:20"
        and item.field in {"BF", "BB"}
        and item.explanation is None
        for item in result.items
    )


def test_mixed_plate_appearance_semantics_reconcile_deterministically() -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[
            _batting_aggregate(player_id="mlb-player:mlbam:10", pa=1, ab=1, hits=0, home_runs=0),
            _batting_aggregate(player_id="mlb-player:mlbam:11", pa=1, ab=0, hits=0, home_runs=0),
            _batting_aggregate(player_id="mlb-player:mlbam:13", pa=1, ab=0, hits=0, home_runs=0, walks=1),
            _batting_aggregate(player_id="mlb-player:mlbam:14", pa=1, ab=1, hits=0, home_runs=0),
        ],
        pitching_aggregates=[
            _pitching_aggregate(batters_faced=2, hits=0, home_runs=0),
            _pitching_aggregate(player_id="mlb-player:mlbam:30", batters_faced=1, hits=0, home_runs=0, walks=1),
            _pitching_aggregate(player_id="mlb-player:mlbam:31", batters_faced=0, hits=0, home_runs=0),
            _pitching_aggregate(player_id="mlb-player:mlbam:40", batters_faced=2, hits=0, home_runs=0),
        ],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=12, pitcher=22, event="truncated_pa"),
            _pitch(at_bat=2, number=1, batter=10, pitcher=20, event="field_error"),
            _pitch(
                at_bat=3,
                number=1,
                batter=11,
                pitcher=20,
                event="field_error",
                classification=DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
            ),
            _pitch(at_bat=4, number=1, batter=13, pitcher=30, event=None),
            _pitch(at_bat=4, number=2, batter=13, pitcher=31, event="walk"),
            _pitch(at_bat=5, number=1, batter=14, pitcher=40, event="field_out"),
        ],
    )

    assert result.fields_compared == 44
    assert result.fields_matched == 39
    assert result.explained_difference_count == 4
    assert result.unexplained_difference_count == 1
    assert [
        (item.entity_key, item.field)
        for item in result.items
        if item.explanation is None
    ] == [("pitcher:mlb-player:mlbam:40", "BF")]


def test_unexplained_cross_source_difference_remains_a_warning() -> None:
    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[_batting_aggregate(hits=0)],
        pitching_aggregates=[_pitching_aggregate()],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event="home_run")
        ],
    )

    assert result.status == "completed_with_warnings"
    assert result.explained_difference_count == 0
    assert result.unexplained_difference_count == 1
    assert result.items[0].entity_key == "batter:mlb-player:mlbam:10"
    assert result.items[0].field == "H"


def test_cross_source_reconciliation_enforces_cutoff_and_selects_total_line() -> None:
    total = _batting_aggregate(team="TOT", stint="TOT:1")
    club = _batting_aggregate(team="LAD", stint="LAD:1")
    later = NormalizedPitch(
        game_pk=2,
        game_date=date(2026, 7, 13),
        at_bat_number=1,
        pitch_number=1,
        batter_id=10,
        pitcher_id=20,
        home_team_key="LAD",
        away_team_key="SF",
        metrics={"events": "home_run"},
        checksum_sha256="f" * 64,
    )

    result = reconcile_baseball_reference_to_statcast(
        through_date=date(2026, 7, 12),
        batting_aggregates=[club, total],
        pitching_aggregates=[_pitching_aggregate()],
        statcast_pitches=[
            _pitch(at_bat=1, number=1, batter=10, pitcher=20, event="home_run"),
            later,
        ],
    )

    assert result.status == "passed"
    assert result.fields_matched == result.fields_compared


def test_ambiguous_multi_stint_aggregate_fails_closed() -> None:
    with pytest.raises(ValueError, match="ambiguous Baseball-Reference batting"):
        reconcile_baseball_reference_to_statcast(
            through_date=date(2026, 7, 12),
            batting_aggregates=[
                _batting_aggregate(team="LAD", stint="LAD:1"),
                _batting_aggregate(team="SF", stint="SF:1"),
            ],
            pitching_aggregates=[],
            statcast_pitches=[],
        )


def test_schedule_pair_requires_both_clubs_and_exact_agreement() -> None:
    assert reconcile_schedule_pair(
        game_key="g1", home_observation={"status": "final"}, away_observation=None
    ).status == "failed"
    observation = {
        "official_date": "2026-07-16",
        "home_team_key": "MIN",
        "away_team_key": "CWS",
        "status": "final",
        "home_score": 4,
        "away_score": 2,
    }
    assert reconcile_schedule_pair(
        game_key="g1", home_observation=observation, away_observation=dict(observation)
    ).status == "passed"
