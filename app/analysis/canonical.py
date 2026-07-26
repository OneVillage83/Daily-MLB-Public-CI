from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.analysis.models import (
    DataQualityState,
    QualityIssue,
    checksum_payload,
    finite_number,
    json_value,
    optional_utc_datetime,
    utc_datetime,
)
from app.stadiums import (
    is_field_verified,
    material_weather_metadata_errors,
    roof_operational_status,
)

CANONICAL_GAME_VERSION = "canonical-game-v1"
FEATURE_VERSION = "mlb-features-v1"
MAX_GAME_WEATHER_OFFSET_SECONDS = 60 * 60
MINIMUM_LAUNCH_H2H_BOOKS = 4
MAXIMUM_LAUNCH_ODDS_AGE_SECONDS = 120
MAXIMUM_LAUNCH_ODDS_FUTURE_SKEW_SECONDS = 30
MAXIMUM_PAIR_TIMESTAMP_SKEW_SECONDS = 5


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    contract_version: str
    run_id: str
    event_id: str
    requested_date: date
    commence_time: datetime
    raw_home_team: str
    raw_away_team: str
    home_team_key: str
    away_team_key: str
    venue_context: Mapping[str, Any]
    roof_context: str
    odds_consensus: Mapping[str, Any]
    weather_context: Mapping[str, Any]
    weather_gate_clear: bool
    quality_state: DataQualityState
    quality_issues: tuple[QualityIssue, ...]
    source_checksums: Mapping[str, str]
    assembled_at: datetime
    checksum: str

    def as_dict(self) -> dict[str, Any]:
        return dict(json_value(self))


@dataclass(frozen=True, slots=True)
class MlbFeatures:
    contract_version: str
    event_id: str
    canonical_game_checksum: str
    schedule_context: Mapping[str, Any]
    team_context: Mapping[str, Any]
    venue_context: Mapping[str, Any]
    weather_context: Mapping[str, Any]
    market_context: Mapping[str, Any]
    data_quality_state: DataQualityState
    uncertainty_flags: tuple[str, ...]
    generated_at: datetime
    checksum: str

    def as_dict(self) -> dict[str, Any]:
        return dict(json_value(self))

    def prediction_input_view(self) -> dict[str, Any]:
        """Return the market-blind context permitted during analyst prediction entry."""
        return {
            "contract_version": self.contract_version,
            "event_id": self.event_id,
            "canonical_game_checksum": self.canonical_game_checksum,
            "schedule_context": json_value(self.schedule_context),
            "team_context": json_value(self.team_context),
            "venue_context": json_value(self.venue_context),
            "weather_context": json_value(self.weather_context),
            "data_quality_state": self.data_quality_state.value,
            "uncertainty_flags": list(self.uncertainty_flags),
            "generated_at": self.generated_at.isoformat(),
        }


def _copy_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return deepcopy(dict(value)) if value is not None else {}


def _weather_row(weather: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("nws", "openweather"):
        row = weather.get(key)
        if isinstance(row, Mapping):
            return row
    if any(key in weather for key in ("forecast_time", "temperature_f", "wind_speed_mph")):
        return weather
    return None


def _weather_time(weather: Mapping[str, Any]) -> datetime | None:
    row = _weather_row(weather)
    if row is None:
        return None
    for key in ("forecast_time", "valid_time", "observation_time"):
        parsed = optional_utc_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _h2h_line(odds: Mapping[str, Any]) -> Mapping[str, Any] | None:
    markets = odds.get("markets")
    if not isinstance(markets, Mapping):
        return None
    h2h = markets.get("h2h")
    if not isinstance(h2h, Mapping):
        return None
    lines = h2h.get("lines")
    if not isinstance(lines, Mapping):
        return None
    line = lines.get("moneyline")
    return line if isinstance(line, Mapping) else None


def _valid_american_price(value: object) -> bool:
    number = finite_number(value)
    return number is not None and abs(number) >= 100.0


def _launch_offer(
    value: object,
    *,
    assembled_at: datetime,
) -> tuple[str, datetime, datetime] | None:
    if not isinstance(value, Mapping) or not bool(value.get("calculation_eligible", True)):
        return None
    bookmaker = str(value.get("bookmaker_key") or "").strip()
    effective = optional_utc_datetime(value.get("effective_provider_timestamp"))
    retrieved = optional_utc_datetime(
        value.get("provider_retrieved_at") or value.get("retrieved_at")
    )
    if (
        not bookmaker
        or not _valid_american_price(value.get("price"))
        or effective is None
        or retrieved is None
        or retrieved > assembled_at
    ):
        return None
    age = (assembled_at - effective).total_seconds()
    if (
        age < -MAXIMUM_LAUNCH_ODDS_FUTURE_SKEW_SECONDS
        or age > MAXIMUM_LAUNCH_ODDS_AGE_SECONDS
    ):
        return None
    return bookmaker, effective, retrieved


def _fresh_h2h_pair_count(
    line: Mapping[str, Any] | None,
    *,
    home_team_key: str,
    away_team_key: str,
    assembled_at: datetime,
) -> int:
    if line is None:
        return 0
    outcomes = line.get("outcomes")
    if not isinstance(outcomes, Mapping):
        return 0
    by_side: list[dict[str, list[tuple[datetime, datetime]]]] = []
    for team_key in (home_team_key, away_team_key):
        outcome = outcomes.get(team_key)
        offers = outcome.get("offers") if isinstance(outcome, Mapping) else None
        grouped: dict[str, list[tuple[datetime, datetime]]] = {}
        if isinstance(offers, list):
            for offer in offers:
                normalized = _launch_offer(offer, assembled_at=assembled_at)
                if normalized is None:
                    continue
                bookmaker, effective, retrieved = normalized
                grouped.setdefault(bookmaker, []).append((effective, retrieved))
        by_side.append(grouped)
    paired = 0
    for bookmaker in set(by_side[0]) & set(by_side[1]):
        compatible = any(
            abs((home_retrieved - away_retrieved).total_seconds())
            <= MAXIMUM_PAIR_TIMESTAMP_SKEW_SECONDS
            and abs((home_effective - away_effective).total_seconds())
            <= MAXIMUM_PAIR_TIMESTAMP_SKEW_SECONDS
            for home_effective, home_retrieved in by_side[0][bookmaker]
            for away_effective, away_retrieved in by_side[1][bookmaker]
        )
        if compatible:
            paired += 1
    return paired


def _quality_state(issues: list[QualityIssue]) -> DataQualityState:
    if any(issue.blocking for issue in issues):
        return DataQualityState.BLOCKED
    return DataQualityState.DEGRADED if issues else DataQualityState.READY


def assemble_canonical_game(
    *,
    run_id: str,
    requested_date: date,
    odds_summary: Mapping[str, Any],
    weather_packet: Mapping[str, Any] | None,
    assembled_at: datetime | str,
    venue: Mapping[str, Any] | None = None,
) -> CanonicalGame:
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    if not isinstance(requested_date, date) or isinstance(requested_date, datetime):
        raise TypeError("requested_date must be a date")
    assembled = utc_datetime(assembled_at, field="assembled_at")
    event_id = str(odds_summary.get("event_id") or "").strip()
    commence = utc_datetime(str(odds_summary.get("commence_time") or ""), field="commence_time")
    raw_home = str(odds_summary.get("raw_home_team") or odds_summary.get("home_team") or "").strip()
    raw_away = str(odds_summary.get("raw_away_team") or odds_summary.get("away_team") or "").strip()
    home_key = str(odds_summary.get("home_team_key") or "").strip()
    away_key = str(odds_summary.get("away_team_key") or "").strip()
    weather = _copy_mapping(weather_packet)
    venue_data = _copy_mapping(venue)
    if not venue_data and isinstance(weather.get("stadium"), Mapping):
        venue_data = _copy_mapping(weather["stadium"])
    roof_type = str(venue_data.get("roof_type") or "unknown").strip().lower()
    roof_verified = bool(venue_data) and is_field_verified(venue_data, "roof_type")
    all_material_venue_errors = (
        material_weather_metadata_errors(venue_data) if venue_data else []
    )
    operational_roof = roof_operational_status(venue_data)
    roof = str(getattr(operational_roof, "value", operational_roof))
    material_venue_errors = [
        error
        for error in all_material_venue_errors
        if not (
            roof_verified
            and roof_type == "fixed"
            and error.rsplit(":", 1)[-1] in {"latitude", "longitude"}
        )
    ]
    issues: list[QualityIssue] = []

    if not event_id:
        issues.append(QualityIssue("missing_event_id", "Event identity is missing", True))
    if not raw_home or not raw_away or not home_key or not away_key or home_key == away_key:
        issues.append(QualityIssue("invalid_team_identity", "Canonical team identity is incomplete or invalid", True))
    if assembled >= commence:
        issues.append(QualityIssue("event_not_pregame", "Event is at or past scheduled first pitch", True))
    if not venue_data:
        issues.append(QualityIssue("missing_venue", "Venue metadata is missing", True))
    elif material_venue_errors:
        issues.append(
            QualityIssue(
                "venue_weather_metadata_invalid",
                "Material venue metadata is invalid or unverified: "
                + "; ".join(material_venue_errors),
                True,
            )
        )
    if venue_data and str(venue_data.get("team_key") or "").strip() != home_key:
        issues.append(
            QualityIssue(
                "venue_team_mismatch",
                "Venue active-club association does not match the home team",
                True,
            )
        )

    venue_timezone = venue_data.get("timezone")
    if isinstance(venue_timezone, str) and venue_timezone:
        try:
            local_date = commence.astimezone(ZoneInfo(venue_timezone)).date()
        except ZoneInfoNotFoundError:
            issues.append(QualityIssue("invalid_venue_timezone", "Venue timezone is not recognized", True))
        else:
            if local_date != requested_date:
                issues.append(QualityIssue("requested_date_mismatch", "Game does not fall on requested date in venue timezone", True))
    elif venue_data:
        issues.append(QualityIssue("missing_venue_timezone", "Venue timezone is unavailable", False))

    line = _h2h_line(odds_summary)
    if line is None or not bool(line.get("complete_two_way_market")):
        issues.append(QualityIssue("invalid_h2h_market", "A complete two-way h2h market is unavailable", True))
    outcomes = line.get("outcomes") if line is not None else None
    if not isinstance(outcomes, Mapping) or home_key not in outcomes or away_key not in outcomes:
        issues.append(QualityIssue("h2h_identity_mismatch", "H2h outcomes do not match canonical teams", True))
    fresh_h2h_books = _fresh_h2h_pair_count(
        line,
        home_team_key=home_key,
        away_team_key=away_key,
        assembled_at=assembled,
    )
    if fresh_h2h_books < MINIMUM_LAUNCH_H2H_BOOKS:
        issues.append(
            QualityIssue(
                "insufficient_fresh_h2h_books",
                (
                    f"Only {fresh_h2h_books} complete fresh same-book h2h pair(s) "
                    f"are available; {MINIMUM_LAUNCH_H2H_BOOKS} are required"
                ),
                True,
            )
        )

    material_venue_clear = bool(venue_data) and not material_venue_errors and not any(
        issue.code == "venue_team_mismatch" for issue in issues
    )
    indoor = (
        material_venue_clear
        and roof_verified
        and roof_type == "fixed"
        and roof == "closed"
    )
    retractable_unknown = (
        material_venue_clear and roof_verified and roof_type == "retractable"
    )
    weather_gate_clear = indoor
    if retractable_unknown:
        issues.append(
            QualityIssue(
                "retractable_roof_status_unknown",
                "Retractable-roof operational status is unknown",
                False,
            )
        )
    if material_venue_clear and not indoor and not retractable_unknown:
        row = _weather_row(weather)
        forecast_time = _weather_time(weather)
        if row is None:
            issues.append(QualityIssue("weather_missing", "Outdoor game weather is unavailable", False))
        elif forecast_time is None:
            issues.append(QualityIssue("weather_timestamp_missing", "Weather valid timestamp is unavailable", False))
        else:
            offset = abs((forecast_time - commence).total_seconds())
            weather_gate_clear = offset <= MAX_GAME_WEATHER_OFFSET_SECONDS
            if not weather_gate_clear:
                issues.append(QualityIssue("weather_offset_exceeded", "Weather timestamp is more than 60 minutes from first pitch", False))

    odds_copy = _copy_mapping(odds_summary)
    sources = {
        "odds": checksum_payload(odds_copy),
        "venue": checksum_payload(venue_data),
        "weather": checksum_payload(weather),
    }
    state = _quality_state(issues)
    unsigned = {
        "contract_version": CANONICAL_GAME_VERSION,
        "run_id": run_id,
        "event_id": event_id,
        "requested_date": requested_date,
        "commence_time": commence,
        "raw_home_team": raw_home,
        "raw_away_team": raw_away,
        "home_team_key": home_key,
        "away_team_key": away_key,
        "venue_context": venue_data,
        "roof_context": roof,
        "odds_consensus": odds_copy,
        "weather_context": weather,
        "weather_gate_clear": weather_gate_clear,
        "quality_state": state,
        "quality_issues": tuple(issues),
        "source_checksums": sources,
        "assembled_at": assembled,
    }
    return CanonicalGame(
        contract_version=CANONICAL_GAME_VERSION,
        run_id=run_id,
        event_id=event_id,
        requested_date=requested_date,
        commence_time=commence,
        raw_home_team=raw_home,
        raw_away_team=raw_away,
        home_team_key=home_key,
        away_team_key=away_key,
        venue_context=venue_data,
        roof_context=roof,
        odds_consensus=odds_copy,
        weather_context=weather,
        weather_gate_clear=weather_gate_clear,
        quality_state=state,
        quality_issues=tuple(issues),
        source_checksums=sources,
        assembled_at=assembled,
        checksum=checksum_payload(unsigned),
    )


def assemble_features(game: CanonicalGame, *, generated_at: datetime | str) -> MlbFeatures:
    generated = utc_datetime(generated_at, field="generated_at")
    markets = game.odds_consensus.get("markets")
    h2h = markets.get("h2h", {}) if isinstance(markets, Mapping) else {}
    market_context = {
        "market": "h2h",
        "consensus_contract_version": game.odds_consensus.get("contract_version"),
        "h2h": deepcopy(h2h),
        "retrieval_summary": deepcopy(game.odds_consensus.get("retrieval_summary", {})),
    }
    schedule = {
        "requested_date": game.requested_date.isoformat(),
        "commence_time": game.commence_time.isoformat(),
        "pregame_at_generation": generated < game.commence_time,
    }
    teams = {
        "home_team_key": game.home_team_key,
        "away_team_key": game.away_team_key,
        "raw_home_team": game.raw_home_team,
        "raw_away_team": game.raw_away_team,
    }
    flags = tuple(issue.code for issue in game.quality_issues)
    unsigned = {
        "contract_version": FEATURE_VERSION,
        "event_id": game.event_id,
        "canonical_game_checksum": game.checksum,
        "schedule_context": schedule,
        "team_context": teams,
        "venue_context": deepcopy(dict(game.venue_context)),
        "weather_context": deepcopy(dict(game.weather_context)),
        "market_context": market_context,
        "data_quality_state": game.quality_state,
        "uncertainty_flags": flags,
        "generated_at": generated,
    }
    return MlbFeatures(
        contract_version=FEATURE_VERSION,
        event_id=game.event_id,
        canonical_game_checksum=game.checksum,
        schedule_context=schedule,
        team_context=teams,
        venue_context=deepcopy(dict(game.venue_context)),
        weather_context=deepcopy(dict(game.weather_context)),
        market_context=market_context,
        data_quality_state=game.quality_state,
        uncertainty_flags=flags,
        generated_at=generated,
        checksum=checksum_payload(unsigned),
    )
