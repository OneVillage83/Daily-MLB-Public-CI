from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from app.baseball_intelligence.contracts import (
    PlayerIntelligenceV1,
    TeamBaseballIntelligenceV1,
)
from app.matchup_packet.contracts import MatchupPacketGameV1, MatchupPacketV1
from app.model_feature_set.contracts import ModelFeatureGameV1, ModelFeatureSetV1
from app.model_feature_set.schema import (
    FEATURE_INDEX_V1,
    FEATURE_WINDOWS,
    MODEL_FEATURE_NAMES_V1,
)


class ModelFeatureSetBuildError(RuntimeError):
    """Fail-closed error while deriving the frozen V1 model feature schema."""


class _FeatureVectorBuilder:
    def __init__(self) -> None:
        self.values: list[float | None] = [None] * len(MODEL_FEATURE_NAMES_V1)

    def set(self, name: str, value: object) -> None:
        index = FEATURE_INDEX_V1.get(name)
        if index is None:
            raise ModelFeatureSetBuildError(
                f"feature is not in frozen V1 schema: {name}"
            )
        if value is None:
            return
        if isinstance(value, bool):
            self.values[index] = 1.0 if value else 0.0
            return
        if not isinstance(value, int | float):
            raise ModelFeatureSetBuildError(
                f"feature {name} must be numeric or null"
            )
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ModelFeatureSetBuildError(f"feature {name} must be finite")
        self.values[index] = numeric

    def one_hot(self, prefix: str, values: Sequence[str], selected: str) -> None:
        normalized = selected if selected in values else "unknown"
        for value in values:
            self.set(f"{prefix}.{value}", 1.0 if value == normalized else 0.0)


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _mapping_sequence(value: object) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if denominator <= 0 else numerator / denominator


def _player_payload(player: PlayerIntelligenceV1) -> Mapping[str, Any] | None:
    if player.feature is None:
        return None
    return player.feature.features


def _players_by_source(
    team: TeamBaseballIntelligenceV1,
) -> dict[str, PlayerIntelligenceV1]:
    return {player.source_player_id: player for player in team.players}


def _window(
    payload: Mapping[str, Any],
    section: str,
    window: str,
) -> Mapping[str, Any] | None:
    root = _mapping(payload.get(section))
    if root is None:
        return None
    if section in {"batted_ball", "pitch_traits", "swing_metrics", "pitch_physics"}:
        windows = _mapping(root.get("windows"))
        return None if windows is None else _mapping(windows.get(window))
    return _mapping(root.get(window))


def _sum_counts(
    rows: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> dict[str, float] | None:
    if not rows:
        return None
    totals = {key: 0.0 for key in keys}
    for row in rows:
        for key in keys:
            value = _finite(row.get(key))
            if value is None:
                return None
            totals[key] += value
    return totals


def _hitting_rates(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    totals = _sum_counts(
        rows,
        ("pa", "ab", "h", "2b", "3b", "hr", "bb", "hbp", "so", "sf"),
    )
    if totals is None:
        return {}
    singles = max(0.0, totals["h"] - totals["2b"] - totals["3b"] - totals["hr"])
    total_bases = (
        singles
        + 2.0 * totals["2b"]
        + 3.0 * totals["3b"]
        + 4.0 * totals["hr"]
    )
    avg = _ratio(totals["h"], totals["ab"])
    obp = _ratio(
        totals["h"] + totals["bb"] + totals["hbp"],
        totals["ab"] + totals["bb"] + totals["hbp"] + totals["sf"],
    )
    slg = _ratio(total_bases, totals["ab"])
    babip = _ratio(
        totals["h"] - totals["hr"],
        totals["ab"] - totals["so"] - totals["hr"] + totals["sf"],
    )
    return {
        "pa": totals["pa"],
        "avg": avg,
        "obp": obp,
        "slg": slg,
        "ops": None if obp is None or slg is None else obp + slg,
        "iso": None if avg is None or slg is None else slg - avg,
        "babip": babip,
        "k_rate": _ratio(totals["so"], totals["pa"]),
        "bb_rate": _ratio(totals["bb"], totals["pa"]),
    }


def _pitching_rates(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    totals = _sum_counts(
        rows,
        ("bf", "outs_recorded", "h", "er", "hr", "bb", "hbp", "so", "pitches"),
    )
    if totals is None:
        return {}
    innings = totals["outs_recorded"] / 3.0
    k_rate = _ratio(totals["so"], totals["bf"])
    bb_rate = _ratio(totals["bb"], totals["bf"])
    return {
        "bf": totals["bf"],
        "era": _ratio(9.0 * totals["er"], innings),
        "whip": _ratio(totals["bb"] + totals["h"], innings),
        "k_rate": k_rate,
        "bb_rate": bb_rate,
        "k_minus_bb_rate": (
            None if k_rate is None or bb_rate is None else k_rate - bb_rate
        ),
        "k_per_9": _ratio(9.0 * totals["so"], innings),
        "bb_per_9": _ratio(9.0 * totals["bb"], innings),
        "hr_per_9": _ratio(9.0 * totals["hr"], innings),
    }


def _weighted_mean(
    rows: Sequence[Mapping[str, Any]],
    value_key: str,
    weight_key: str,
) -> float | None:
    numerator = 0.0
    denominator = 0.0
    for row in rows:
        value = _finite(row.get(value_key))
        weight = _finite(row.get(weight_key))
        if value is None or weight is None or weight <= 0:
            continue
        numerator += value * weight
        denominator += weight
    return None if denominator <= 0 else numerator / denominator


def _starter_history_metrics(
    payload: Mapping[str, Any],
) -> dict[str, float | None]:
    source = payload.get("pitcher_appearance_history_source")
    if source == "unavailable":
        return {}

    result: dict[str, float | None] = {}
    fields = (
        "pitches",
        "batters_faced",
        "hits",
        "home_runs",
        "walks",
        "strikeouts",
        "outs_recorded",
    )
    previous_start = _mapping(payload.get("previous_start"))
    if previous_start is not None:
        for field in fields:
            result[f"previous_start.{field}"] = _finite(previous_start.get(field))

    previous_three = _mapping_sequence(payload.get("previous_three_starts"))
    result["previous_three_starts.start_count"] = float(len(previous_three))
    if not previous_three:
        return result

    for field in fields:
        values = [_finite(row.get(field)) for row in previous_three]
        if any(value is None for value in values):
            continue
        numeric_values = [value for value in values if value is not None]
        total = sum(numeric_values)
        result[f"previous_three_starts.{field}_sum"] = total
        result[f"previous_three_starts.{field}_mean"] = total / len(numeric_values)
    return result


def _bullpen_workload_metrics(
    payloads: Sequence[Mapping[str, Any]],
) -> dict[str, float | None]:
    workloads: list[Mapping[str, Any]] = []
    for payload in payloads:
        workload = _mapping(payload.get("pitcher_workload"))
        if workload is None or workload.get("source") == "unavailable":
            continue
        workloads.append(workload)

    result: dict[str, float | None] = {
        "available_player_count": float(len(workloads))
    }
    if not workloads:
        return result

    for source_field, output_prefix in (
        ("days_since_previous_appearance", "days_since_previous_appearance"),
        ("days_since_previous_start", "days_since_previous_start"),
    ):
        values = [
            value
            for workload in workloads
            if (value := _finite(workload.get(source_field))) is not None
        ]
        if values:
            result[f"{output_prefix}_min"] = min(values)
            result[f"{output_prefix}_mean"] = sum(values) / len(values)

    for days in (1, 3, 7):
        recent_rows: list[Mapping[str, Any]] = []
        for workload in workloads:
            recent = _mapping(workload.get("recent"))
            row = (
                None
                if recent is None
                else _mapping(recent.get(f"previous_{days}_days"))
            )
            if row is None:
                recent_rows = []
                break
            recent_rows.append(row)
        if not recent_rows:
            continue
        for source_field in (
            "appearance_count",
            "start_count",
            "relief_appearance_count",
            "pitch_count",
        ):
            numeric_values: list[float] = []
            complete = True
            for row in recent_rows:
                value = _finite(row.get(source_field))
                if value is None:
                    complete = False
                    break
                numeric_values.append(value)
            if complete:
                result[f"previous_{days}_days.{source_field}_sum"] = sum(
                    numeric_values
                )
    return result


def _set_structural_features(
    builder: _FeatureVectorBuilder,
    game: MatchupPacketGameV1,
) -> None:
    builder.set("schedule.game_number", game.schedule.game_number)
    builder.one_hot(
        "schedule.doubleheader",
        ("single", "doubleheader", "unknown"),
        game.schedule.doubleheader_status.value,
    )
    for side in ("away", "home"):
        state_team = getattr(game.game_state, side)
        intelligence_team = getattr(game.baseball_intelligence, side)
        builder.one_hot(
            f"{side}.starter_certainty",
            ("unavailable", "probable", "announced", "confirmed"),
            state_team.starter.certainty.value,
        )
        builder.one_hot(
            f"{side}.lineup_availability",
            ("unavailable", "partial", "posted"),
            state_team.lineup.availability.value,
        )
        coverage = intelligence_team.coverage
        for field in (
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
        ):
            builder.set(f"{side}.coverage.{field}", getattr(coverage, field))


def _set_starter_features(
    builder: _FeatureVectorBuilder,
    side: str,
    team: TeamBaseballIntelligenceV1,
) -> None:
    starter_id = team.starter_source_player_id
    if starter_id is None:
        return
    player = _players_by_source(team).get(starter_id)
    if player is None:
        return
    payload = _player_payload(player)
    if payload is None:
        return

    for window_name in FEATURE_WINDOWS:
        pitching = _window(payload, "pitching", window_name)
        if pitching is not None and pitching.get("data_status") == "available":
            for field in (
                "era",
                "whip",
                "k_rate",
                "bb_rate",
                "k_minus_bb_rate",
                "k_per_9",
                "bb_per_9",
                "hr_per_9",
            ):
                builder.set(
                    f"{side}.starter.{window_name}.pitching.{field}",
                    pitching.get(field),
                )
        traits = _window(payload, "pitch_traits", window_name)
        if traits is not None:
            for field in ("strike_rate", "whiff_rate", "chase_rate", "contact_rate"):
                builder.set(
                    f"{side}.starter.{window_name}.pitch_traits.{field}",
                    traits.get(field),
                )
        physics = _window(payload, "pitch_physics", window_name)
        if physics is not None:
            for field in (
                "effective_speed_mean",
                "release_pos_x_mean",
                "release_pos_z_mean",
                "arm_angle_mean",
                "api_break_x_arm_mean",
                "api_break_z_with_gravity_mean",
            ):
                builder.set(
                    f"{side}.starter.{window_name}.pitch_physics.{field}",
                    physics.get(field),
                )

    workload = _mapping(payload.get("pitcher_workload"))
    if workload is not None and workload.get("source") != "unavailable":
        builder.set(
            f"{side}.starter.workload.days_since_previous_appearance",
            workload.get("days_since_previous_appearance"),
        )
        builder.set(
            f"{side}.starter.workload.days_since_previous_start",
            workload.get("days_since_previous_start"),
        )
        recent = _mapping(workload.get("recent"))
        if recent is not None:
            for days in (1, 3, 7):
                row = _mapping(recent.get(f"previous_{days}_days"))
                if row is None:
                    continue
                for field in (
                    "appearance_count",
                    "start_count",
                    "relief_appearance_count",
                    "pitch_count",
                ):
                    builder.set(
                        f"{side}.starter.workload.previous_{days}_days.{field}",
                        row.get(field),
                    )

    for key, value in _starter_history_metrics(payload).items():
        builder.set(f"{side}.starter.{key}", value)


def _selected_payloads(
    team: TeamBaseballIntelligenceV1,
    source_ids: Sequence[str],
) -> list[Mapping[str, Any]]:
    index = _players_by_source(team)
    result: list[Mapping[str, Any]] = []
    for source_id in source_ids:
        player = index.get(source_id)
        if player is None:
            continue
        payload = _player_payload(player)
        if payload is not None:
            result.append(payload)
    return result


def _set_lineup_features(
    builder: _FeatureVectorBuilder,
    side: str,
    team: TeamBaseballIntelligenceV1,
) -> None:
    payloads = _selected_payloads(team, team.lineup_source_player_ids)
    for window_name in FEATURE_WINDOWS:
        hitting_rows = [
            row
            for payload in payloads
            if (row := _window(payload, "hitting", window_name)) is not None
            and row.get("data_status") == "available"
        ]
        builder.set(
            f"{side}.lineup.{window_name}.available_player_count",
            len(hitting_rows),
        )
        for field, value in _hitting_rates(hitting_rows).items():
            builder.set(f"{side}.lineup.{window_name}.hitting.{field}", value)

        batted_rows = [
            row
            for payload in payloads
            if (row := _window(payload, "batted_ball", window_name)) is not None
        ]
        batted_present = [
            row
            for row in batted_rows
            if (_finite(row.get("batted_ball_count")) or 0.0) > 0
        ]
        builder.set(
            f"{side}.lineup.{window_name}.batted_ball_player_count",
            len(batted_present),
        )
        total_batted = sum(
            _finite(row.get("batted_ball_count")) or 0.0 for row in batted_present
        )
        if batted_present:
            builder.set(
                f"{side}.lineup.{window_name}.batted_ball.batted_ball_count",
                total_batted,
            )
            for field in (
                "exit_velocity",
                "hard_hit_rate",
                "barrel_rate",
                "launch_angle",
                "xba",
                "xslg",
                "xwoba",
            ):
                builder.set(
                    f"{side}.lineup.{window_name}.batted_ball.{field}",
                    _weighted_mean(batted_present, field, "batted_ball_count"),
                )
            maxima = [
                value
                for row in batted_present
                if (value := _finite(row.get("max_exit_velocity"))) is not None
            ]
            builder.set(
                f"{side}.lineup.{window_name}.batted_ball.max_exit_velocity",
                max(maxima) if maxima else None,
            )

        swing_rows = [
            row
            for payload in payloads
            if (row := _window(payload, "swing_metrics", window_name)) is not None
        ]
        swing_present = [
            row
            for row in swing_rows
            if (_finite(row.get("swing_observation_count")) or 0.0) > 0
        ]
        builder.set(
            f"{side}.lineup.{window_name}.swing_player_count",
            len(swing_present),
        )
        for field in (
            "bat_speed_mean",
            "swing_length_mean",
            "attack_angle_mean",
            "attack_direction_mean",
            "swing_path_tilt_mean",
            "miss_distance_mean",
            "hyper_speed_mean",
        ):
            builder.set(
                f"{side}.lineup.{window_name}.swing.{field}",
                _weighted_mean(
                    swing_present,
                    field,
                    field.replace("_mean", "_samples"),
                ),
            )


def _set_bullpen_features(
    builder: _FeatureVectorBuilder,
    side: str,
    team: TeamBaseballIntelligenceV1,
) -> None:
    payloads = _selected_payloads(team, team.bullpen_source_player_ids)
    for window_name in FEATURE_WINDOWS:
        pitching_rows = [
            row
            for payload in payloads
            if (row := _window(payload, "pitching", window_name)) is not None
            and row.get("data_status") == "available"
        ]
        builder.set(
            f"{side}.bullpen.{window_name}.available_player_count",
            len(pitching_rows),
        )
        for field, value in _pitching_rates(pitching_rows).items():
            builder.set(f"{side}.bullpen.{window_name}.pitching.{field}", value)

    for key, value in _bullpen_workload_metrics(payloads).items():
        builder.set(f"{side}.bullpen.workload.{key}", value)


def _set_weather_features(
    builder: _FeatureVectorBuilder,
    game: MatchupPacketGameV1,
) -> None:
    weather = game.odds_weather.weather
    builder.one_hot(
        "weather.status",
        ("available", "indoor_fixed_roof", "unavailable"),
        weather.status.value,
    )
    builder.one_hot(
        "weather.relevance",
        (
            "direct",
            "contextual_roof_status_unknown",
            "contextual_roof_type_unverified",
            "indoor_suppressed",
            "unavailable",
        ),
        weather.relevance.value,
    )
    roof_type = "unknown"
    if weather.venue_context is not None:
        roof_type = weather.venue_context.roof_type
    builder.one_hot(
        "weather.roof_type",
        ("open", "fixed", "retractable", "unknown"),
        roof_type,
    )
    primary = None
    if weather.primary_source is not None:
        primary = (
            weather.nws
            if weather.primary_source.value == "nws"
            else weather.openweather
        )
    if primary is not None:
        forecast = primary.forecast
        for field in (
            "temperature_f",
            "humidity_pct",
            "precipitation_probability_pct",
            "wind_speed_mph",
            "wind_gust_mph",
            "clouds_pct",
            "pressure_hpa",
        ):
            builder.set(f"weather.{field}", forecast.get(field))
    wind = weather.baseball_wind_impact
    builder.set(
        "weather.wind_outfield_component_mph",
        wind.get("outfield_component_mph"),
    )
    builder.set(
        "weather.wind_crosswind_component_mph",
        wind.get("crosswind_component_mph"),
    )


def build_model_feature_game(game: MatchupPacketGameV1) -> ModelFeatureGameV1:
    builder = _FeatureVectorBuilder()
    _set_structural_features(builder, game)
    for side in ("away", "home"):
        team = getattr(game.baseball_intelligence, side)
        _set_starter_features(builder, side, team)
        _set_lineup_features(builder, side, team)
        _set_bullpen_features(builder, side, team)
    _set_weather_features(builder, game)
    return ModelFeatureGameV1(
        edge_event_id=game.edge_event_id,
        daily_mlb_game_id=game.daily_mlb_game_id,
        source_game_id=game.source_game_id,
        away_team_id=game.away_team_id,
        home_team_id=game.home_team_id,
        upstream_matchup_packet_game_checksum=game.checksum,
        quality_disposition=game.quality_disposition,
        quality_issue_codes=tuple(issue.code for issue in game.data_quality.issues),
        market_reference_checksum=game.odds_weather.odds.summary_checksum,
        feature_values=tuple(builder.values),
    )


def build_model_feature_set(
    packet: MatchupPacketV1,
    *,
    observed_at: datetime | None = None,
) -> ModelFeatureSetV1:
    packet_observed = packet.observed_at.astimezone(timezone.utc)
    selected_observed = packet_observed if observed_at is None else observed_at
    if selected_observed.tzinfo is None or selected_observed.utcoffset() is None:
        raise ModelFeatureSetBuildError(
            "ModelFeatureSet observed_at must be timezone-aware"
        )
    selected_observed = selected_observed.astimezone(timezone.utc)
    if selected_observed < packet_observed:
        raise ModelFeatureSetBuildError(
            "ModelFeatureSet observed_at cannot precede MatchupPacket evidence"
        )
    return ModelFeatureSetV1(
        requested_date=packet.requested_date,
        as_of_time=packet.as_of_time,
        observed_at=selected_observed,
        upstream_matchup_packet_checksum=packet.checksum,
        games=tuple(build_model_feature_game(game) for game in packet.games),
    )
