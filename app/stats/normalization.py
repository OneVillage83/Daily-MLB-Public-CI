from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping

from app.stats.identities import require_active_team_identity


REGULAR_SEASON_GAME_TYPES = frozenset({"r", "regular", "regular_season"})
NULL_TOKENS = frozenset({"", "na", "n/a", "null", "none", "nan", "--"})
STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT = (
    "DSE_STATCAST_PLATE_APPEARANCE_CLASSIFICATION_V1"
)
DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT = (
    "defensive_shift_violation_non_at_bat"
)
_DEFENSIVE_SHIFT_VIOLATION_RE = re.compile(
    r"\bdefensive\s+shift\s+violation\s+error\b", re.IGNORECASE
)


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED_RESCHEDULED = "postponed_rescheduled"
    SUSPENDED_PENDING = "suspended_pending"
    CANCELLED_NO_GAME = "cancelled_no_game"
    ABANDONED = "abandoned"
    SOURCE_INCOMPLETE = "source_incomplete"
    VALIDATION_FAILED = "validation_failed"


@dataclass(frozen=True, slots=True)
class ScheduleGame:
    provider_game_id: str
    official_date: date
    home_team_key: str
    away_team_key: str
    status: GameStatus
    status_reason: str
    home_score: int | None = None
    away_score: int | None = None
    source_row: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class NormalizedPitch:
    game_pk: int
    game_date: date
    at_bat_number: int
    pitch_number: int
    batter_id: int
    pitcher_id: int
    home_team_key: str
    away_team_key: str
    metrics: Mapping[str, Any]
    checksum_sha256: str

    @property
    def identity(self) -> tuple[int, int, int]:
        return self.game_pk, self.at_bat_number, self.pitch_number


@dataclass(frozen=True, slots=True)
class PitchNormalizationResult:
    pitches: tuple[NormalizedPitch, ...]
    excluded_count: int
    duplicate_count: int
    conflicts: tuple[tuple[tuple[int, int, int], str, str], ...]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def checksum_payload(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.casefold() in NULL_TOKENS:
        return None
    try:
        number = float(text.replace(",", ""))
    except ValueError:
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    if text.casefold() in NULL_TOKENS:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _first(row: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _parse_date(value: object, *, season: int) -> date:
    text = str(value or "").strip()
    try:
        parsed = date.fromisoformat(text[:10])
    except ValueError:
        cleaned = re.sub(r"^[A-Za-z]+,\s*", "", text)
        cleaned = re.sub(r"\s*\(.*\)$", "", cleaned).strip()
        for pattern in ("%b %d", "%B %d"):
            try:
                parsed = datetime.strptime(cleaned, pattern).date().replace(year=season)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Unrecognized schedule date: {text!r}")
    if parsed.year != season:
        raise ValueError(f"Schedule date {parsed.isoformat()} is outside season {season}")
    return parsed


def _status(row: Mapping[str, Any]) -> tuple[GameStatus, str]:
    explicit = str(_first(row, "status", "game_status") or "").strip().casefold()
    if explicit in {item.value for item in GameStatus}:
        return GameStatus(explicit), "explicit_source_status"
    result = str(_first(row, "win_loss_result", "w_l", "W/L", "result") or "").strip()
    notes = " ".join(
        str(_first(row, key) or "")
        for key in ("status_text", "date_game", "Date", "notes", "boxscore")
    ).casefold()
    if "suspend" in notes:
        return GameStatus.SUSPENDED_PENDING, "source_reports_suspended"
    if "postpon" in notes:
        return GameStatus.POSTPONED_RESCHEDULED, "source_reports_postponed"
    if "cancel" in notes or "no game" in notes:
        return GameStatus.CANCELLED_NO_GAME, "source_reports_cancelled"
    if "abandon" in notes:
        return GameStatus.ABANDONED, "source_reports_abandoned"
    if any(token in notes for token in ("in progress", "live", "delayed")):
        return GameStatus.IN_PROGRESS, "source_reports_in_progress"
    home_score = optional_int(_first(row, "home_score", "R_home"))
    away_score = optional_int(_first(row, "away_score", "R_away"))
    team_score = optional_int(_first(row, "R", "runs"))
    opponent_score = optional_int(_first(row, "RA", "runs_allowed"))
    if result[:1].upper() in {"W", "L", "T"} and (
        (home_score is not None and away_score is not None)
        or (team_score is not None and opponent_score is not None)
    ):
        return GameStatus.FINAL, "result_and_scores_present"
    if not result and team_score is None and opponent_score is None:
        return GameStatus.SCHEDULED, "scheduled_without_final_result"
    return GameStatus.SOURCE_INCOMPLETE, "result_or_score_incomplete"


def normalize_baseball_reference_schedule_row(
    row: Mapping[str, Any],
    *,
    season: int,
    subject_team_source_id: str,
    row_index: int,
) -> ScheduleGame:
    subject = require_active_team_identity("baseball_reference", subject_team_source_id)
    opponent_source = str(_first(row, "opp_ID", "opp", "Opp") or "").strip()
    opponent = require_active_team_identity("baseball_reference", opponent_source)
    official_date = _parse_date(_first(row, "date_game", "Date", "date"), season=season)
    home_marker = str(_first(row, "homeORvis", "Home_Away", "home_away") or "").strip()
    subject_is_away = home_marker in {"@", "A", "away", "Away"}
    home = opponent if subject_is_away else subject
    away = subject if subject_is_away else opponent
    status, reason = _status(row)
    team_score = optional_int(_first(row, "R", "runs"))
    opponent_score = optional_int(_first(row, "RA", "runs_allowed"))
    home_score = opponent_score if subject_is_away else team_score
    away_score = team_score if subject_is_away else opponent_score
    source_game_id = str(_first(row, "game_id", "boxscore", "boxscore_word") or "").strip()
    if not source_game_id:
        source_game_id = (
            f"bref:{official_date.isoformat()}:{away.canonical_team_key}:"
            f"{home.canonical_team_key}:{row_index}"
        )
    return ScheduleGame(
        provider_game_id=source_game_id,
        official_date=official_date,
        home_team_key=str(home.canonical_team_key),
        away_team_key=str(away.canonical_team_key),
        status=status,
        status_reason=reason,
        home_score=home_score,
        away_score=away_score,
        source_row=dict(row),
    )


_STATCAST_METRIC_FIELDS = (
    # Pitch identity / classification context retained inside metrics.
    "pitch_type",
    "pitch_name",

    # Pitch velocity, release, spin, movement, and trajectory.
    "release_speed",
    "effective_speed",
    "release_spin_rate",
    "spin_axis",
    "release_extension",
    "release_pos_x",
    "release_pos_y",
    "release_pos_z",
    "pfx_x",
    "pfx_z",
    "plate_x",
    "plate_z",
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
    "zone",
    "sz_top",
    "sz_bot",

    # Count / inning / plate-appearance state.
    "balls",
    "strikes",
    "outs_when_up",
    "inning",
    "inning_topbot",
    "description",
    "events",
    "type",

    # Batted-ball and expected-outcome measurements.
    "launch_speed",
    "launch_angle",
    "launch_speed_angle",
    "hit_distance_sc",
    "hc_x",
    "hc_y",
    "hit_location",
    "bb_type",
    "estimated_ba_using_speedangle",
    "estimated_slg_using_speedangle",
    "estimated_woba_using_speedangle",
    "woba_value",
    "woba_denom",
    "babip_value",
    "iso_value",

    # Bat / swing tracking.
    "bat_speed",
    "swing_length",
    "attack_angle",
    "attack_direction",
    "swing_path_tilt",
    "miss_distance",
    "hyper_speed",

    # Score / base / lineup-turn context.
    "home_score",
    "away_score",
    "post_home_score",
    "post_away_score",
    "bat_score",
    "fld_score",
    "post_bat_score",
    "post_fld_score",
    "bat_score_diff",
    "home_score_diff",
    "on_1b",
    "on_2b",
    "on_3b",
    "n_thruorder_pitcher",
    "n_priorpa_thisgame_player_at_bat",

    # Run / win-value observations available on the pitch.
    "delta_home_win_exp",
    "delta_run_exp",

    # Batter / pitcher handedness.
    "stand",
    "p_throws",

    # Defensive context.
    "if_fielding_alignment",
    "of_fielding_alignment",
    "fielder_2",
    "fielder_3",
    "fielder_4",
    "fielder_5",
    "fielder_6",
    "fielder_7",
    "fielder_8",
    "fielder_9",

    # Rest / workload information known before the game.
    "pitcher_days_since_prev_game",
    "batter_days_since_prev_game",

    # Source / QA context.
    "umpire",
    "sv_id",

    # Doubleheader context.
    "game_number",
    "doubleheader_sequence",
)


def _statcast_plate_appearance_classification(
    row: Mapping[str, Any],
) -> Mapping[str, str] | None:
    event = str(row.get("events") or "").strip().casefold()
    description = " ".join(str(row.get("des") or "").split())
    if event != "field_error" or not _DEFENSIVE_SHIFT_VIOLATION_RE.search(
        description
    ):
        return None
    return {
        "contract_version": STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT,
        "classification": DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT,
        "source_field": "des",
    }


def statcast_plate_appearance_counts_as_at_bat(
    event: str, metrics: Mapping[str, Any]
) -> bool:
    normalized_event = event.strip().casefold()
    if normalized_event in _NON_AT_BAT_EVENTS_FOR_CLASSIFICATION:
        return False
    return not (
        normalized_event == "field_error"
        and statcast_plate_appearance_classification(metrics)
        == DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT
    )


def statcast_plate_appearance_classification(
    metrics: Mapping[str, Any],
) -> str | None:
    marker = metrics.get("_dse_plate_appearance_classification")
    if not isinstance(marker, Mapping):
        return None
    if (
        marker.get("contract_version")
        != STATCAST_PLATE_APPEARANCE_CLASSIFICATION_CONTRACT
        or marker.get("source_field") != "des"
    ):
        return None
    classification = marker.get("classification")
    return str(classification) if classification is not None else None


_NON_AT_BAT_EVENTS_FOR_CLASSIFICATION = frozenset(
    {
        "walk",
        "intent_walk",
        "hit_by_pitch",
        "sac_bunt",
        "sac_fly",
        "sac_fly_double_play",
        "catcher_interf",
    }
)


def normalize_statcast_rows(rows: Iterable[Mapping[str, Any]]) -> PitchNormalizationResult:
    pitches: dict[tuple[int, int, int], NormalizedPitch] = {}
    excluded = 0
    duplicates = 0
    conflicts: list[tuple[tuple[int, int, int], str, str]] = []
    for row in rows:
        game_type = str(row.get("game_type") or "").strip().casefold()
        if game_type not in REGULAR_SEASON_GAME_TYPES:
            excluded += 1
            continue
        game_pk = optional_int(row.get("game_pk"))
        at_bat = optional_int(row.get("at_bat_number"))
        pitch_number = optional_int(row.get("pitch_number"))
        batter = optional_int(row.get("batter"))
        pitcher = optional_int(row.get("pitcher"))
        if (
            game_pk is None
            or at_bat is None
            or pitch_number is None
            or batter is None
            or pitcher is None
        ):
            raise ValueError("Statcast pitch identity fields must be non-null integers")
        raw_game_date = str(row.get("game_date") or "")
        try:
            game_date_year = date.fromisoformat(raw_game_date[:10]).year
        except ValueError as exc:
            raise ValueError("Statcast game_date must be an ISO calendar date") from exc
        game_date = _parse_date(raw_game_date, season=game_date_year)
        home = require_active_team_identity("statcast", str(row.get("home_team") or ""))
        away = require_active_team_identity("statcast", str(row.get("away_team") or ""))
        metrics = {key: row.get(key) for key in _STATCAST_METRIC_FIELDS if key in row}
        plate_appearance_classification = _statcast_plate_appearance_classification(
            row
        )
        if plate_appearance_classification is not None:
            metrics["_dse_plate_appearance_classification"] = (
                plate_appearance_classification
            )
        payload = {
            "game_pk": game_pk,
            "game_date": game_date.isoformat(),
            "at_bat_number": at_bat,
            "pitch_number": pitch_number,
            "batter": batter,
            "pitcher": pitcher,
            "home_team_key": home.canonical_team_key,
            "away_team_key": away.canonical_team_key,
            "metrics": metrics,
        }
        checksum = checksum_payload(payload)
        normalized = NormalizedPitch(
            game_pk=int(game_pk),
            game_date=game_date,
            at_bat_number=int(at_bat),
            pitch_number=int(pitch_number),
            batter_id=int(batter),
            pitcher_id=int(pitcher),
            home_team_key=str(home.canonical_team_key),
            away_team_key=str(away.canonical_team_key),
            metrics=metrics,
            checksum_sha256=checksum,
        )
        prior = pitches.get(normalized.identity)
        if prior is None:
            pitches[normalized.identity] = normalized
        elif prior.checksum_sha256 == checksum:
            duplicates += 1
        else:
            conflicts.append((normalized.identity, prior.checksum_sha256, checksum))
    return PitchNormalizationResult(
        pitches=tuple(pitches[key] for key in sorted(pitches)),
        excluded_count=excluded,
        duplicate_count=duplicates,
        conflicts=tuple(conflicts),
    )
