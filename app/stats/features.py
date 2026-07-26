from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import fmean
from typing import Any, Iterable, Sequence

from app.stats.normalization import DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT


FEATURE_VERSION_V2 = "DSE_MLB_STATS_FEATURES_V2"
FEATURE_VERSION_V3 = "DSE_MLB_STATS_FEATURES_V3"

# Existing builders intentionally remain V2 until the V3 calculators
# are introduced and validated separately.
FEATURE_VERSION = FEATURE_VERSION_V2

ROLLING_WINDOWS = (7, 14, 30)
AGGREGATE_WINDOW_KEYS = frozenset(
    {"season_to_date", *(f"rolling_{days}_days" for days in ROLLING_WINDOWS)}
)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


def _finite_mean(values: Iterable[float | None]) -> float | None:
    finite = [float(value) for value in values if value is not None and math.isfinite(value)]
    return fmean(finite) if finite else None


def _finite_count(values: Iterable[float | int | None]) -> int:
    """Count only finite numeric observations."""
    return sum(
        value is not None and math.isfinite(float(value))
        for value in values
    )


def _checksum(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BattingGameLine:
    game_date: date
    player_id: str
    team_key: str
    pa: int = 0
    ab: int = 0
    hits: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    strikeouts: int = 0
    sacrifice_flies: int = 0
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class BattingAggregateLine:
    """A provider aggregate whose date window is explicit and point-in-time bounded."""

    through_date: date
    player_id: str
    window_key: str
    pa: int = 0
    ab: int = 0
    hits: int = 0
    doubles: int = 0
    triples: int = 0
    home_runs: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    strikeouts: int = 0
    sacrifice_flies: int = 0
    source_team_id: str | None = field(default=None, kw_only=True)
    source_stint_key: str | None = field(default=None, kw_only=True)
    team_key: str | None = field(default=None, kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.window_key not in AGGREGATE_WINDOW_KEYS:
            raise ValueError("unsupported batting aggregate window")
        if any(
            value < 0
            for value in (
                self.pa,
                self.ab,
                self.hits,
                self.doubles,
                self.triples,
                self.home_runs,
                self.walks,
                self.hit_by_pitch,
                self.strikeouts,
                self.sacrifice_flies,
            )
        ):
            raise ValueError("batting aggregate counts must be nonnegative")
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )
        for name in ("source_team_id", "source_stint_key", "team_key"):
            value = getattr(self, name)
            object.__setattr__(self, name, value.strip() if value else None)

    def as_game_line(self) -> BattingGameLine:
        return BattingGameLine(
            self.through_date,
            self.player_id,
            "AGGREGATE",
            self.pa,
            self.ab,
            self.hits,
            self.doubles,
            self.triples,
            self.home_runs,
            self.walks,
            self.hit_by_pitch,
            self.strikeouts,
            self.sacrifice_flies,
            available_at=self.available_at,
        )


@dataclass(frozen=True, slots=True)
class PitchingGameLine:
    game_date: date
    player_id: str
    team_key: str
    batters_faced: int = 0
    outs_recorded: int = 0
    hits: int = 0
    earned_runs: int = 0
    home_runs: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    strikeouts: int = 0
    pitches: int = 0
    is_start: bool = False
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class PitchingAggregateLine:
    """A provider pitching aggregate with an explicit source window."""

    through_date: date
    player_id: str
    window_key: str
    batters_faced: int = 0
    outs_recorded: int = 0
    hits: int = 0
    earned_runs: int = 0
    home_runs: int = 0
    walks: int = 0
    hit_by_pitch: int = 0
    strikeouts: int = 0
    pitches: int = 0
    source_team_id: str | None = field(default=None, kw_only=True)
    source_stint_key: str | None = field(default=None, kw_only=True)
    team_key: str | None = field(default=None, kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.window_key not in AGGREGATE_WINDOW_KEYS:
            raise ValueError("unsupported pitching aggregate window")
        if any(
            value < 0
            for value in (
                self.batters_faced,
                self.outs_recorded,
                self.hits,
                self.earned_runs,
                self.home_runs,
                self.walks,
                self.hit_by_pitch,
                self.strikeouts,
                self.pitches,
            )
        ):
            raise ValueError("pitching aggregate counts must be nonnegative")
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )
        for name in ("source_team_id", "source_stint_key", "team_key"):
            value = getattr(self, name)
            object.__setattr__(self, name, value.strip() if value else None)

    def as_game_line(self) -> PitchingGameLine:
        return PitchingGameLine(
            self.through_date,
            self.player_id,
            "AGGREGATE",
            self.batters_faced,
            self.outs_recorded,
            self.hits,
            self.earned_runs,
            self.home_runs,
            self.walks,
            self.hit_by_pitch,
            self.strikeouts,
            self.pitches,
            False,
            available_at=self.available_at,
        )


@dataclass(frozen=True, slots=True)
class StatcastBattedBall:
    game_date: date
    batter_id: str
    launch_speed: float | None = None
    launch_angle: float | None = None
    launch_speed_angle: int | None = None
    estimated_ba: float | None = None
    estimated_slg: float | None = None
    estimated_woba: float | None = None

    # Statcast V3 contact/location inputs. These are transport fields only
    # for now; the V2 feature contract deliberately does not consume them.
    hit_distance_sc: float | None = field(default=None, kw_only=True)
    hc_x: float | None = field(default=None, kw_only=True)
    hc_y: float | None = field(default=None, kw_only=True)
    hit_location: int | None = field(default=None, kw_only=True)

    is_home: bool | None = field(default=None, kw_only=True)
    batter_hand: str | None = field(default=None, kw_only=True)
    opposing_pitcher_hand: str | None = field(default=None, kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.is_home is not None and not isinstance(self.is_home, bool):
            raise ValueError("is_home must be true, false, or null")
        for name in ("batter_hand", "opposing_pitcher_hand"):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                value.strip().upper() if value and value.strip() else None,
            )
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class StatcastSwingMetric:
    """A batter swing observation retained independently of batted-ball events."""

    game_date: date
    batter_id: str

    bat_speed: float | None = field(default=None, kw_only=True)
    swing_length: float | None = field(default=None, kw_only=True)
    attack_angle: float | None = field(default=None, kw_only=True)
    attack_direction: float | None = field(default=None, kw_only=True)
    swing_path_tilt: float | None = field(default=None, kw_only=True)
    miss_distance: float | None = field(default=None, kw_only=True)
    hyper_speed: float | None = field(default=None, kw_only=True)

    pitch_type: str | None = field(default=None, kw_only=True)
    is_home: bool | None = field(default=None, kw_only=True)
    batter_hand: str | None = field(default=None, kw_only=True)
    opposing_pitcher_hand: str | None = field(default=None, kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.is_home is not None and not isinstance(self.is_home, bool):
            raise ValueError("is_home must be true, false, or null")

        for name in (
            "pitch_type",
            "batter_hand",
            "opposing_pitcher_hand",
        ):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                value.strip().upper() if value and value.strip() else None,
            )

        object.__setattr__(
            self,
            "available_at",
            _aware_utc(self.available_at, "available_at"),
        )


@dataclass(frozen=True, slots=True)
class StatcastPitchMetric:
    game_date: date
    pitcher_id: str
    team_key: str
    pitch_type: str | None = None
    release_speed: float | None = None
    release_spin_rate: float | None = None
    release_extension: float | None = None
    pfx_x: float | None = None
    pfx_z: float | None = None
    zone: int | None = None
    description: str | None = None

    # Statcast V3 pitcher/pitch-shape inputs. These remain kw-only so the
    # existing V2 positional constructor contract stays backward compatible.
    effective_speed: float | None = field(default=None, kw_only=True)
    spin_axis: int | None = field(default=None, kw_only=True)

    release_pos_x: float | None = field(default=None, kw_only=True)
    release_pos_y: float | None = field(default=None, kw_only=True)
    release_pos_z: float | None = field(default=None, kw_only=True)

    vx0: float | None = field(default=None, kw_only=True)
    vy0: float | None = field(default=None, kw_only=True)
    vz0: float | None = field(default=None, kw_only=True)

    ax: float | None = field(default=None, kw_only=True)
    ay: float | None = field(default=None, kw_only=True)
    az: float | None = field(default=None, kw_only=True)

    api_break_x_arm: float | None = field(default=None, kw_only=True)
    api_break_x_batter_in: float | None = field(default=None, kw_only=True)
    api_break_z_with_gravity: float | None = field(default=None, kw_only=True)

    arm_angle: float | None = field(default=None, kw_only=True)
    sz_top: float | None = field(default=None, kw_only=True)
    sz_bot: float | None = field(default=None, kw_only=True)

    n_thruorder_pitcher: int | None = field(default=None, kw_only=True)
    pitcher_days_since_prev_game: int | None = field(
        default=None,
        kw_only=True,
    )

    opposing_batter_hand: str | None = field(default=None, kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opposing_batter_hand",
            (
                self.opposing_batter_hand.strip().upper()
                if self.opposing_batter_hand and self.opposing_batter_hand.strip()
                else None
            ),
        )
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class StatcastPitcherPitch:
    """A final-game Statcast pitch used to derive factual pitcher appearances.

    The acquisition layer must set ``game_is_final`` from the reconciled schedule
    state. Partial pitches remain raw evidence but are excluded here. ``outs_when_up``
    is the provider's pre-pitch/plate-appearance out state; it is never replaced with
    a guessed zero when absent.
    """

    game_pk: int
    game_date: date
    pitcher_id: str
    team_key: str
    at_bat_number: int
    pitch_number: int
    inning: int
    inning_half: str
    outs_when_up: int | None
    event: str | None = None
    description: str | None = None
    plate_appearance_classification: str | None = None
    batter_id: str | None = field(default=None, kw_only=True)
    batter_team_key: str | None = field(default=None, kw_only=True)
    batter_is_home: bool | None = field(default=None, kw_only=True)
    batter_hand: str | None = field(default=None, kw_only=True)
    pitcher_hand: str | None = field(default=None, kw_only=True)
    game_is_final: bool = field(kw_only=True)
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.game_pk <= 0:
            raise ValueError("game_pk must be positive")
        if not self.pitcher_id.strip() or not self.team_key.strip():
            raise ValueError("pitcher_id and team_key must not be empty")
        if self.at_bat_number < 0 or self.pitch_number <= 0 or self.inning <= 0:
            raise ValueError("Statcast pitch sequence values are invalid")
        half = self.inning_half.strip().casefold()
        if half not in {"top", "bottom"}:
            raise ValueError("inning_half must be top or bottom")
        if self.outs_when_up is not None and self.outs_when_up not in {0, 1, 2}:
            raise ValueError("outs_when_up must be 0, 1, 2, or null")
        if self.batter_is_home is not None and not isinstance(
            self.batter_is_home, bool
        ):
            raise ValueError("batter_is_home must be true, false, or null")
        object.__setattr__(self, "pitcher_id", self.pitcher_id.strip())
        object.__setattr__(self, "team_key", self.team_key.strip())
        for name in ("batter_id", "batter_team_key"):
            value = getattr(self, name)
            object.__setattr__(self, name, value.strip() if value and value.strip() else None)
        for name in ("batter_hand", "pitcher_hand"):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                value.strip().upper() if value and value.strip() else None,
            )
        object.__setattr__(self, "inning_half", half)
        object.__setattr__(
            self,
            "event",
            self.event.strip().casefold() if self.event and self.event.strip() else None,
        )
        object.__setattr__(
            self,
            "description",
            self.description.strip().casefold()
            if self.description and self.description.strip()
            else None,
        )
        object.__setattr__(
            self,
            "plate_appearance_classification",
            (
                self.plate_appearance_classification.strip().casefold()
                if self.plate_appearance_classification
                and self.plate_appearance_classification.strip()
                else None
            ),
        )
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class StatcastPlateAppearance:
    """One final-game terminal Statcast plate appearance for factual splits."""

    game_pk: int
    game_date: date
    batter_id: str | None
    pitcher_id: str
    batter_team_key: str | None
    pitcher_team_key: str
    batter_is_home: bool | None
    batter_hand: str | None
    pitcher_hand: str | None
    event: str | None
    outs_recorded: int | None
    outs_recorded_status: str
    attribution_status: str
    plate_appearance_classification: str | None = None
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.game_pk <= 0 or not self.pitcher_id.strip() or not self.pitcher_team_key.strip():
            raise ValueError("Statcast plate-appearance identity is invalid")
        if self.batter_is_home is not None and not isinstance(self.batter_is_home, bool):
            raise ValueError("batter_is_home must be true, false, or null")
        if self.outs_recorded_status not in {"known", "unknown"}:
            raise ValueError("unsupported outs_recorded_status")
        if (self.outs_recorded is None) != (self.outs_recorded_status == "unknown"):
            raise ValueError("outs_recorded and its status disagree")
        if self.outs_recorded is not None and self.outs_recorded not in {0, 1, 2, 3}:
            raise ValueError("outs_recorded must be between zero and three")
        if self.attribution_status not in {
            "supported",
            "ambiguous_pitcher_change",
            "ambiguous_batter_change",
        }:
            raise ValueError("unsupported plate-appearance attribution status")
        object.__setattr__(
            self, "batter_id", self.batter_id.strip() if self.batter_id else None
        )
        object.__setattr__(self, "pitcher_id", self.pitcher_id.strip())
        object.__setattr__(
            self,
            "batter_team_key",
            self.batter_team_key.strip() if self.batter_team_key else None,
        )
        object.__setattr__(self, "pitcher_team_key", self.pitcher_team_key.strip())
        for name in ("batter_hand", "pitcher_hand"):
            value = getattr(self, name)
            object.__setattr__(
                self,
                name,
                value.strip().upper() if value and value.strip() else None,
            )
        object.__setattr__(
            self,
            "event",
            self.event.strip().casefold() if self.event and self.event.strip() else None,
        )
        object.__setattr__(
            self,
            "plate_appearance_classification",
            (
                self.plate_appearance_classification.strip().casefold()
                if self.plate_appearance_classification
                and self.plate_appearance_classification.strip()
                else None
            ),
        )
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


@dataclass(frozen=True, slots=True)
class StatcastPitcherAppearance:
    game_pk: int
    game_date: date
    pitcher_id: str
    team_key: str
    is_start: bool
    pitches: int
    batters_faced: int
    hits: int
    home_runs: int
    walks: int
    hit_by_pitch: int
    strikeouts: int
    outs_recorded: int | None
    outs_recorded_status: str
    available_at: datetime = field(kw_only=True)

    def __post_init__(self) -> None:
        if self.game_pk <= 0 or not self.pitcher_id.strip() or not self.team_key.strip():
            raise ValueError("Statcast appearance identity is invalid")
        if any(
            value < 0
            for value in (
                self.pitches,
                self.batters_faced,
                self.hits,
                self.home_runs,
                self.walks,
                self.hit_by_pitch,
                self.strikeouts,
            )
        ):
            raise ValueError("Statcast appearance counts must be nonnegative")
        if self.outs_recorded_status not in {"known", "unknown"}:
            raise ValueError("unsupported outs_recorded_status")
        if (self.outs_recorded is None) != (
            self.outs_recorded_status == "unknown"
        ):
            raise ValueError("outs_recorded and its status disagree")
        if self.outs_recorded is not None and self.outs_recorded < 0:
            raise ValueError("outs_recorded must be nonnegative")
        object.__setattr__(self, "pitcher_id", self.pitcher_id.strip())
        object.__setattr__(self, "team_key", self.team_key.strip())
        object.__setattr__(
            self, "available_at", _aware_utc(self.available_at, "available_at")
        )


def hitter_rate_stats(lines: Sequence[BattingGameLine]) -> dict[str, int | float | None]:
    totals = {
        "pa": sum(item.pa for item in lines),
        "ab": sum(item.ab for item in lines),
        "h": sum(item.hits for item in lines),
        "2b": sum(item.doubles for item in lines),
        "3b": sum(item.triples for item in lines),
        "hr": sum(item.home_runs for item in lines),
        "bb": sum(item.walks for item in lines),
        "hbp": sum(item.hit_by_pitch for item in lines),
        "so": sum(item.strikeouts for item in lines),
        "sf": sum(item.sacrifice_flies for item in lines),
    }
    singles = max(0, totals["h"] - totals["2b"] - totals["3b"] - totals["hr"])
    total_bases = singles + (2 * totals["2b"]) + (3 * totals["3b"]) + (4 * totals["hr"])
    average = _ratio(totals["h"], totals["ab"])
    obp = _ratio(
        totals["h"] + totals["bb"] + totals["hbp"],
        totals["ab"] + totals["bb"] + totals["hbp"] + totals["sf"],
    )
    slugging = _ratio(total_bases, totals["ab"])
    babip = _ratio(
        totals["h"] - totals["hr"],
        totals["ab"] - totals["so"] - totals["hr"] + totals["sf"],
    )
    return {
        **totals,
        "1b": singles,
        "avg": average,
        "obp": obp,
        "slg": slugging,
        "ops": None if obp is None or slugging is None else obp + slugging,
        "iso": None if average is None or slugging is None else slugging - average,
        "babip": babip,
        "k_rate": _ratio(totals["so"], totals["pa"]),
        "bb_rate": _ratio(totals["bb"], totals["pa"]),
    }


def pitcher_rate_stats(lines: Sequence[PitchingGameLine]) -> dict[str, int | float | None]:
    totals = {
        "bf": sum(item.batters_faced for item in lines),
        "outs_recorded": sum(item.outs_recorded for item in lines),
        "h": sum(item.hits for item in lines),
        "er": sum(item.earned_runs for item in lines),
        "hr": sum(item.home_runs for item in lines),
        "bb": sum(item.walks for item in lines),
        "hbp": sum(item.hit_by_pitch for item in lines),
        "so": sum(item.strikeouts for item in lines),
        "pitches": sum(item.pitches for item in lines),
    }
    innings = totals["outs_recorded"] / 3.0
    k_rate = _ratio(totals["so"], totals["bf"])
    bb_rate = _ratio(totals["bb"], totals["bf"])
    return {
        **totals,
        "innings_pitched": innings,
        "era": _ratio(9.0 * totals["er"], innings),
        "whip": _ratio(totals["bb"] + totals["h"], innings),
        "k_rate": k_rate,
        "bb_rate": bb_rate,
        "k_minus_bb_rate": None if k_rate is None or bb_rate is None else k_rate - bb_rate,
        "k_per_9": _ratio(9.0 * totals["so"], innings),
        "bb_per_9": _ratio(9.0 * totals["bb"], innings),
        "hr_per_9": _ratio(9.0 * totals["hr"], innings),
    }


def _window_payload(
    values: Sequence[BattingGameLine] | Sequence[PitchingGameLine],
    calculator: Any,
) -> dict[str, Any]:
    calculated = calculator(values)
    if values:
        return {
            **calculated,
            "data_status": "available",
            "observation_count": len(values),
        }
    return {
        **{key: None for key in calculated},
        "data_status": "unavailable",
        "observation_count": 0,
    }


def batted_ball_stats(rows: Sequence[StatcastBattedBall]) -> dict[str, int | float | None]:
    tracked = [row for row in rows if row.launch_speed is not None]
    hard_hit = [row for row in tracked if float(row.launch_speed or 0) >= 95.0]
    barrels = [row for row in rows if row.launch_speed_angle == 6]
    return {
        "batted_ball_count": len(rows),
        "exit_velocity": _finite_mean(row.launch_speed for row in rows),
        "max_exit_velocity": max(
            (float(row.launch_speed) for row in tracked if row.launch_speed is not None),
            default=None,
        ),
        "hard_hit_rate": _ratio(len(hard_hit), len(tracked)),
        "barrel_rate": _ratio(len(barrels), len(rows)),
        "launch_angle": _finite_mean(row.launch_angle for row in rows),
        "xba": _finite_mean(row.estimated_ba for row in rows),
        "xslg": _finite_mean(row.estimated_slg for row in rows),
        "xwoba": _finite_mean(row.estimated_woba for row in rows),
    }


_SWING_DESCRIPTIONS = frozenset(
    {
        "swinging_strike", "swinging_strike_blocked", "foul", "foul_tip",
        "hit_into_play", "hit_into_play_no_out", "hit_into_play_score",
    }
)
_WHIFF_DESCRIPTIONS = frozenset({"swinging_strike", "swinging_strike_blocked"})
_CONTACT_DESCRIPTIONS = _SWING_DESCRIPTIONS - _WHIFF_DESCRIPTIONS
_STRIKE_DESCRIPTIONS = _SWING_DESCRIPTIONS | frozenset({"called_strike"})

_STATCAST_HIT_EVENTS = frozenset({"single", "double", "triple", "home_run"})
_STATCAST_WALK_EVENTS = frozenset({"walk", "intent_walk"})
_STATCAST_STRIKEOUT_EVENTS = frozenset(
    {"strikeout", "strikeout_double_play"}
)
_STATCAST_ONE_OUT_EVENTS = frozenset(
    {
        "caught_stealing_2b",
        "caught_stealing_3b",
        "caught_stealing_home",
        "field_out",
        "fielders_choice_out",
        "force_out",
        "other_out",
        "pickoff_1b",
        "pickoff_2b",
        "pickoff_3b",
        "pickoff_caught_stealing_2b",
        "pickoff_caught_stealing_3b",
        "pickoff_caught_stealing_home",
        "sac_bunt",
        "sac_fly",
        "strikeout",
    }
)
_STATCAST_TWO_OUT_EVENTS = frozenset(
    {
        "double_play",
        "grounded_into_double_play",
        "runner_double_play",
        "sac_bunt_double_play",
        "sac_fly_double_play",
        "strikeout_double_play",
    }
)
_STATCAST_THREE_OUT_EVENTS = frozenset({"triple_play"})
_STATCAST_ZERO_OUT_EVENTS = frozenset(
    {
        "catcher_interf",
        "double",
        "field_error",
        "hit_by_pitch",
        "home_run",
        "intent_walk",
        "single",
        "triple",
        "walk",
    }
)

_STATCAST_NON_AT_BAT_EVENTS = frozenset(
    {
        "catcher_interf",
        "hit_by_pitch",
        "intent_walk",
        "sac_bunt",
        "sac_bunt_double_play",
        "sac_fly",
        "sac_fly_double_play",
        "walk",
    }
)
_STATCAST_SUPPORTED_PLATE_APPEARANCE_EVENTS = frozenset(
    {
        *_STATCAST_HIT_EVENTS,
        *_STATCAST_WALK_EVENTS,
        *_STATCAST_STRIKEOUT_EVENTS,
        *_STATCAST_ZERO_OUT_EVENTS,
        *_STATCAST_ONE_OUT_EVENTS,
        *_STATCAST_TWO_OUT_EVENTS,
        *_STATCAST_THREE_OUT_EVENTS,
    }
)


def _terminal_event_outs(event: str | None) -> int | None:
    if event in _STATCAST_ZERO_OUT_EVENTS:
        return 0
    if event in _STATCAST_ONE_OUT_EVENTS:
        return 1
    if event in _STATCAST_TWO_OUT_EVENTS:
        return 2
    if event in _STATCAST_THREE_OUT_EVENTS:
        return 3
    return None


def _statcast_outs_between(
    current: StatcastPitcherPitch,
    next_plate_appearance: StatcastPitcherPitch | None,
) -> int | None:
    if current.outs_when_up is None:
        return None
    if next_plate_appearance is None:
        return _terminal_event_outs(current.event)
    same_half = (
        current.inning == next_plate_appearance.inning
        and current.inning_half == next_plate_appearance.inning_half
    )
    if same_half:
        if next_plate_appearance.outs_when_up is None:
            return None
        difference = next_plate_appearance.outs_when_up - current.outs_when_up
        return difference if 0 <= difference <= 3 else None
    remaining = 3 - current.outs_when_up
    return remaining if 1 <= remaining <= 3 else None


def derive_statcast_pitcher_appearances(
    pitches: Iterable[StatcastPitcherPitch],
) -> tuple[StatcastPitcherAppearance, ...]:
    """Derive final-game appearances while leaving non-provable outs unknown.

    Duplicate pitch identities and inconsistent plate-appearance identity fail closed.
    Outs use Statcast's explicit out-state progression or a terminal event with defined
    out semantics. Mixed-pitcher plate appearances are retained as appearances but
    their outs are unknown because attribution cannot be proven from this contract.
    """

    final_rows = [pitch for pitch in pitches if pitch.game_is_final]
    by_identity: dict[tuple[int, int, int], StatcastPitcherPitch] = {}
    for pitch in final_rows:
        identity = (pitch.game_pk, pitch.at_bat_number, pitch.pitch_number)
        if identity in by_identity:
            raise ValueError("duplicate Statcast pitch identity in feature input")
        by_identity[identity] = pitch
    rows = sorted(
        by_identity.values(),
        key=lambda pitch: (
            pitch.game_date,
            pitch.game_pk,
            pitch.at_bat_number,
            pitch.pitch_number,
        ),
    )
    if not rows:
        return ()

    appearance_rows: dict[
        tuple[int, date, str, str], list[StatcastPitcherPitch]
    ] = defaultdict(list)
    plate_appearances: dict[
        tuple[int, int], list[StatcastPitcherPitch]
    ] = defaultdict(list)
    for pitch in rows:
        appearance_rows[
            (pitch.game_pk, pitch.game_date, pitch.pitcher_id, pitch.team_key)
        ].append(pitch)
        plate_appearances[(pitch.game_pk, pitch.at_bat_number)].append(pitch)

    terminal_by_game: dict[int, list[StatcastPitcherPitch]] = defaultdict(list)
    ambiguous_appearances: set[tuple[int, date, str, str]] = set()
    for (game_pk, _), plate_rows in sorted(plate_appearances.items()):
        dates = {pitch.game_date for pitch in plate_rows}
        locations = {(pitch.inning, pitch.inning_half) for pitch in plate_rows}
        if len(dates) != 1 or len(locations) != 1:
            raise ValueError("Statcast plate-appearance identity is inconsistent")
        terminal = max(plate_rows, key=lambda pitch: pitch.pitch_number)
        terminal_by_game[game_pk].append(terminal)
        pitcher_keys = {
            (pitch.game_pk, pitch.game_date, pitch.pitcher_id, pitch.team_key)
            for pitch in plate_rows
        }
        if len(pitcher_keys) > 1:
            ambiguous_appearances.update(pitcher_keys)

    first_pitcher: dict[tuple[int, str], str] = {}
    for pitch in rows:
        first_pitcher.setdefault((pitch.game_pk, pitch.team_key), pitch.pitcher_id)

    terminal_stats: dict[tuple[int, date, str, str], Counter[str]] = defaultdict(
        Counter
    )
    out_contributions: dict[
        tuple[int, date, str, str], list[int | None]
    ] = defaultdict(list)
    for game_pk, game_terminals in sorted(terminal_by_game.items()):
        ordered = sorted(
            game_terminals,
            key=lambda pitch: (pitch.at_bat_number, pitch.pitch_number),
        )
        for index, terminal in enumerate(ordered):
            key = (
                terminal.game_pk,
                terminal.game_date,
                terminal.pitcher_id,
                terminal.team_key,
            )
            event = terminal.event
            if event and event != "truncated_pa":
                stats = terminal_stats[key]
                stats["batters_faced"] += 1
                if event in _STATCAST_HIT_EVENTS:
                    stats["hits"] += 1
                if event == "home_run":
                    stats["home_runs"] += 1
                if event in _STATCAST_WALK_EVENTS:
                    stats["walks"] += 1
                if event == "hit_by_pitch":
                    stats["hit_by_pitch"] += 1
                if event in _STATCAST_STRIKEOUT_EVENTS:
                    stats["strikeouts"] += 1
            next_plate_appearance = (
                ordered[index + 1] if index + 1 < len(ordered) else None
            )
            out_contributions[key].append(
                _statcast_outs_between(terminal, next_plate_appearance)
            )

    appearances: list[StatcastPitcherAppearance] = []
    for key, selected in sorted(appearance_rows.items()):
        game_pk, game_date, pitcher_id, team_key = key
        contributions = out_contributions.get(key, [])
        outs_known = (
            key not in ambiguous_appearances
            and bool(contributions)
            and all(value is not None for value in contributions)
        )
        outs_recorded = (
            sum(int(value) for value in contributions if value is not None)
            if outs_known
            else None
        )
        stats = terminal_stats[key]
        appearances.append(
            StatcastPitcherAppearance(
                game_pk=game_pk,
                game_date=game_date,
                pitcher_id=pitcher_id,
                team_key=team_key,
                is_start=first_pitcher[(game_pk, team_key)] == pitcher_id,
                pitches=len(selected),
                batters_faced=stats["batters_faced"],
                hits=stats["hits"],
                home_runs=stats["home_runs"],
                walks=stats["walks"],
                hit_by_pitch=stats["hit_by_pitch"],
                strikeouts=stats["strikeouts"],
                outs_recorded=outs_recorded,
                outs_recorded_status="known" if outs_known else "unknown",
                available_at=max(pitch.available_at for pitch in selected),
            )
        )
    return tuple(appearances)


def derive_statcast_plate_appearances(
    pitches: Iterable[StatcastPitcherPitch],
) -> tuple[StatcastPlateAppearance, ...]:
    """Derive terminal final-game observations without inventing attribution.

    A mid-plate-appearance pitching change is retained with an explicit ambiguous
    attribution state. Its result is not allowed into standard rate calculations.
    """

    by_identity: dict[tuple[int, int, int], StatcastPitcherPitch] = {}
    for pitch in pitches:
        if not pitch.game_is_final:
            continue
        identity = (pitch.game_pk, pitch.at_bat_number, pitch.pitch_number)
        if identity in by_identity:
            raise ValueError("duplicate Statcast pitch identity in feature input")
        by_identity[identity] = pitch
    plate_rows: dict[tuple[int, int], list[StatcastPitcherPitch]] = defaultdict(list)
    for pitch in by_identity.values():
        plate_rows[(pitch.game_pk, pitch.at_bat_number)].append(pitch)

    terminals_by_game: dict[
        int, list[tuple[StatcastPitcherPitch, str]]
    ] = defaultdict(list)
    for _, plate_pitches in sorted(plate_rows.items()):
        dates = {pitch.game_date for pitch in plate_pitches}
        locations = {(pitch.inning, pitch.inning_half) for pitch in plate_pitches}
        batter_ids = {pitch.batter_id for pitch in plate_pitches}
        if len(dates) != 1 or len(locations) != 1:
            raise ValueError("Statcast plate-appearance identity is inconsistent")
        terminal = max(plate_pitches, key=lambda pitch: pitch.pitch_number)
        pitcher_changed = (
            len({(pitch.pitcher_id, pitch.team_key) for pitch in plate_pitches}) != 1
        )
        batter_changed = len(batter_ids) != 1
        # Statcast can retain a pinch-hitter substitution under one at-bat number.
        # The terminal event is preserved, but neither hitter nor pitcher rate
        # attribution is provable for an in-progress substitution.
        attribution = (
            "ambiguous_pitcher_change"
            if pitcher_changed
            else "ambiguous_batter_change"
            if batter_changed
            else "supported"
        )
        terminals_by_game[terminal.game_pk].append((terminal, attribution))

    result: list[StatcastPlateAppearance] = []
    for game_pk, game_terminals in sorted(terminals_by_game.items()):
        ordered = sorted(
            game_terminals,
            key=lambda item: (item[0].at_bat_number, item[0].pitch_number),
        )
        for index, (terminal, attribution) in enumerate(ordered):
            if terminal.event == "truncated_pa":
                continue
            next_terminal = ordered[index + 1][0] if index + 1 < len(ordered) else None
            progression_outs = (
                _statcast_outs_between(terminal, next_terminal)
                if attribution == "supported"
                else None
            )
            event_outs = _terminal_event_outs(terminal.event)
            # A state transition can include a caught stealing or another
            # non-PA out. Such an out is valid for appearance workload, but it
            # cannot be assigned to this batter/handedness split.
            outs = (
                event_outs
                if progression_outs is not None and progression_outs == event_outs
                else None
            )
            result.append(
                StatcastPlateAppearance(
                    game_pk=game_pk,
                    game_date=terminal.game_date,
                    batter_id=terminal.batter_id,
                    pitcher_id=terminal.pitcher_id,
                    batter_team_key=terminal.batter_team_key,
                    pitcher_team_key=terminal.team_key,
                    batter_is_home=terminal.batter_is_home,
                    batter_hand=terminal.batter_hand,
                    pitcher_hand=terminal.pitcher_hand,
                    event=terminal.event,
                    outs_recorded=outs,
                    outs_recorded_status="known" if outs is not None else "unknown",
                    attribution_status=attribution,
                    plate_appearance_classification=(
                        terminal.plate_appearance_classification
                    ),
                    available_at=max(pitch.available_at for pitch in plate_rows[(game_pk, terminal.at_bat_number)]),
                )
            )
    return tuple(result)


_HITTER_RATE_FIELDS = (
    "avg",
    "obp",
    "slg",
    "ops",
    "iso",
    "babip",
    "k_rate",
    "bb_rate",
)
_PITCHER_OUT_RATE_FIELDS = ("whip", "k_per_9", "bb_per_9", "hr_per_9")


def _recognized_plate_appearances(
    rows: Sequence[StatcastPlateAppearance],
) -> tuple[list[StatcastPlateAppearance], list[StatcastPlateAppearance]]:
    recognized: list[StatcastPlateAppearance] = []
    unsupported: list[StatcastPlateAppearance] = []
    for row in rows:
        if (
            row.event in _STATCAST_SUPPORTED_PLATE_APPEARANCE_EVENTS
            and row.attribution_status == "supported"
        ):
            recognized.append(row)
        else:
            unsupported.append(row)
    return recognized, unsupported


def statcast_hitter_standard_stats(
    rows: Sequence[StatcastPlateAppearance],
) -> dict[str, Any]:
    recognized, unsupported = _recognized_plate_appearances(rows)
    lines = [
        BattingGameLine(
            row.game_date,
            row.batter_id or "UNRESOLVED",
            row.batter_team_key or "UNRESOLVED",
            pa=1,
            ab=(
                0
                if row.event in _STATCAST_NON_AT_BAT_EVENTS
                or row.plate_appearance_classification
                == DEFENSIVE_SHIFT_VIOLATION_NON_AT_BAT
                else 1
            ),
            hits=1 if row.event in _STATCAST_HIT_EVENTS else 0,
            doubles=1 if row.event == "double" else 0,
            triples=1 if row.event == "triple" else 0,
            home_runs=1 if row.event == "home_run" else 0,
            walks=1 if row.event in _STATCAST_WALK_EVENTS else 0,
            hit_by_pitch=1 if row.event == "hit_by_pitch" else 0,
            strikeouts=1 if row.event in _STATCAST_STRIKEOUT_EVENTS else 0,
            sacrifice_flies=1 if row.event in {"sac_fly", "sac_fly_double_play"} else 0,
            available_at=row.available_at,
        )
        for row in recognized
    ]
    calculated = hitter_rate_stats(lines)
    unavailable_fields: list[str] = []
    if unsupported:
        unavailable_fields.extend(_HITTER_RATE_FIELDS)
        for field_name in _HITTER_RATE_FIELDS:
            calculated[field_name] = None
    if not rows:
        calculated = {key: None for key in calculated}
        unavailable_fields = list(_HITTER_RATE_FIELDS)
    return {
        **calculated,
        "source": "statcast_terminal_completed_plate_appearance",
        "data_status": (
            "unavailable" if not rows else "partial" if unsupported else "available"
        ),
        "observation_count": len(rows),
        "recognized_plate_appearance_count": len(recognized),
        "unsupported_observation_count": len(unsupported),
        "unsupported_events": sorted({row.event or "missing_event" for row in unsupported}),
        "unavailable_fields": unavailable_fields,
    }


def statcast_pitcher_standard_stats(
    rows: Sequence[StatcastPlateAppearance],
) -> dict[str, Any]:
    recognized, unsupported = _recognized_plate_appearances(rows)
    totals: dict[str, int | float | None] = {
        "bf": len(recognized),
        "outs_recorded": None,
        "h": sum(row.event in _STATCAST_HIT_EVENTS for row in recognized),
        "er": None,
        "hr": sum(row.event == "home_run" for row in recognized),
        "bb": sum(row.event in _STATCAST_WALK_EVENTS for row in recognized),
        "hbp": sum(row.event == "hit_by_pitch" for row in recognized),
        "so": sum(row.event in _STATCAST_STRIKEOUT_EVENTS for row in recognized),
        "innings_pitched": None,
        "era": None,
        "whip": None,
        "k_rate": None,
        "bb_rate": None,
        "k_minus_bb_rate": None,
        "k_per_9": None,
        "bb_per_9": None,
        "hr_per_9": None,
    }
    unavailable_fields = ["era"]
    if not unsupported and recognized:
        k_rate = _ratio(float(totals["so"] or 0), len(recognized))
        bb_rate = _ratio(float(totals["bb"] or 0), len(recognized))
        totals["k_rate"] = k_rate
        totals["bb_rate"] = bb_rate
        totals["k_minus_bb_rate"] = (
            None if k_rate is None or bb_rate is None else k_rate - bb_rate
        )
        if all(row.outs_recorded_status == "known" for row in recognized):
            outs = sum(int(row.outs_recorded or 0) for row in recognized)
            innings = outs / 3.0
            totals["outs_recorded"] = outs
            totals["innings_pitched"] = innings
            totals["whip"] = _ratio(float((totals["bb"] or 0) + (totals["h"] or 0)), innings)
            totals["k_per_9"] = _ratio(9.0 * float(totals["so"] or 0), innings)
            totals["bb_per_9"] = _ratio(9.0 * float(totals["bb"] or 0), innings)
            totals["hr_per_9"] = _ratio(9.0 * float(totals["hr"] or 0), innings)
        else:
            unavailable_fields.extend(_PITCHER_OUT_RATE_FIELDS)
    elif rows:
        unavailable_fields.extend(
            ("k_rate", "bb_rate", "k_minus_bb_rate", *_PITCHER_OUT_RATE_FIELDS)
        )
    else:
        totals = {key: None for key in totals}
        unavailable_fields.extend(
            ("k_rate", "bb_rate", "k_minus_bb_rate", *_PITCHER_OUT_RATE_FIELDS)
        )
    return {
        **totals,
        "source": "statcast_terminal_completed_plate_appearance",
        "data_status": "unavailable" if not rows else "partial",
        "observation_count": len(rows),
        "recognized_plate_appearance_count": len(recognized),
        "unsupported_observation_count": len(unsupported),
        "unsupported_events": sorted({row.event or "missing_event" for row in unsupported}),
        "unavailable_fields": sorted(set(unavailable_fields)),
    }


def pitch_trait_stats(rows: Sequence[StatcastPitchMetric]) -> dict[str, Any]:
    counts = Counter(row.pitch_type for row in rows if row.pitch_type)
    total = len(rows)
    by_type: dict[str, dict[str, int | float | None]] = {}
    for pitch_type in sorted(counts):
        selected = [row for row in rows if row.pitch_type == pitch_type]
        by_type[pitch_type] = {
            "count": len(selected),
            "usage_rate": _ratio(len(selected), total),
            "velocity": _finite_mean(row.release_speed for row in selected),
            "spin": _finite_mean(row.release_spin_rate for row in selected),
            "extension": _finite_mean(row.release_extension for row in selected),
            "horizontal_movement": _finite_mean(row.pfx_x for row in selected),
            "vertical_movement": _finite_mean(row.pfx_z for row in selected),
        }
    swings = [row for row in rows if (row.description or "") in _SWING_DESCRIPTIONS]
    whiffs = [row for row in rows if (row.description or "") in _WHIFF_DESCRIPTIONS]
    contacts = [row for row in rows if (row.description or "") in _CONTACT_DESCRIPTIONS]
    out_zone = [row for row in rows if row.zone is not None and row.zone not in range(1, 10)]
    chase_swings = [row for row in out_zone if (row.description or "") in _SWING_DESCRIPTIONS]
    strikes = [row for row in rows if (row.description or "") in _STRIKE_DESCRIPTIONS]
    return {
        "pitch_count": total,
        "pitch_types": by_type,
        "strike_rate": _ratio(len(strikes), total),
        "whiff_rate": _ratio(len(whiffs), len(swings)),
        "chase_rate": _ratio(len(chase_swings), len(out_zone)),
        "contact_rate": _ratio(len(contacts), len(swings)),
    }


def _known_by(row: object, knowledge_cutoff: datetime) -> bool:
    available_at = getattr(row, "available_at", None)
    if not isinstance(available_at, datetime):
        raise TypeError("feature input records require an available_at timestamp")
    return available_at <= knowledge_cutoff


def _windowed[T](
    rows: Iterable[T],
    feature_as_of: date,
    days: int | None,
    knowledge_cutoff: datetime,
) -> list[T]:
    lower = date(feature_as_of.year, 1, 1) if days is None else feature_as_of - timedelta(days=days)
    return [
        row
        for row in rows
        if lower <= getattr(row, "game_date") < feature_as_of
        and _known_by(row, knowledge_cutoff)
    ]


def _batted_ball_windows(
    rows: Sequence[StatcastBattedBall],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window in (None, *ROLLING_WINDOWS):
        name = "season_to_date" if window is None else f"rolling_{window}_days"
        result[name] = batted_ball_stats(
            _windowed(rows, feature_as_of, window, knowledge_cutoff)
        )
    return result


def swing_metric_stats(
    rows: Sequence[StatcastSwingMetric],
) -> dict[str, int | float | None]:
    """Summarize retained swing observations without imputing missing values."""

    return {
        "swing_observation_count": len(rows),
        "bat_speed_samples": _finite_count(row.bat_speed for row in rows),
        "bat_speed_mean": _finite_mean(row.bat_speed for row in rows),
        "swing_length_samples": _finite_count(row.swing_length for row in rows),
        "swing_length_mean": _finite_mean(
            row.swing_length for row in rows
        ),
        "attack_angle_samples": _finite_count(row.attack_angle for row in rows),
        "attack_angle_mean": _finite_mean(
            row.attack_angle for row in rows
        ),
        "attack_direction_samples": _finite_count(row.attack_direction for row in rows),
        "attack_direction_mean": _finite_mean(
            row.attack_direction for row in rows
        ),
        "swing_path_tilt_samples": _finite_count(row.swing_path_tilt for row in rows),
        "swing_path_tilt_mean": _finite_mean(
            row.swing_path_tilt for row in rows
        ),
        "miss_distance_samples": _finite_count(row.miss_distance for row in rows),
        "miss_distance_mean": _finite_mean(
            row.miss_distance for row in rows
        ),
        "hyper_speed_samples": _finite_count(row.hyper_speed for row in rows),
        "hyper_speed_mean": _finite_mean(
            row.hyper_speed for row in rows
        ),
    }


def _swing_metric_windows(
    rows: Sequence[StatcastSwingMetric],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window in (None, *ROLLING_WINDOWS):
        name = (
            "season_to_date"
            if window is None
            else f"rolling_{window}_days"
        )
        result[name] = swing_metric_stats(
            _windowed(
                rows,
                feature_as_of,
                window,
                knowledge_cutoff,
            )
        )
    return result


def _circular_mean_degrees(
    values: Iterable[float | int | None],
) -> float | None:
    """Return the circular mean of finite degree observations."""

    finite = [
        float(value) % 360.0
        for value in values
        if value is not None and math.isfinite(float(value))
    ]
    if not finite:
        return None

    x = fmean(math.cos(math.radians(value)) for value in finite)
    y = fmean(math.sin(math.radians(value)) for value in finite)

    if math.isclose(x, 0.0, abs_tol=1e-12) and math.isclose(
        y,
        0.0,
        abs_tol=1e-12,
    ):
        return None

    angle = math.degrees(math.atan2(y, x)) % 360.0
    if math.isclose(angle, 360.0, abs_tol=1e-12):
        return 0.0
    return angle


def pitch_physics_stats(
    rows: Sequence[StatcastPitchMetric],
) -> dict[str, int | float | None]:
    """Summarize physical pitch observations retained by Statcast V3."""

    return {
        "pitch_observation_count": len(rows),
        "effective_speed_samples": _finite_count(row.effective_speed for row in rows),
        "effective_speed_mean": _finite_mean(
            row.effective_speed for row in rows
        ),
        "spin_axis_samples": _finite_count(row.spin_axis for row in rows),
        "spin_axis_circular_mean": _circular_mean_degrees(
            row.spin_axis for row in rows
        ),
        "release_pos_x_samples": _finite_count(row.release_pos_x for row in rows),
        "release_pos_x_mean": _finite_mean(
            row.release_pos_x for row in rows
        ),
        "release_pos_y_samples": _finite_count(row.release_pos_y for row in rows),
        "release_pos_y_mean": _finite_mean(
            row.release_pos_y for row in rows
        ),
        "release_pos_z_samples": _finite_count(row.release_pos_z for row in rows),
        "release_pos_z_mean": _finite_mean(
            row.release_pos_z for row in rows
        ),
        "arm_angle_samples": _finite_count(row.arm_angle for row in rows),
        "arm_angle_mean": _finite_mean(
            row.arm_angle for row in rows
        ),
        "api_break_x_arm_samples": _finite_count(row.api_break_x_arm for row in rows),
        "api_break_x_arm_mean": _finite_mean(
            row.api_break_x_arm for row in rows
        ),
        "api_break_x_batter_in_samples": _finite_count(row.api_break_x_batter_in for row in rows),
        "api_break_x_batter_in_mean": _finite_mean(
            row.api_break_x_batter_in for row in rows
        ),
        "api_break_z_with_gravity_samples": _finite_count(row.api_break_z_with_gravity for row in rows),
        "api_break_z_with_gravity_mean": _finite_mean(
            row.api_break_z_with_gravity for row in rows
        ),
    }


def _pitch_physics_windows(
    rows: Sequence[StatcastPitchMetric],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window in (None, *ROLLING_WINDOWS):
        name = (
            "season_to_date"
            if window is None
            else f"rolling_{window}_days"
        )
        result[name] = pitch_physics_stats(
            _windowed(
                rows,
                feature_as_of,
                window,
                knowledge_cutoff,
            )
        )
    return result


def _pitch_trait_windows(
    rows: Sequence[StatcastPitchMetric],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window in (None, *ROLLING_WINDOWS):
        name = "season_to_date" if window is None else f"rolling_{window}_days"
        result[name] = pitch_trait_stats(
            _windowed(rows, feature_as_of, window, knowledge_cutoff)
        )
    return result


def _window_bundle(windows: dict[str, Any]) -> dict[str, Any]:
    return {
        **windows["season_to_date"],
        "windows": windows,
    }


def _handedness_bucket(value: str | None) -> str:
    return {
        "L": "left",
        "R": "right",
        "S": "switch",
    }.get(value or "", "unknown")


def _batted_ball_splits(
    rows: Sequence[StatcastBattedBall],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    dimensions: tuple[
        tuple[str, tuple[str, ...], Any], ...
    ] = (
        (
            "home_away",
            ("home", "away", "unknown"),
            lambda row: (
                "home" if row.is_home is True else "away" if row.is_home is False else "unknown"
            ),
        ),
        (
            "batter_handedness",
            ("left", "right", "switch", "unknown"),
            lambda row: _handedness_bucket(row.batter_hand),
        ),
        (
            "opposing_pitcher_handedness",
            ("left", "right", "switch", "unknown"),
            lambda row: _handedness_bucket(row.opposing_pitcher_hand),
        ),
    )
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, buckets, selector in dimensions:
        result[dimension] = {}
        for bucket in buckets:
            windows = _batted_ball_windows(
                [row for row in rows if selector(row) == bucket],
                feature_as_of=feature_as_of,
                knowledge_cutoff=knowledge_cutoff,
            )
            result[dimension][bucket] = _window_bundle(windows)
    return result


def _pitch_trait_splits(
    rows: Sequence[StatcastPitchMetric],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {
        "opposing_batter_handedness": {}
    }
    for bucket in ("left", "right", "switch", "unknown"):
        windows = _pitch_trait_windows(
            [
                row
                for row in rows
                if _handedness_bucket(row.opposing_batter_hand) == bucket
            ],
            feature_as_of=feature_as_of,
            knowledge_cutoff=knowledge_cutoff,
        )
        result["opposing_batter_handedness"][bucket] = _window_bundle(windows)
    return result


def _standard_split_windows(
    rows: Sequence[StatcastPlateAppearance],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    calculator: Any,
) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    for window in (None, *ROLLING_WINDOWS):
        name = "season_to_date" if window is None else f"rolling_{window}_days"
        windows[name] = calculator(
            _windowed(rows, feature_as_of, window, knowledge_cutoff)
        )
    return _window_bundle(windows)


def _hitter_standard_splits(
    rows: Sequence[StatcastPlateAppearance],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    dimensions: tuple[tuple[str, tuple[str, ...], Any], ...] = (
        (
            "home_away",
            ("home", "away", "unknown"),
            lambda row: (
                "home"
                if row.batter_is_home is True
                else "away"
                if row.batter_is_home is False
                else "unknown"
            ),
        ),
        (
            "opposing_pitcher_handedness",
            ("left", "right", "switch", "unknown"),
            lambda row: _handedness_bucket(row.pitcher_hand),
        ),
    )
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, buckets, selector in dimensions:
        result[dimension] = {}
        for bucket in buckets:
            result[dimension][bucket] = _standard_split_windows(
                [row for row in rows if selector(row) == bucket],
                feature_as_of=feature_as_of,
                knowledge_cutoff=knowledge_cutoff,
                calculator=statcast_hitter_standard_stats,
            )
    return result


def _pitcher_standard_splits(
    rows: Sequence[StatcastPlateAppearance],
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
) -> dict[str, dict[str, dict[str, Any]]]:
    dimensions: tuple[tuple[str, tuple[str, ...], Any], ...] = (
        (
            "home_away",
            ("home", "away", "unknown"),
            lambda row: (
                "away"
                if row.batter_is_home is True
                else "home"
                if row.batter_is_home is False
                else "unknown"
            ),
        ),
        (
            "opposing_batter_handedness",
            ("left", "right", "switch", "unknown"),
            lambda row: _handedness_bucket(row.batter_hand),
        ),
    )
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for dimension, buckets, selector in dimensions:
        result[dimension] = {}
        for bucket in buckets:
            result[dimension][bucket] = _standard_split_windows(
                [row for row in rows if selector(row) == bucket],
                feature_as_of=feature_as_of,
                knowledge_cutoff=knowledge_cutoff,
                calculator=statcast_pitcher_standard_stats,
            )
    return result


def _pitcher_history(
    *,
    player_id: str,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    pitching_lines: Sequence[PitchingGameLine],
    pitcher_appearances: Sequence[StatcastPitcherAppearance],
) -> tuple[
    list[PitchingGameLine | StatcastPitcherAppearance],
    dict[str, Any],
    str,
]:
    statcast_history = sorted(
        (
            row
            for row in pitcher_appearances
            if row.pitcher_id == player_id
            and row.game_date < feature_as_of
            and _known_by(row, knowledge_cutoff)
        ),
        key=lambda row: (row.game_date, row.game_pk),
    )
    line_history = sorted(
        (
            row
            for row in pitching_lines
            if row.player_id == player_id
            and row.game_date < feature_as_of
            and _known_by(row, knowledge_cutoff)
        ),
        key=lambda row: row.game_date,
    )
    history: list[PitchingGameLine | StatcastPitcherAppearance]
    if statcast_history:
        history = list(statcast_history)
        source = "statcast_final_game_appearances"
    elif line_history:
        history = list(line_history)
        source = "pitching_game_lines"
    else:
        history = []
        source = "unavailable"

    starts = [row for row in history if row.is_start]
    previous_appearance = history[-1] if history else None
    previous_start = starts[-1] if starts else None
    recent: dict[str, dict[str, int]] = {}
    for days in (1, 3, 7):
        selected = _windowed(history, feature_as_of, days, knowledge_cutoff)
        recent[f"previous_{days}_days"] = {
            "appearance_count": len(selected),
            "start_count": sum(row.is_start for row in selected),
            "relief_appearance_count": sum(not row.is_start for row in selected),
            "pitch_count": sum(row.pitches for row in selected),
        }
    workload = {
        "source": source,
        "previous_appearance": (
            asdict(previous_appearance) if previous_appearance is not None else None
        ),
        "days_since_previous_appearance": (
            (feature_as_of - previous_appearance.game_date).days
            if previous_appearance is not None
            else None
        ),
        "days_since_previous_start": (
            (feature_as_of - previous_start.game_date).days
            if previous_start is not None
            else None
        ),
        "recent": recent,
    }
    return starts, workload, source


def build_player_feature_payloads(
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    batting_lines: Iterable[BattingGameLine],
    pitching_lines: Iterable[PitchingGameLine],
    batted_balls: Iterable[StatcastBattedBall] = (),
    pitch_metrics: Iterable[StatcastPitchMetric] = (),
    pitcher_appearances: Iterable[StatcastPitcherAppearance] = (),
    statcast_plate_appearances: Iterable[StatcastPlateAppearance] = (),
) -> dict[str, dict[str, Any]]:
    cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
    batting = [row for row in batting_lines if _known_by(row, cutoff)]
    pitching = [row for row in pitching_lines if _known_by(row, cutoff)]
    balls = [row for row in batted_balls if _known_by(row, cutoff)]
    pitches = [row for row in pitch_metrics if _known_by(row, cutoff)]
    appearances = [
        row for row in pitcher_appearances if _known_by(row, cutoff)
    ]
    plate_appearances = [
        row for row in statcast_plate_appearances if _known_by(row, cutoff)
    ]
    players = sorted(
        {row.player_id for row in batting}
        | {row.player_id for row in pitching}
        | {row.batter_id for row in balls}
        | {row.pitcher_id for row in pitches}
        | {row.pitcher_id for row in appearances}
        | {
            row.batter_id
            for row in plate_appearances
            if row.batter_id is not None
        }
        | {row.pitcher_id for row in plate_appearances}
    )
    result: dict[str, dict[str, Any]] = {}
    for player_id in players:
        player_batting = [row for row in batting if row.player_id == player_id]
        player_pitching = [row for row in pitching if row.player_id == player_id]
        hitter_windows: dict[str, Any] = {}
        pitcher_windows: dict[str, Any] = {}
        for window in (None, *ROLLING_WINDOWS):
            name = "season_to_date" if window is None else f"rolling_{window}_days"
            hitter_windows[name] = _window_payload(
                _windowed(player_batting, feature_as_of, window, cutoff),
                hitter_rate_stats,
            )
            pitcher_windows[name] = _window_payload(
                _windowed(player_pitching, feature_as_of, window, cutoff),
                pitcher_rate_stats,
            )
        starts, pitcher_workload, start_source = _pitcher_history(
            player_id=player_id,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
            pitching_lines=player_pitching,
            pitcher_appearances=appearances,
        )
        current_balls = [row for row in balls if row.batter_id == player_id]
        current_pitches = [row for row in pitches if row.pitcher_id == player_id]
        hitter_plate_appearances = [
            row for row in plate_appearances if row.batter_id == player_id
        ]
        pitcher_plate_appearances = [
            row for row in plate_appearances if row.pitcher_id == player_id
        ]
        hitter_windows["statcast_standard_splits"] = _hitter_standard_splits(
            hitter_plate_appearances,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        pitcher_windows["statcast_standard_splits"] = _pitcher_standard_splits(
            pitcher_plate_appearances,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        batted_windows = _batted_ball_windows(
            current_balls,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        pitch_windows = _pitch_trait_windows(
            current_pitches,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        payload = {
            "contract_version": FEATURE_VERSION,
            "feature_as_of": feature_as_of.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "player_id": player_id,
            "hitting": hitter_windows,
            "batted_ball": {
                **_window_bundle(batted_windows),
                "splits": _batted_ball_splits(
                    current_balls,
                    feature_as_of=feature_as_of,
                    knowledge_cutoff=cutoff,
                ),
            },
            "pitching": pitcher_windows,
            "pitch_traits": {
                **_window_bundle(pitch_windows),
                "splits": _pitch_trait_splits(
                    current_pitches,
                    feature_as_of=feature_as_of,
                    knowledge_cutoff=cutoff,
                ),
            },
            "previous_start": asdict(starts[-1]) if starts else None,
            "previous_three_starts": [asdict(row) for row in starts[-3:]],
            "pitcher_appearance_history_source": start_source,
            "pitcher_workload": pitcher_workload,
        }
        payload["feature_checksum"] = _checksum(payload)
        result[player_id] = payload
    return result


def build_aggregate_player_feature_payloads(
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    batting_aggregates: Iterable[BattingAggregateLine],
    pitching_aggregates: Iterable[PitchingAggregateLine],
    batted_balls: Iterable[StatcastBattedBall] = (),
    pitch_metrics: Iterable[StatcastPitchMetric] = (),
    pitcher_appearances: Iterable[StatcastPitcherAppearance] = (),
    statcast_plate_appearances: Iterable[StatcastPlateAppearance] = (),
) -> dict[str, dict[str, Any]]:
    """Build features from explicit provider windows without relabeling partial data.

    Aggregate rows are accepted only when their through date is strictly earlier than
    ``feature_as_of``. A duplicate player/window observation is rejected because the
    caller must reconcile provider revisions before producing a sealed feature record.
    """

    cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
    batting_candidates: defaultdict[
        tuple[str, str], list[BattingAggregateLine]
    ] = defaultdict(list)
    pitching_candidates: defaultdict[
        tuple[str, str], list[PitchingAggregateLine]
    ] = defaultdict(list)
    for batting_row in batting_aggregates:
        if batting_row.through_date >= feature_as_of:
            raise ValueError("batting aggregate crosses the D-1 feature boundary")
        if not _known_by(batting_row, cutoff):
            continue
        key = (batting_row.player_id, batting_row.window_key)
        batting_candidates[key].append(batting_row)
    for pitching_row in pitching_aggregates:
        if pitching_row.through_date >= feature_as_of:
            raise ValueError("pitching aggregate crosses the D-1 feature boundary")
        if not _known_by(pitching_row, cutoff):
            continue
        key = (pitching_row.player_id, pitching_row.window_key)
        pitching_candidates[key].append(pitching_row)

    def is_total(source_team_id: str | None) -> bool:
        value = (source_team_id or "").strip().upper()
        return value == "TOT" or (
            len(value) >= 3 and value[:-2].isdigit() and value.endswith("TM")
        )

    def select_batting(
        rows: list[BattingAggregateLine], key: tuple[str, str]
    ) -> BattingAggregateLine:
        if len(rows) == 1:
            return rows[0]
        identities = [(row.source_team_id, row.source_stint_key) for row in rows]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate batting aggregate player/window/team stint")
        totals = [row for row in rows if is_total(row.source_team_id)]
        if len(totals) != 1:
            raise ValueError(
                f"ambiguous batting aggregate player/window/team stints: {key}"
            )
        return totals[0]

    def select_pitching(
        rows: list[PitchingAggregateLine], key: tuple[str, str]
    ) -> PitchingAggregateLine:
        if len(rows) == 1:
            return rows[0]
        identities = [(row.source_team_id, row.source_stint_key) for row in rows]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate pitching aggregate player/window/team stint")
        totals = [row for row in rows if is_total(row.source_team_id)]
        if len(totals) != 1:
            raise ValueError(
                f"ambiguous pitching aggregate player/window/team stints: {key}"
            )
        return totals[0]

    batting_by_key = {
        key: select_batting(rows, key)
        for key, rows in sorted(batting_candidates.items())
    }
    pitching_by_key = {
        key: select_pitching(rows, key)
        for key, rows in sorted(pitching_candidates.items())
    }

    balls = [row for row in batted_balls if _known_by(row, cutoff)]
    pitches = [row for row in pitch_metrics if _known_by(row, cutoff)]
    appearances = [
        row for row in pitcher_appearances if _known_by(row, cutoff)
    ]
    plate_appearances = [
        row for row in statcast_plate_appearances if _known_by(row, cutoff)
    ]
    # Current-season materialization may include every retained pitch. Build
    # deterministic player indexes once rather than rescanning that full input
    # for each player; eligibility and feature calculations remain unchanged.
    balls_by_batter: defaultdict[str, list[StatcastBattedBall]] = defaultdict(list)
    pitches_by_pitcher: defaultdict[str, list[StatcastPitchMetric]] = defaultdict(
        list
    )
    plate_appearances_by_batter: defaultdict[
        str, list[StatcastPlateAppearance]
    ] = defaultdict(list)
    plate_appearances_by_pitcher: defaultdict[
        str, list[StatcastPlateAppearance]
    ] = defaultdict(list)
    appearances_by_pitcher: defaultdict[
        str, list[StatcastPitcherAppearance]
    ] = defaultdict(list)
    for ball in balls:
        balls_by_batter[ball.batter_id].append(ball)
    for metric in pitches:
        pitches_by_pitcher[metric.pitcher_id].append(metric)
    for plate_appearance in plate_appearances:
        if plate_appearance.batter_id is not None:
            plate_appearances_by_batter[plate_appearance.batter_id].append(
                plate_appearance
            )
        plate_appearances_by_pitcher[plate_appearance.pitcher_id].append(
            plate_appearance
        )
    for appearance in appearances:
        appearances_by_pitcher[appearance.pitcher_id].append(appearance)
    for ball_rows in balls_by_batter.values():
        ball_rows.sort(
            key=lambda row: (
                row.game_date,
                row.available_at,
                row.launch_speed is None,
                row.launch_speed or 0.0,
                row.launch_angle is None,
                row.launch_angle or 0.0,
                row.launch_speed_angle is None,
                row.launch_speed_angle or 0,
            )
        )
    for metric_rows in pitches_by_pitcher.values():
        metric_rows.sort(
            key=lambda row: (
                row.game_date,
                row.available_at,
                row.team_key,
                row.pitch_type or "",
                row.release_speed is None,
                row.release_speed or 0.0,
                row.release_spin_rate is None,
                row.release_spin_rate or 0.0,
            )
        )
    for batter_plate_rows in plate_appearances_by_batter.values():
        batter_plate_rows.sort(
            key=lambda row: (row.game_date, row.game_pk, row.available_at)
        )
    for pitcher_plate_rows in plate_appearances_by_pitcher.values():
        pitcher_plate_rows.sort(
            key=lambda row: (row.game_date, row.game_pk, row.available_at)
        )
    for appearance_rows in appearances_by_pitcher.values():
        appearance_rows.sort(
            key=lambda row: (row.game_date, row.game_pk, row.available_at)
        )
    players = sorted(
        {key[0] for key in batting_by_key}
        | {key[0] for key in pitching_by_key}
        | {row.batter_id for row in balls}
        | {row.pitcher_id for row in pitches}
        | {row.pitcher_id for row in appearances}
        | {
            row.batter_id
            for row in plate_appearances
            if row.batter_id is not None
        }
        | {row.pitcher_id for row in plate_appearances}
    )
    result: dict[str, dict[str, Any]] = {}
    for player_id in players:
        hitting: dict[str, Any] = {}
        pitching: dict[str, Any] = {}
        for window_key in sorted(AGGREGATE_WINDOW_KEYS):
            batting_value = batting_by_key.get((player_id, window_key))
            pitching_value = pitching_by_key.get((player_id, window_key))
            hitting[window_key] = (
                _window_payload(
                    [batting_value.as_game_line()], hitter_rate_stats
                )
                if batting_value is not None
                else _window_payload([], hitter_rate_stats)
            )
            if batting_value is not None:
                hitting[window_key]["source_team_id"] = batting_value.source_team_id
                hitting[window_key]["source_stint_key"] = batting_value.source_stint_key
            pitching[window_key] = (
                _window_payload(
                    [pitching_value.as_game_line()], pitcher_rate_stats
                )
                if pitching_value is not None
                else _window_payload([], pitcher_rate_stats)
            )
            if pitching_value is not None:
                pitching[window_key]["source_team_id"] = pitching_value.source_team_id
                pitching[window_key]["source_stint_key"] = pitching_value.source_stint_key
        player_balls = balls_by_batter.get(player_id, [])
        player_pitches = pitches_by_pitcher.get(player_id, [])
        hitter_plate_appearances = plate_appearances_by_batter.get(player_id, [])
        pitcher_plate_appearances = plate_appearances_by_pitcher.get(player_id, [])
        hitting["statcast_standard_splits"] = _hitter_standard_splits(
            hitter_plate_appearances,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        pitching["statcast_standard_splits"] = _pitcher_standard_splits(
            pitcher_plate_appearances,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        batted_windows = _batted_ball_windows(
            player_balls,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        pitch_windows = _pitch_trait_windows(
            player_pitches,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        statcast_starts, pitcher_workload, history_source = _pitcher_history(
            player_id=player_id,
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
            pitching_lines=(),
            pitcher_appearances=appearances_by_pitcher.get(player_id, []),
        )
        payload: dict[str, Any] = {
            "contract_version": FEATURE_VERSION,
            "feature_as_of": feature_as_of.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "player_id": player_id,
            "hitting": hitting,
            "batted_ball": {
                **_window_bundle(batted_windows),
                "splits": _batted_ball_splits(
                    player_balls,
                    feature_as_of=feature_as_of,
                    knowledge_cutoff=cutoff,
                ),
            },
            "pitching": pitching,
            "pitch_traits": {
                **_window_bundle(pitch_windows),
                "splits": _pitch_trait_splits(
                    player_pitches,
                    feature_as_of=feature_as_of,
                    knowledge_cutoff=cutoff,
                ),
            },
            "previous_start": asdict(statcast_starts[-1]) if statcast_starts else None,
            "previous_three_starts": [
                asdict(row) for row in statcast_starts[-3:]
            ],
            "pitcher_appearance_history_source": history_source,
            "pitcher_workload": pitcher_workload,
        }
        payload["feature_checksum"] = _checksum(payload)
        result[player_id] = payload
    return result


def build_aggregate_player_feature_payloads_v3(
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    batting_aggregates: Iterable[BattingAggregateLine],
    pitching_aggregates: Iterable[PitchingAggregateLine],
    batted_balls: Iterable[StatcastBattedBall] = (),
    pitch_metrics: Iterable[StatcastPitchMetric] = (),
    swing_metrics: Iterable[StatcastSwingMetric] = (),
    pitcher_appearances: Iterable[StatcastPitcherAppearance] = (),
    statcast_plate_appearances: Iterable[StatcastPlateAppearance] = (),
) -> dict[str, dict[str, Any]]:
    """Build additive Feature V3 player payloads on the trusted V2 contract.

    V2 remains the compatibility baseline. This builder materializes all
    iterable inputs once, delegates existing baseball calculations to the
    V2 builder, then adds V3-only Statcast swing and pitch-physics windows.
    """

    batting_rows = tuple(batting_aggregates)
    pitching_rows = tuple(pitching_aggregates)
    batted_ball_rows = tuple(batted_balls)
    pitch_metric_rows = tuple(pitch_metrics)
    swing_metric_rows = tuple(swing_metrics)
    appearance_rows = tuple(pitcher_appearances)
    plate_appearance_rows = tuple(statcast_plate_appearances)

    base_payloads = build_aggregate_player_feature_payloads(
        feature_as_of=feature_as_of,
        knowledge_cutoff=knowledge_cutoff,
        batting_aggregates=batting_rows,
        pitching_aggregates=pitching_rows,
        batted_balls=batted_ball_rows,
        pitch_metrics=pitch_metric_rows,
        pitcher_appearances=appearance_rows,
        statcast_plate_appearances=plate_appearance_rows,
    )

    cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
    known_swings = [
        row
        for row in swing_metric_rows
        if row.game_date < feature_as_of
        and _known_by(row, cutoff)
    ]

    known_pitches = [
        row for row in pitch_metric_rows if _known_by(row, cutoff)
    ]

    swings_by_batter: defaultdict[
        str, list[StatcastSwingMetric]
    ] = defaultdict(list)
    pitches_by_pitcher: defaultdict[
        str, list[StatcastPitchMetric]
    ] = defaultdict(list)

    for swing_row in known_swings:
        swings_by_batter[swing_row.batter_id].append(swing_row)

    for pitch_row in known_pitches:
        pitches_by_pitcher[pitch_row.pitcher_id].append(pitch_row)

    orphan_swing_players = sorted(
        set(swings_by_batter) - set(base_payloads)
    )
    if orphan_swing_players:
        raise ValueError(
            "Feature V3 swing evidence references player absent from "
            "the V2 base payload: "
            + ", ".join(orphan_swing_players)
        )

    result: dict[str, dict[str, Any]] = {}

    for player_id, base_payload in base_payloads.items():
        swing_windows = _swing_metric_windows(
            swings_by_batter.get(player_id, []),
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )
        physics_windows = _pitch_physics_windows(
            pitches_by_pitcher.get(player_id, []),
            feature_as_of=feature_as_of,
            knowledge_cutoff=cutoff,
        )

        payload: dict[str, Any] = {
            key: value
            for key, value in base_payload.items()
            if key != "feature_checksum"
        }

        payload["contract_version"] = FEATURE_VERSION_V3
        payload["swing_metrics"] = _window_bundle(swing_windows)
        payload["pitch_physics"] = _window_bundle(physics_windows)
        payload["feature_checksum"] = _checksum(payload)

        result[player_id] = payload

    return result


def build_aggregate_team_feature_payloads(
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    batting_aggregates: Iterable[BattingAggregateLine],
    pitching_aggregates: Iterable[PitchingAggregateLine],
) -> dict[str, dict[str, Any]]:
    """Aggregate exact club/stint player rows into point-in-time team totals."""

    cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
    batting = [
        row
        for row in batting_aggregates
        if row.team_key is not None
        and row.through_date < feature_as_of
        and _known_by(row, cutoff)
    ]
    pitching = [
        row
        for row in pitching_aggregates
        if row.team_key is not None
        and row.through_date < feature_as_of
        and _known_by(row, cutoff)
    ]
    for kind, rows in (("batting", batting), ("pitching", pitching)):
        identities = [
            (row.player_id, row.window_key, row.source_stint_key)
            for row in rows
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(f"duplicate {kind} aggregate player/window/stint")

    teams = sorted(
        {str(row.team_key) for row in batting}
        | {str(row.team_key) for row in pitching}
    )
    result: dict[str, dict[str, Any]] = {}
    for team_key in teams:
        hitting: dict[str, Any] = {}
        pitching_payload: dict[str, Any] = {}
        for window_key in sorted(AGGREGATE_WINDOW_KEYS):
            hitting_rows = [
                row.as_game_line()
                for row in batting
                if row.team_key == team_key and row.window_key == window_key
            ]
            pitching_rows = [
                row.as_game_line()
                for row in pitching
                if row.team_key == team_key and row.window_key == window_key
            ]
            hitting[window_key] = _window_payload(hitting_rows, hitter_rate_stats)
            pitching_payload[window_key] = _window_payload(
                pitching_rows, pitcher_rate_stats
            )
        payload: dict[str, Any] = {
            "contract_version": FEATURE_VERSION,
            "feature_as_of": feature_as_of.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "team_key": team_key,
            "hitting": hitting,
            "pitching": pitching_payload,
            "source": "baseball_reference_player_aggregate_club_stints",
        }
        payload["feature_checksum"] = _checksum(payload)
        result[team_key] = payload
    return result


def build_bullpen_workload(
    *,
    feature_as_of: date,
    knowledge_cutoff: datetime,
    pitching_lines: Iterable[PitchingGameLine] = (),
    pitcher_appearances: Iterable[StatcastPitcherAppearance] = (),
) -> dict[str, dict[str, Any]]:
    cutoff = _aware_utc(knowledge_cutoff, "knowledge_cutoff")
    relief_lines = [
        row
        for row in pitching_lines
        if not row.is_start
        and row.game_date < feature_as_of
        and _known_by(row, cutoff)
    ]
    relief_appearances = [
        row
        for row in pitcher_appearances
        if not row.is_start
        and row.game_date < feature_as_of
        and _known_by(row, cutoff)
    ]
    line_keys = {
        (row.game_date, row.player_id, row.team_key) for row in relief_lines
    }
    appearance_keys = {
        (row.game_date, row.pitcher_id, row.team_key)
        for row in relief_appearances
    }
    if line_keys & appearance_keys:
        raise ValueError(
            "duplicate bullpen appearance across game-line and Statcast inputs"
        )
    team_keys = sorted(
        {row.team_key for row in relief_lines}
        | {row.team_key for row in relief_appearances}
    )
    result: dict[str, dict[str, Any]] = {}
    for team_key in team_keys:
        team_lines = [row for row in relief_lines if row.team_key == team_key]
        team_appearances = [
            row for row in relief_appearances if row.team_key == team_key
        ]
        windows: dict[str, Any] = {}
        for days in (1, 3, 7):
            selected_lines = _windowed(
                team_lines, feature_as_of, days, cutoff
            )
            selected_appearances = _windowed(
                team_appearances, feature_as_of, days, cutoff
            )
            known_outs = [row.outs_recorded for row in selected_lines]
            known_outs.extend(
                row.outs_recorded
                for row in selected_appearances
                if row.outs_recorded is not None
            )
            unknown_outs = sum(
                row.outs_recorded is None for row in selected_appearances
            )
            appearance_count = len(selected_lines) + len(selected_appearances)
            windows[f"previous_{days}_days"] = {
                "appearances": appearance_count,
                "pitchers_used": len(
                    {row.player_id for row in selected_lines}
                    | {row.pitcher_id for row in selected_appearances}
                ),
                "batters_faced": sum(
                    row.batters_faced for row in selected_lines
                )
                + sum(row.batters_faced for row in selected_appearances),
                "outs_recorded": (
                    sum(int(value) for value in known_outs)
                    if unknown_outs == 0
                    else None
                ),
                "outs_recorded_known": sum(
                    int(value) for value in known_outs
                ),
                "outs_recorded_known_appearances": appearance_count
                - unknown_outs,
                "outs_recorded_unknown_appearances": unknown_outs,
                "pitches": sum(row.pitches for row in selected_lines)
                + sum(row.pitches for row in selected_appearances),
            }
        payload = {
            "contract_version": FEATURE_VERSION,
            "feature_as_of": feature_as_of.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "team_key": team_key,
            "bullpen_workload_source": (
                "statcast_final_game_appearances"
                if team_appearances and not team_lines
                else "pitching_game_lines"
                if team_lines and not team_appearances
                else "reconciled_mixed_sources"
            ),
            "bullpen_workload": windows,
        }
        payload["feature_checksum"] = _checksum(payload)
        result[team_key] = payload
    return result
