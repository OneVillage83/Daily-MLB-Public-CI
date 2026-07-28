from __future__ import annotations

from app.daily_slate.contracts import canonical_sha256

MODEL_FEATURE_SET_CONTRACT_VERSION = "DSE_MODEL_FEATURE_SET_V1"
MODEL_FEATURE_SCHEMA_VERSION = "DSE_MODEL_FEATURE_SCHEMA_V1"
MODEL_FEATURE_SET_SPORT = "MLB"
MODEL_FEATURE_SET_LEAGUE = "MLB"
EXPECTED_MODEL_FEATURE_COUNT_V1 = 535
EXPECTED_MODEL_FEATURE_SCHEMA_CHECKSUM_V1 = (
    "912fb7f961a410fdee7e63beffefcd0a11d997dafdd925e96460e5b7afb65d1b"
)

FEATURE_WINDOWS = (
    "season_to_date",
    "rolling_7_days",
    "rolling_14_days",
    "rolling_30_days",
)
TEAM_SIDES = ("away", "home")

_COVERAGE_FIELDS = (
    "gameday_player_count",
    "resolved_player_count",
    "player_feature_count",
    "lineup_player_count",
    "lineup_feature_count",
    "bullpen_player_count",
    "bullpen_feature_count",
    "bench_player_count",
    "bench_feature_count",
    "starter_feature_available",
)

_STARTER_PITCHING_FIELDS = (
    "era",
    "whip",
    "k_rate",
    "bb_rate",
    "k_minus_bb_rate",
    "k_per_9",
    "bb_per_9",
    "hr_per_9",
)

_STARTER_TRAIT_FIELDS = (
    "strike_rate",
    "whiff_rate",
    "chase_rate",
    "contact_rate",
)

_STARTER_PHYSICS_FIELDS = (
    "effective_speed_mean",
    "release_pos_x_mean",
    "release_pos_z_mean",
    "arm_angle_mean",
    "api_break_x_arm_mean",
    "api_break_z_with_gravity_mean",
)

_LINEUP_HITTING_FIELDS = (
    "pa",
    "avg",
    "obp",
    "slg",
    "ops",
    "iso",
    "babip",
    "k_rate",
    "bb_rate",
)

_LINEUP_BATTED_BALL_FIELDS = (
    "batted_ball_count",
    "exit_velocity",
    "max_exit_velocity",
    "hard_hit_rate",
    "barrel_rate",
    "launch_angle",
    "xba",
    "xslg",
    "xwoba",
)

_LINEUP_SWING_FIELDS = (
    "bat_speed_mean",
    "swing_length_mean",
    "attack_angle_mean",
    "attack_direction_mean",
    "swing_path_tilt_mean",
    "miss_distance_mean",
    "hyper_speed_mean",
)

_BULLPEN_PITCHING_FIELDS = (
    "bf",
    "era",
    "whip",
    "k_rate",
    "bb_rate",
    "k_minus_bb_rate",
    "k_per_9",
    "bb_per_9",
    "hr_per_9",
)

_STARTER_CERTAINTY_VALUES = ("unavailable", "probable", "announced", "confirmed")
_LINEUP_AVAILABILITY_VALUES = ("unavailable", "partial", "posted")
_DOUBLEHEADER_VALUES = ("single", "doubleheader", "unknown")
_WEATHER_STATUS_VALUES = ("available", "indoor_fixed_roof", "unavailable")
_WEATHER_RELEVANCE_VALUES = (
    "direct",
    "contextual_roof_status_unknown",
    "contextual_roof_type_unverified",
    "indoor_suppressed",
    "unavailable",
)
_ROOF_TYPE_VALUES = ("open", "fixed", "retractable", "unknown")

_WEATHER_NUMERIC_FIELDS = (
    "temperature_f",
    "humidity_pct",
    "precipitation_probability_pct",
    "wind_speed_mph",
    "wind_gust_mph",
    "clouds_pct",
    "pressure_hpa",
    "wind_outfield_component_mph",
    "wind_crosswind_component_mph",
)


def _build_feature_names() -> tuple[str, ...]:
    names: list[str] = ["schedule.game_number"]
    names.extend(f"schedule.doubleheader.{value}" for value in _DOUBLEHEADER_VALUES)
    for side in TEAM_SIDES:
        names.extend(f"{side}.starter_certainty.{value}" for value in _STARTER_CERTAINTY_VALUES)
        names.extend(f"{side}.lineup_availability.{value}" for value in _LINEUP_AVAILABILITY_VALUES)
        names.extend(f"{side}.coverage.{field}" for field in _COVERAGE_FIELDS)
        for window in FEATURE_WINDOWS:
            names.extend(f"{side}.starter.{window}.pitching.{field}" for field in _STARTER_PITCHING_FIELDS)
            names.extend(f"{side}.starter.{window}.pitch_traits.{field}" for field in _STARTER_TRAIT_FIELDS)
            names.extend(f"{side}.starter.{window}.pitch_physics.{field}" for field in _STARTER_PHYSICS_FIELDS)
            names.append(f"{side}.lineup.{window}.available_player_count")
            names.extend(f"{side}.lineup.{window}.hitting.{field}" for field in _LINEUP_HITTING_FIELDS)
            names.append(f"{side}.lineup.{window}.batted_ball_player_count")
            names.extend(f"{side}.lineup.{window}.batted_ball.{field}" for field in _LINEUP_BATTED_BALL_FIELDS)
            names.append(f"{side}.lineup.{window}.swing_player_count")
            names.extend(f"{side}.lineup.{window}.swing.{field}" for field in _LINEUP_SWING_FIELDS)
            names.append(f"{side}.bullpen.{window}.available_player_count")
            names.extend(f"{side}.bullpen.{window}.pitching.{field}" for field in _BULLPEN_PITCHING_FIELDS)
        names.extend((f"{side}.starter.workload.days_since_previous_appearance", f"{side}.starter.workload.days_since_previous_start"))
        for days in (1, 3, 7):
            for field in ("appearance_count", "start_count", "relief_appearance_count", "pitch_count"):
                names.append(f"{side}.starter.workload.previous_{days}_days.{field}")
    names.extend(f"weather.status.{value}" for value in _WEATHER_STATUS_VALUES)
    names.extend(f"weather.relevance.{value}" for value in _WEATHER_RELEVANCE_VALUES)
    names.extend(f"weather.roof_type.{value}" for value in _ROOF_TYPE_VALUES)
    names.extend(f"weather.{field}" for field in _WEATHER_NUMERIC_FIELDS)
    return tuple(names)


MODEL_FEATURE_NAMES_V1 = _build_feature_names()
MODEL_FEATURE_SCHEMA_CHECKSUM = canonical_sha256(
    {"feature_names": list(MODEL_FEATURE_NAMES_V1), "schema_version": MODEL_FEATURE_SCHEMA_VERSION}
)
if len(MODEL_FEATURE_NAMES_V1) != EXPECTED_MODEL_FEATURE_COUNT_V1:
    raise RuntimeError("ModelFeatureSet V1 schema width changed without a version bump")
if MODEL_FEATURE_SCHEMA_CHECKSUM != EXPECTED_MODEL_FEATURE_SCHEMA_CHECKSUM_V1:
    raise RuntimeError("ModelFeatureSet V1 schema changed without a version bump")
FEATURE_NAME_SET_V1 = frozenset(MODEL_FEATURE_NAMES_V1)
FEATURE_INDEX_V1 = {name: index for index, name in enumerate(MODEL_FEATURE_NAMES_V1)}
