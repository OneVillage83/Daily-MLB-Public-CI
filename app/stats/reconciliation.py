from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Mapping, Sequence

from app.stats.features import BattingAggregateLine, PitchingAggregateLine
from app.stats.normalization import (
    NormalizedPitch,
    statcast_plate_appearance_counts_as_at_bat,
)


COUNTING_RECONCILIATION_CONTRACT_VERSION = (
    "DSE_BREF_STATCAST_COUNTING_RECONCILIATION_V3"
)


class ReconciliationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    severity: ReconciliationSeverity
    code: str
    message: str
    expected: int | float | str | None = None
    observed: int | float | str | None = None
    explanation: str | None = None
    entity_key: str | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSummary:
    status: str
    items: tuple[ReconciliationItem, ...]

    @property
    def failure_count(self) -> int:
        return sum(item.severity is ReconciliationSeverity.ERROR for item in self.items)

    @property
    def warning_count(self) -> int:
        return sum(item.severity is ReconciliationSeverity.WARNING for item in self.items)


@dataclass(frozen=True, slots=True)
class CrossSourceCountingReconciliation:
    """Same-cutoff Baseball-Reference aggregate versus Statcast fact comparison."""

    through_date: date
    status: str
    items: tuple[ReconciliationItem, ...]
    entities_compared: int
    fields_compared: int
    fields_matched: int
    explained_difference_count: int
    unexplained_difference_count: int


_SOURCE_TOTAL_TEAM_RE = re.compile(r"^[1-9][0-9]*TM$")
_PITCHER_TERMINAL_ASSIGNMENT_EXPLANATION = (
    "Statcast assigns a completed plate appearance to the pitcher on its terminal "
    "pitch; official pitcher responsibility can differ when a pitching change occurs "
    "during the plate appearance."
)


def validate_schema_columns(
    *,
    dataset: str,
    observed: Sequence[str],
    required: set[str] | frozenset[str],
) -> ReconciliationSummary:
    duplicates = sorted({column for column in observed if observed.count(column) > 1})
    missing = sorted(set(required) - set(observed))
    items: list[ReconciliationItem] = []
    if duplicates:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.ERROR,
                "duplicate_source_columns",
                f"{dataset} contains duplicate columns: {duplicates}",
            )
        )
    if missing:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.ERROR,
                "missing_required_source_columns",
                f"{dataset} is missing required columns: {missing}",
            )
        )
    if not items:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.INFO,
                "source_schema_valid",
                f"{dataset} contains every required source column",
            )
        )
    return ReconciliationSummary(
        status="failed" if any(item.severity is ReconciliationSeverity.ERROR for item in items) else "passed",
        items=tuple(items),
    )


def reconcile_counting_stats(
    *,
    entity_key: str,
    source: Mapping[str, int | float | None],
    derived: Mapping[str, int | float | None],
    critical_fields: frozenset[str] = frozenset(),
    explanations: Mapping[str, str] | None = None,
) -> ReconciliationSummary:
    items: list[ReconciliationItem] = []
    explained = explanations or {}
    for field in sorted(set(source) | set(derived)):
        expected = source.get(field)
        observed = derived.get(field)
        if expected == observed:
            continue
        explanation = explained.get(field)
        severity = (
            ReconciliationSeverity.INFO
            if explanation is not None
            else ReconciliationSeverity.ERROR
            if field in critical_fields
            else ReconciliationSeverity.WARNING
        )
        items.append(
            ReconciliationItem(
                severity,
                (
                    "counting_stat_mismatch_explained"
                    if explanation is not None
                    else "counting_stat_mismatch"
                ),
                f"{entity_key} {field} differs between source aggregate and derived facts",
                expected=expected,
                observed=observed,
                explanation=explanation,
                entity_key=entity_key,
                field=field,
            )
        )
    if not items:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.INFO,
                "counting_stats_match",
                f"{entity_key} counting statistics match",
                entity_key=entity_key,
            )
        )
    status = "failed" if any(
        item.severity is ReconciliationSeverity.ERROR for item in items
    ) else "completed_with_warnings" if any(
        item.severity is ReconciliationSeverity.WARNING for item in items
    ) else "passed"
    return ReconciliationSummary(status=status, items=tuple(items))


_HIT_EVENTS = frozenset({"single", "double", "triple", "home_run"})
_WALK_EVENTS = frozenset({"walk", "intent_walk"})
_STRIKEOUT_EVENTS = frozenset({"strikeout", "strikeout_double_play"})
_TRUNCATED_PLATE_APPEARANCE_EVENT = "truncated_pa"


@dataclass(frozen=True, slots=True)
class _StatcastCountingDerivation:
    batting: dict[int, dict[str, int]]
    pitching: dict[int, dict[str, int]]
    transition_assignment_values: Mapping[
        int, Mapping[str, frozenset[int]]
    ]


def _empty_batting_counts() -> dict[str, int]:
    return {"PA": 0, "AB": 0, "H": 0, "HR": 0, "BB": 0, "SO": 0}


def _empty_pitching_counts() -> dict[str, int]:
    return {"BF": 0, "H": 0, "HR": 0, "BB": 0, "SO": 0}


def _apply_pitcher_event(counts: dict[str, int], event: str) -> None:
    counts["BF"] += 1
    if event in _HIT_EVENTS:
        counts["H"] += 1
    if event == "home_run":
        counts["HR"] += 1
    if event in _WALK_EVENTS:
        counts["BB"] += 1
    if event in _STRIKEOUT_EVENTS:
        counts["SO"] += 1


def _derive_statcast_counting_evidence(
    pitches: Sequence[NormalizedPitch],
) -> _StatcastCountingDerivation:
    plate_appearances: defaultdict[
        tuple[int, int], list[NormalizedPitch]
    ] = defaultdict(list)
    for pitch in sorted(
        pitches,
        key=lambda item: (
            item.game_pk,
            item.at_bat_number,
            item.pitch_number,
            item.checksum_sha256,
        ),
    ):
        plate_appearances[(pitch.game_pk, pitch.at_bat_number)].append(pitch)

    batting: dict[int, dict[str, int]] = {}
    pitching: dict[int, dict[str, int]] = {}
    transition_deltas: defaultdict[
        int, defaultdict[str, list[int]]
    ] = defaultdict(lambda: defaultdict(list))
    for _, rows in sorted(plate_appearances.items()):
        maximum_pitch_number = max(row.pitch_number for row in rows)
        terminal_candidates = [
            row for row in rows if row.pitch_number == maximum_pitch_number
        ]
        if len({row.checksum_sha256 for row in terminal_candidates}) != 1:
            raise ValueError("conflicting terminal Statcast pitch observation")
        terminal = terminal_candidates[-1]
        event = str(terminal.metrics.get("events") or "").strip().casefold()
        if not event or event == _TRUNCATED_PLATE_APPEARANCE_EVENT:
            continue

        batter = batting.setdefault(terminal.batter_id, _empty_batting_counts())
        terminal_pitcher = pitching.setdefault(
            terminal.pitcher_id, _empty_pitching_counts()
        )
        initial = rows[0]
        pitching.setdefault(initial.pitcher_id, _empty_pitching_counts())
        batter["PA"] += 1
        if statcast_plate_appearance_counts_as_at_bat(event, terminal.metrics):
            batter["AB"] += 1
        if event in _HIT_EVENTS:
            batter["H"] += 1
        if event == "home_run":
            batter["HR"] += 1
        if event in _WALK_EVENTS:
            batter["BB"] += 1
        if event in _STRIKEOUT_EVENTS:
            batter["SO"] += 1
        _apply_pitcher_event(terminal_pitcher, event)

        # A transition explanation is valid only when the retained pitch
        # sequence proves that responsibility moved from the opening pitcher
        # to a different terminal pitcher.  Merely seeing multiple pitcher
        # ids (for example, a pitcher returning later in the same PA) is not
        # sufficient evidence for attributing a source mismatch.
        pitcher_ids = {row.pitcher_id for row in rows}
        if len(pitcher_ids) > 1 and initial.pitcher_id != terminal.pitcher_id:
            supported_fields = ("BF", "BB") if event in _WALK_EVENTS else ("BF",)
            for field in supported_fields:
                # Terminal-pitch attribution is the derived baseline. Official
                # responsibility may instead move this one field count to the
                # opening pitcher for this specific plate appearance.
                transition_deltas[terminal.pitcher_id][field].append(-1)
                transition_deltas[initial.pitcher_id][field].append(1)

    transition_assignment_values: dict[
        int, dict[str, frozenset[int]]
    ] = {}
    for player_id, field_deltas in sorted(transition_deltas.items()):
        baseline = pitching[player_id]
        transition_assignment_values[player_id] = {}
        for field, deltas in sorted(field_deltas.items()):
            reachable = {baseline[field]}
            for delta in deltas:
                reachable.update(value + delta for value in tuple(reachable))
            transition_assignment_values[player_id][field] = frozenset(reachable)

    return _StatcastCountingDerivation(
        batting=batting,
        pitching=pitching,
        transition_assignment_values=transition_assignment_values,
    )


def derive_statcast_counting_stats(
    pitches: Sequence[NormalizedPitch],
) -> tuple[dict[int, dict[str, int]], dict[int, dict[str, int]]]:
    """Derive core batter/pitcher counts from terminal completed plate appearances."""

    derived = _derive_statcast_counting_evidence(pitches)
    return derived.batting, derived.pitching


def _is_source_total(source_team_id: str | None) -> bool:
    value = (source_team_id or "").strip().upper()
    return value == "TOT" or _SOURCE_TOTAL_TEAM_RE.fullmatch(value) is not None


def _select_batting_season_rows(
    rows: Sequence[BattingAggregateLine], through_date: date
) -> dict[str, BattingAggregateLine]:
    candidates: defaultdict[str, list[BattingAggregateLine]] = defaultdict(list)
    for row in rows:
        if row.window_key == "season_to_date" and row.through_date == through_date:
            candidates[row.player_id].append(row)
    selected: dict[str, BattingAggregateLine] = {}
    for player_id, player_rows in sorted(candidates.items()):
        if len(player_rows) == 1:
            selected[player_id] = player_rows[0]
            continue
        identities = [
            (row.source_team_id, row.source_stint_key) for row in player_rows
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "duplicate Baseball-Reference batting aggregate team/stint identity"
            )
        totals = [row for row in player_rows if _is_source_total(row.source_team_id)]
        if len(totals) != 1:
            raise ValueError(
                "ambiguous Baseball-Reference batting aggregate team/stint identity"
            )
        selected[player_id] = totals[0]
    return selected


def _select_pitching_season_rows(
    rows: Sequence[PitchingAggregateLine], through_date: date
) -> dict[str, PitchingAggregateLine]:
    candidates: defaultdict[str, list[PitchingAggregateLine]] = defaultdict(list)
    for row in rows:
        if row.window_key == "season_to_date" and row.through_date == through_date:
            candidates[row.player_id].append(row)
    selected: dict[str, PitchingAggregateLine] = {}
    for player_id, player_rows in sorted(candidates.items()):
        if len(player_rows) == 1:
            selected[player_id] = player_rows[0]
            continue
        identities = [
            (row.source_team_id, row.source_stint_key) for row in player_rows
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "duplicate Baseball-Reference pitching aggregate team/stint identity"
            )
        totals = [row for row in player_rows if _is_source_total(row.source_team_id)]
        if len(totals) != 1:
            raise ValueError(
                "ambiguous Baseball-Reference pitching aggregate team/stint identity"
            )
        selected[player_id] = totals[0]
    return selected


def _canonical_mlbam_player_id(player_id: int) -> str:
    return f"mlb-player:mlbam:{player_id}"


def reconcile_baseball_reference_to_statcast(
    *,
    through_date: date,
    batting_aggregates: Sequence[BattingAggregateLine],
    pitching_aggregates: Sequence[PitchingAggregateLine],
    statcast_pitches: Sequence[NormalizedPitch],
) -> CrossSourceCountingReconciliation:
    """Compare official aggregates with terminal-pitch facts at one exact cutoff.

    The function reports source differences instead of treating cross-provider equality
    as a completeness requirement. Pitcher BF and BB differences are explicitly marked
    as explained because terminal-pitch attribution is not identical to official pitcher
    responsibility for a plate appearance spanning a pitching change. Other differences
    remain warnings requiring review.
    """

    source_batting_rows = _select_batting_season_rows(
        batting_aggregates, through_date
    )
    source_pitching_rows = _select_pitching_season_rows(
        pitching_aggregates, through_date
    )
    eligible_pitches = tuple(
        pitch
        for pitch in statcast_pitches
        if pitch.game_date.year == through_date.year
        and pitch.game_date <= through_date
    )
    derived_evidence = _derive_statcast_counting_evidence(eligible_pitches)
    derived_batting_raw = derived_evidence.batting
    derived_pitching_raw = derived_evidence.pitching
    source_batting = {
        player_id: {
            "PA": row.pa,
            "AB": row.ab,
            "H": row.hits,
            "HR": row.home_runs,
            "BB": row.walks,
            "SO": row.strikeouts,
        }
        for player_id, row in source_batting_rows.items()
    }
    source_pitching = {
        player_id: {
            "BF": row.batters_faced,
            "H": row.hits,
            "HR": row.home_runs,
            "BB": row.walks,
            "SO": row.strikeouts,
        }
        for player_id, row in source_pitching_rows.items()
    }
    derived_batting = {
        _canonical_mlbam_player_id(player_id): counts
        for player_id, counts in derived_batting_raw.items()
    }
    derived_pitching = {
        _canonical_mlbam_player_id(player_id): counts
        for player_id, counts in derived_pitching_raw.items()
    }

    items: list[ReconciliationItem] = []
    fields_compared = 0
    fields_matched = 0
    explained_count = 0
    unexplained_count = 0
    entities_compared = 0
    comparisons = (
        ("batter", source_batting, derived_batting),
        ("pitcher", source_pitching, derived_pitching),
    )
    for role, source_by_player, derived_by_player in comparisons:
        for player_id in sorted(set(source_by_player) | set(derived_by_player)):
            entities_compared += 1
            source = source_by_player.get(player_id)
            derived = derived_by_player.get(player_id)
            field_names = sorted(
                set(source or {}) | set(derived or {})
            )
            fields_compared += len(field_names)
            fields_matched += sum(
                source is not None
                and derived is not None
                and source.get(field) == derived.get(field)
                for field in field_names
            )
            explanations: dict[str, str] = {}
            if role == "pitcher" and source is not None and derived is not None:
                mlbam_player_id = int(player_id.rsplit(":", 1)[-1])
                assignment_values = (
                    derived_evidence.transition_assignment_values.get(
                        mlbam_player_id, {}
                    )
                )
                explanations = {
                    field: _PITCHER_TERMINAL_ASSIGNMENT_EXPLANATION
                    for field in ("BF", "BB")
                    if field in assignment_values
                    and source.get(field) != derived.get(field)
                    and source.get(field) in assignment_values[field]
                }
            result = reconcile_counting_stats(
                entity_key=f"{role}:{player_id}",
                source=(
                    source
                    if source is not None
                    else {field: None for field in field_names}
                ),
                derived=(
                    derived
                    if derived is not None
                    else {field: None for field in field_names}
                ),
                explanations=explanations,
            )
            differences = tuple(
                item for item in result.items if item.code != "counting_stats_match"
            )
            items.extend(differences)
            explained_count += sum(
                item.code == "counting_stat_mismatch_explained"
                for item in differences
            )
            unexplained_count += sum(
                item.code == "counting_stat_mismatch" for item in differences
            )

    if entities_compared == 0:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.WARNING,
                "cross_source_counting_stats_unavailable",
                "No same-cutoff Baseball-Reference and Statcast counting facts were available",
            )
        )
        unexplained_count += 1
    elif not items:
        items.append(
            ReconciliationItem(
                ReconciliationSeverity.INFO,
                "cross_source_counting_stats_match",
                "Every comparable same-cutoff counting statistic matched",
            )
        )
    status = (
        "completed_with_warnings"
        if any(item.severity is ReconciliationSeverity.WARNING for item in items)
        else "passed"
    )
    return CrossSourceCountingReconciliation(
        through_date=through_date,
        status=status,
        items=tuple(items),
        entities_compared=entities_compared,
        fields_compared=fields_compared,
        fields_matched=fields_matched,
        explained_difference_count=explained_count,
        unexplained_difference_count=unexplained_count,
    )


def reconcile_schedule_pair(
    *,
    game_key: str,
    home_observation: Mapping[str, object] | None,
    away_observation: Mapping[str, object] | None,
) -> ReconciliationSummary:
    if home_observation is None or away_observation is None:
        return ReconciliationSummary(
            status="failed",
            items=(
                ReconciliationItem(
                    ReconciliationSeverity.ERROR,
                    "missing_team_schedule_observation",
                    f"{game_key} is not represented by both club schedules",
                ),
            ),
        )
    fields = ("official_date", "home_team_key", "away_team_key", "status", "home_score", "away_score")
    mismatches = [field for field in fields if home_observation.get(field) != away_observation.get(field)]
    if mismatches:
        return ReconciliationSummary(
            status="failed",
            items=(
                ReconciliationItem(
                    ReconciliationSeverity.ERROR,
                    "team_schedule_conflict",
                    f"{game_key} club schedule observations disagree: {mismatches}",
                ),
            ),
        )
    return ReconciliationSummary(
        status="passed",
        items=(
            ReconciliationItem(
                ReconciliationSeverity.INFO,
                "team_schedule_pair_matches",
                f"{game_key} is consistent across both club schedules",
            ),
        ),
    )
