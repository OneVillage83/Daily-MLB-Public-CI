from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.baseball_intelligence.contracts import (
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceGameV1,
)
from app.daily_slate.contracts import DailySlateGameV1, DailySlateV1, canonical_sha256
from app.odds_weather.contracts import (
    MAX_WEATHER_FORECAST_OFFSET_MINUTES,
    OddsAvailability,
    OddsProviderEventV1,
    OddsSnapshotV1,
    OddsWeatherContractError,
    OddsWeatherGameV1,
    OddsWeatherV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    VenueWeatherContextV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
    WeatherRelevance,
    WeatherSnapshotV1,
    WeatherStatus,
    selected_raw_capture_checksums,
    thaw_mapping,
)
from app.processors.odds_processor import (
    ConsensusThresholds,
    FreshnessThresholds,
    process_game,
)
from app.processors.weather_processor import compare, wind_impact
from app.stadiums import (
    VerificationState,
    field_verification,
    material_weather_metadata_errors,
    resolve_venue_alias,
    roof_operational_status,
    stadium_for_team,
    verified_outfield_bearing,
)

DEFAULT_ODDS_EVENT_MATCH_TOLERANCE_MINUTES = 180.0
_ASSOCIATION_FIELDS = frozenset(
    {"physical_venue_key", "active_club_association", "timezone"}
)
_COORDINATE_FIELDS = frozenset({"latitude", "longitude"})


class OddsWeatherAssemblyError(RuntimeError):
    """Fail-closed canonical Odds + Weather assembly error."""


@dataclass(frozen=True, slots=True)
class OddsWeatherAssemblyResultV1:
    snapshot: OddsWeatherV1

    @property
    def warnings(self) -> tuple[OddsWeatherWarningV1, ...]:
        return self.snapshot.warnings


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise OddsWeatherAssemblyError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _warning(
    code: str,
    domain: OddsWeatherWarningDomain,
    message: str,
    *,
    source_game_id: str | None = None,
    provider: str | None = None,
    provider_event_id: str | None = None,
) -> OddsWeatherWarningV1:
    return OddsWeatherWarningV1(
        code=code,
        domain=domain,
        message=message,
        source_game_id=source_game_id,
        provider=provider,
        provider_event_id=provider_event_id,
    )


def _material_errors_for_fields(
    stadium: Mapping[str, Any], fields: frozenset[str]
) -> tuple[str, ...]:
    return tuple(
        sorted(
            error
            for error in material_weather_metadata_errors(stadium)
            if error.rsplit(":", 1)[-1] in fields
        )
    )


def _venue_context(
    slate_game: DailySlateGameV1,
) -> VenueWeatherContextV1 | None:
    stadium = stadium_for_team(slate_game.home_team_id)
    if stadium is None:
        return None
    association_errors = list(
        _material_errors_for_fields(stadium, _ASSOCIATION_FIELDS)
    )
    if str(stadium.get("team_key") or "") != slate_game.home_team_id:
        association_errors.append("venue_metadata_invalid:active_club_team_key")

    if slate_game.source_venue_name:
        resolution = resolve_venue_alias(
            slate_game.source_venue_name,
            team_key=slate_game.home_team_id,
        )
        if resolution.get("status") not in {"current", "historical_same_venue"}:
            association_errors.append("venue_metadata_invalid:daily_slate_venue_alias")
        elif resolution.get("physical_venue_key") != stadium.get("physical_venue_key"):
            association_errors.append("venue_metadata_invalid:physical_venue_identity")

    coordinate_errors = list(
        _material_errors_for_fields(stadium, _COORDINATE_FIELDS)
    )
    roof_status_value = roof_operational_status(stadium)
    roof_status = str(getattr(roof_status_value, "value", roof_status_value))
    roof_verification = str(
        field_verification(stadium, "roof_type").get(
            "status", VerificationState.UNKNOWN.value
        )
    )
    bearing_verification = str(
        field_verification(stadium, "outfield_bearing_degrees").get(
            "status", VerificationState.UNKNOWN.value
        )
    )
    latitude = stadium.get("latitude")
    longitude = stadium.get("longitude")
    bearing = stadium.get("outfield_bearing_degrees")
    return VenueWeatherContextV1(
        team_id=slate_game.home_team_id,
        physical_venue_key=(
            str(stadium["physical_venue_key"])
            if stadium.get("physical_venue_key")
            else None
        ),
        venue_name=(
            str(stadium.get("current_display_name") or stadium.get("venue"))
            if stadium.get("current_display_name") or stadium.get("venue")
            else None
        ),
        latitude=(float(latitude) if isinstance(latitude, int | float) else None),
        longitude=(float(longitude) if isinstance(longitude, int | float) else None),
        timezone_name=(
            str(stadium["timezone"]) if stadium.get("timezone") else None
        ),
        roof_type=str(stadium.get("roof_type") or "unknown"),
        operational_roof_status=roof_status,
        roof_verification_state=roof_verification,
        outfield_bearing_degrees=(
            float(bearing) if isinstance(bearing, int | float) else None
        ),
        outfield_bearing_verification_state=bearing_verification,
        metadata_policy_version=(
            str(stadium["metadata_policy_version"])
            if stadium.get("metadata_policy_version")
            else None
        ),
        catalog_version=(
            int(stadium["catalog_version"])
            if isinstance(stadium.get("catalog_version"), int)
            else None
        ),
        association_errors=tuple(sorted(set(association_errors))),
        coordinate_errors=tuple(sorted(set(coordinate_errors))),
    )


def _same_odds_revision(
    left: OddsProviderEventV1,
    right: OddsProviderEventV1,
) -> bool:
    return canonical_sha256(left.as_dict()) == canonical_sha256(right.as_dict())


def _latest_odds_revisions(
    odds_events: Iterable[OddsProviderEventV1],
    *,
    observed_at: datetime,
    warnings: list[OddsWeatherWarningV1],
) -> tuple[OddsProviderEventV1, ...]:
    by_event: dict[str, list[OddsProviderEventV1]] = defaultdict(list)
    for event in odds_events:
        if not isinstance(event, OddsProviderEventV1):
            raise OddsWeatherAssemblyError(
                "odds_events must contain OddsProviderEventV1 values"
            )
        if event.retrieved_at > observed_at:
            warnings.append(
                _warning(
                    "future_odds_revision_ignored",
                    OddsWeatherWarningDomain.ODDS,
                    "Odds revision was retrieved after the phase observation cutoff",
                    provider="the_odds_api",
                    provider_event_id=event.provider_event_id,
                )
            )
            continue
        by_event[event.provider_event_id].append(event)

    selected: list[OddsProviderEventV1] = []
    for provider_event_id, candidates in sorted(by_event.items()):
        latest_time = max(candidate.retrieved_at for candidate in candidates)
        latest = [
            candidate for candidate in candidates if candidate.retrieved_at == latest_time
        ]
        if len(latest) > 1 and any(
            not _same_odds_revision(latest[0], candidate)
            for candidate in latest[1:]
        ):
            raise OddsWeatherAssemblyError(
                "conflicting odds revisions share one provider event/retrieval time"
            )
        selected.append(
            min(
                latest,
                key=lambda candidate: (
                    candidate.raw_capture_checksum,
                    canonical_sha256(candidate.as_dict()),
                ),
            )
        )
    return tuple(selected)


def _match_odds_events(
    games: tuple[DailySlateGameV1, ...],
    odds_events: tuple[OddsProviderEventV1, ...],
    *,
    tolerance_minutes: float,
    warnings: list[OddsWeatherWarningV1],
) -> dict[str, tuple[OddsProviderEventV1, float]]:
    if tolerance_minutes <= 0:
        raise OddsWeatherAssemblyError(
            "odds event match tolerance must be greater than zero"
        )
    assigned: dict[str, list[tuple[OddsProviderEventV1, float]]] = defaultdict(list)
    for event in odds_events:
        game_candidates: list[tuple[DailySlateGameV1, float]] = []
        for game in games:
            if (
                game.home_team_id != event.home_team_id
                or game.away_team_id != event.away_team_id
                or game.scheduled_start_time is None
            ):
                continue
            scheduled = _aware_utc(game.scheduled_start_time, "scheduled_start_time")
            offset = abs((event.commence_time - scheduled).total_seconds()) / 60.0
            if offset <= tolerance_minutes:
                game_candidates.append((game, offset))
        if not game_candidates:
            warnings.append(
                _warning(
                    "odds_event_unmatched",
                    OddsWeatherWarningDomain.ODDS_MATCHING,
                    "Provider odds event did not match a canonical DailySlate game inside the configured tolerance",
                    provider="the_odds_api",
                    provider_event_id=event.provider_event_id,
                )
            )
            continue
        minimum = min(offset for _game, offset in game_candidates)
        nearest_games = [
            (game, offset)
            for game, offset in game_candidates
            if abs(offset - minimum) <= 1e-9
        ]
        if len(nearest_games) != 1:
            raise OddsWeatherAssemblyError(
                "provider odds event is equally close to multiple canonical games"
            )
        game, offset = nearest_games[0]
        assigned[game.source_game_id].append((event, offset))

    result: dict[str, tuple[OddsProviderEventV1, float]] = {}
    for source_game_id, event_candidates in sorted(assigned.items()):
        minimum = min(offset for _event, offset in event_candidates)
        nearest_events = [
            (event, offset)
            for event, offset in event_candidates
            if abs(offset - minimum) <= 1e-9
        ]
        if len(nearest_events) != 1:
            raise OddsWeatherAssemblyError(
                "canonical game has equally close competing provider odds events"
            )
        selected_event, selected_offset = nearest_events[0]
        result[source_game_id] = (selected_event, selected_offset)
        for excluded_event, _offset in event_candidates:
            if excluded_event.provider_event_id == selected_event.provider_event_id:
                continue
            warnings.append(
                _warning(
                    "competing_odds_event_excluded",
                    OddsWeatherWarningDomain.ODDS_MATCHING,
                    "A second provider odds event mapped to the same canonical game and was excluded by nearest-start selection",
                    source_game_id=source_game_id,
                    provider="the_odds_api",
                    provider_event_id=excluded_event.provider_event_id,
                )
            )
    return result


def _same_weather_revision(
    left: WeatherForecastEvidenceV1,
    right: WeatherForecastEvidenceV1,
) -> bool:
    return canonical_sha256(left.as_dict()) == canonical_sha256(right.as_dict())


def _latest_weather_revisions(
    weather_evidence: Iterable[WeatherForecastEvidenceV1],
    *,
    known_game_ids: set[str],
    observed_at: datetime,
    warnings: list[OddsWeatherWarningV1],
) -> dict[tuple[str, WeatherProvider], WeatherForecastEvidenceV1]:
    grouped: dict[
        tuple[str, WeatherProvider], list[WeatherForecastEvidenceV1]
    ] = defaultdict(list)
    for evidence in weather_evidence:
        if not isinstance(evidence, WeatherForecastEvidenceV1):
            raise OddsWeatherAssemblyError(
                "weather_evidence must contain WeatherForecastEvidenceV1 values"
            )
        if evidence.retrieved_at > observed_at:
            warnings.append(
                _warning(
                    "future_weather_revision_ignored",
                    OddsWeatherWarningDomain.WEATHER,
                    "Weather revision was retrieved after the phase observation cutoff",
                    source_game_id=evidence.source_game_id,
                    provider=evidence.provider.value,
                )
            )
            continue
        if evidence.source_game_id not in known_game_ids:
            warnings.append(
                _warning(
                    "weather_evidence_unmatched",
                    OddsWeatherWarningDomain.WEATHER,
                    "Weather evidence references a game outside the supplied DailySlate",
                    source_game_id=evidence.source_game_id,
                    provider=evidence.provider.value,
                )
            )
            continue
        grouped[(evidence.source_game_id, evidence.provider)].append(evidence)

    result: dict[tuple[str, WeatherProvider], WeatherForecastEvidenceV1] = {}
    for key, candidates in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1].value)
    ):
        latest_time = max(candidate.retrieved_at for candidate in candidates)
        latest = [
            candidate for candidate in candidates if candidate.retrieved_at == latest_time
        ]
        if len(latest) > 1 and any(
            not _same_weather_revision(latest[0], candidate)
            for candidate in latest[1:]
        ):
            raise OddsWeatherAssemblyError(
                "conflicting weather revisions share one game/provider/retrieval time"
            )
        result[key] = min(
            latest,
            key=lambda candidate: (
                candidate.raw_capture_checksums,
                canonical_sha256(candidate.as_dict()),
            ),
        )
    return result


def _validate_weather_first_pitch(
    evidence: WeatherForecastEvidenceV1,
    scheduled_start_time: datetime,
) -> None:
    scheduled = _aware_utc(scheduled_start_time, "scheduled_start_time")
    computed = abs((evidence.forecast_time - scheduled).total_seconds()) / 60.0
    if computed > MAX_WEATHER_FORECAST_OFFSET_MINUTES + 1e-9:
        raise OddsWeatherAssemblyError(
            "weather forecast does not cover first pitch within 60 minutes"
        )
    reported = evidence.forecast.get("forecast_offset_minutes")
    if isinstance(reported, int | float) and abs(float(reported) - computed) > 0.05:
        raise OddsWeatherAssemblyError(
            "weather forecast offset disagrees with authoritative first pitch"
        )


def _unavailable_wind(reason_code: str) -> dict[str, object]:
    return {
        "classification": "unknown",
        "crosswind_component_mph": None,
        "outfield_component_mph": None,
        "reason_code": reason_code,
        "wind_to_degrees": None,
    }


def _build_weather_snapshot(
    *,
    slate_game: DailySlateGameV1,
    revisions: Mapping[
        tuple[str, WeatherProvider], WeatherForecastEvidenceV1
    ],
    warnings: list[OddsWeatherWarningV1],
) -> WeatherSnapshotV1:
    context = _venue_context(slate_game)
    if context is None:
        warnings.append(
            _warning(
                "stadium_metadata_missing",
                OddsWeatherWarningDomain.STADIUM,
                "Canonical home team has no retained stadium metadata",
                source_game_id=slate_game.source_game_id,
            )
        )
        return WeatherSnapshotV1(
            status=WeatherStatus.UNAVAILABLE,
            relevance=WeatherRelevance.UNAVAILABLE,
            venue_context=None,
            primary_source=None,
            nws=None,
            openweather=None,
            comparison={"agreement": "unavailable"},
            baseball_wind_impact=_unavailable_wind("stadium_metadata_missing"),
        )

    roof_verified = context.roof_verification_state == VerificationState.VERIFIED.value
    fixed_indoor = (
        not context.association_errors
        and context.roof_type == "fixed"
        and roof_verified
        and context.operational_roof_status == "closed"
    )
    if fixed_indoor:
        ignored = [
            provider.value
            for provider in WeatherProvider
            if (slate_game.source_game_id, provider) in revisions
        ]
        if ignored:
            warnings.append(
                _warning(
                    "weather_evidence_ignored_fixed_roof",
                    OddsWeatherWarningDomain.WEATHER,
                    "Weather evidence was supplied but is not game-relevant for a verified closed fixed roof",
                    source_game_id=slate_game.source_game_id,
                    provider=",".join(sorted(ignored)),
                )
            )
        return WeatherSnapshotV1(
            status=WeatherStatus.INDOOR_FIXED_ROOF,
            relevance=WeatherRelevance.INDOOR_SUPPRESSED,
            venue_context=context,
            primary_source=None,
            nws=None,
            openweather=None,
            comparison={"agreement": "not_applicable_fixed_roof"},
            baseball_wind_impact=_unavailable_wind("indoor_fixed_roof"),
        )

    blocking_errors = tuple(
        sorted(set(context.association_errors) | set(context.coordinate_errors))
    )
    if blocking_errors:
        warnings.append(
            _warning(
                "weather_unavailable_invalid_stadium_metadata",
                OddsWeatherWarningDomain.STADIUM,
                "Weather is unavailable because required stadium association/coordinate metadata is invalid or unverified: "
                + "; ".join(blocking_errors),
                source_game_id=slate_game.source_game_id,
            )
        )
        return WeatherSnapshotV1(
            status=WeatherStatus.UNAVAILABLE,
            relevance=WeatherRelevance.UNAVAILABLE,
            venue_context=context,
            primary_source=None,
            nws=None,
            openweather=None,
            comparison={"agreement": "unavailable"},
            baseball_wind_impact=_unavailable_wind(
                "invalid_or_unverified_stadium_metadata"
            ),
        )

    if slate_game.scheduled_start_time is None:
        warnings.append(
            _warning(
                "weather_unavailable_missing_start_time",
                OddsWeatherWarningDomain.WEATHER,
                "Weather cannot be aligned because DailySlate has no scheduled first-pitch time",
                source_game_id=slate_game.source_game_id,
            )
        )
        return WeatherSnapshotV1(
            status=WeatherStatus.UNAVAILABLE,
            relevance=WeatherRelevance.UNAVAILABLE,
            venue_context=context,
            primary_source=None,
            nws=None,
            openweather=None,
            comparison={"agreement": "unavailable"},
            baseball_wind_impact=_unavailable_wind("missing_scheduled_start_time"),
        )

    nws = revisions.get((slate_game.source_game_id, WeatherProvider.NWS))
    openweather = revisions.get(
        (slate_game.source_game_id, WeatherProvider.OPENWEATHER)
    )
    for evidence in (nws, openweather):
        if evidence is not None:
            _validate_weather_first_pitch(evidence, slate_game.scheduled_start_time)
    if nws is None and openweather is None:
        warnings.append(
            _warning(
                "weather_forecast_missing",
                OddsWeatherWarningDomain.WEATHER,
                "No retained game-time weather forecast is available",
                source_game_id=slate_game.source_game_id,
            )
        )
        return WeatherSnapshotV1(
            status=WeatherStatus.UNAVAILABLE,
            relevance=WeatherRelevance.UNAVAILABLE,
            venue_context=context,
            primary_source=None,
            nws=None,
            openweather=None,
            comparison={"agreement": "unavailable"},
            baseball_wind_impact=_unavailable_wind("weather_forecast_missing"),
        )

    if nws is None or openweather is None:
        missing = "nws" if nws is None else "openweather"
        warnings.append(
            _warning(
                "weather_secondary_source_missing",
                OddsWeatherWarningDomain.WEATHER,
                f"Only one game-time weather provider is available; missing {missing}",
                source_game_id=slate_game.source_game_id,
                provider=missing,
            )
        )

    primary = nws or openweather
    assert primary is not None
    stadium = stadium_for_team(slate_game.home_team_id)
    assert stadium is not None
    bearing_verification = str(
        field_verification(stadium, "outfield_bearing_degrees").get(
            "status", VerificationState.UNKNOWN.value
        )
    )
    bearing = verified_outfield_bearing(stadium)
    wind = wind_impact(
        primary.forecast.get("wind_direction_deg"),
        primary.forecast.get("wind_speed_mph"),
        bearing,
        bearing_verification_state=bearing_verification,
    )
    relevance = WeatherRelevance.DIRECT
    if context.roof_type == "retractable":
        relevance = WeatherRelevance.CONTEXTUAL_ROOF_STATUS_UNKNOWN
    elif not roof_verified:
        relevance = WeatherRelevance.CONTEXTUAL_ROOF_TYPE_UNVERIFIED

    return WeatherSnapshotV1(
        status=WeatherStatus.AVAILABLE,
        relevance=relevance,
        venue_context=context,
        primary_source=(
            WeatherProvider.NWS if nws is not None else WeatherProvider.OPENWEATHER
        ),
        nws=nws,
        openweather=openweather,
        comparison=compare(
            None if nws is None else thaw_mapping(nws.forecast),
            None if openweather is None else thaw_mapping(openweather.forecast),
        ),
        baseball_wind_impact=wind,
    )


def _build_odds_snapshot(
    *,
    slate_game: DailySlateGameV1,
    selected: tuple[OddsProviderEventV1, float] | None,
    freshness_thresholds: FreshnessThresholds,
    consensus_thresholds: ConsensusThresholds,
    warnings: list[OddsWeatherWarningV1],
) -> OddsSnapshotV1:
    if slate_game.scheduled_start_time is None:
        warnings.append(
            _warning(
                "odds_unavailable_missing_start_time",
                OddsWeatherWarningDomain.ODDS_MATCHING,
                "Odds cannot be aligned because DailySlate has no scheduled first-pitch time",
                source_game_id=slate_game.source_game_id,
                provider="the_odds_api",
            )
        )
        return OddsSnapshotV1(
            availability=OddsAvailability.UNAVAILABLE,
            provider_event_id=None,
            retrieved_at=None,
            raw_capture_checksum=None,
            event_match_offset_minutes=None,
            summary=None,
            normalized_market_count=0,
            raw_snapshot_count=0,
            freshness_counts={},
        )

    if selected is None:
        warnings.append(
            _warning(
                "odds_event_missing",
                OddsWeatherWarningDomain.ODDS_MATCHING,
                "No provider odds event matched this canonical DailySlate game",
                source_game_id=slate_game.source_game_id,
                provider="the_odds_api",
            )
        )
        return OddsSnapshotV1(
            availability=OddsAvailability.UNAVAILABLE,
            provider_event_id=None,
            retrieved_at=None,
            raw_capture_checksum=None,
            event_match_offset_minutes=None,
            summary=None,
            normalized_market_count=0,
            raw_snapshot_count=0,
            freshness_counts={},
        )

    event, offset = selected
    processed = process_game(
        event.mutable_event(),
        run_id=f"odds_weather:{slate_game.source_game_id}",
        retrieved_at=event.retrieved_at.isoformat(),
        freshness_thresholds=freshness_thresholds,
        consensus_thresholds=consensus_thresholds,
        history_rows=event.mutable_history_rows(),
    )
    for item in processed.warnings:
        warnings.append(
            _warning(
                str(item.get("code") or "odds_warning"),
                OddsWeatherWarningDomain.ODDS,
                str(item.get("message") or "Odds processor warning"),
                source_game_id=slate_game.source_game_id,
                provider=(
                    str(item.get("bookmaker"))
                    if item.get("bookmaker")
                    else "the_odds_api"
                ),
                provider_event_id=event.provider_event_id,
            )
        )
    try:
        return OddsSnapshotV1(
            availability=OddsAvailability.AVAILABLE,
            provider_event_id=event.provider_event_id,
            retrieved_at=event.retrieved_at,
            raw_capture_checksum=event.raw_capture_checksum,
            event_match_offset_minutes=round(offset, 4),
            summary=processed.summary,
            normalized_market_count=processed.normalized_market_count,
            raw_snapshot_count=processed.raw_snapshot_count,
            freshness_counts=processed.freshness_counts,
        )
    except OddsWeatherContractError as exc:
        raise OddsWeatherAssemblyError(
            "frozen odds processor output violates canonical phase-4 contract"
        ) from exc


def _validate_upstream_game(
    slate_game: DailySlateGameV1,
    intelligence_game: BaseballIntelligenceGameV1,
) -> None:
    comparisons = (
        ("edge_event_id", slate_game.edge_event_id, intelligence_game.edge_event_id),
        (
            "daily_mlb_game_id",
            slate_game.daily_mlb_game_id,
            intelligence_game.daily_mlb_game_id,
        ),
        ("source_game_id", slate_game.source_game_id, intelligence_game.source_game_id),
        ("away_team_id", slate_game.away_team_id, intelligence_game.away_team_id),
        ("home_team_id", slate_game.home_team_id, intelligence_game.home_team_id),
    )
    mismatches = [name for name, left, right in comparisons if left != right]
    if mismatches:
        raise OddsWeatherAssemblyError(
            "DailySlate/Baseball Intelligence game identity mismatch: "
            + ", ".join(mismatches)
        )


def assemble_odds_weather(
    *,
    slate: DailySlateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_events: Iterable[OddsProviderEventV1] = (),
    weather_evidence: Iterable[WeatherForecastEvidenceV1] = (),
    source_warnings: Iterable[OddsWeatherWarningV1] = (),
    observed_at: datetime | None = None,
    odds_event_match_tolerance_minutes: float = DEFAULT_ODDS_EVENT_MATCH_TOLERANCE_MINUTES,
    freshness_thresholds: FreshnessThresholds | None = None,
    consensus_thresholds: ConsensusThresholds | None = None,
) -> OddsWeatherAssemblyResultV1:
    if slate.requested_date != baseball_intelligence.requested_date:
        raise OddsWeatherAssemblyError(
            "DailySlate and Baseball Intelligence requested dates disagree"
        )
    if baseball_intelligence.upstream_daily_slate_checksum != slate.checksum:
        raise OddsWeatherAssemblyError(
            "Baseball Intelligence does not reference the supplied DailySlate checksum"
        )
    if slate.as_of_time != baseball_intelligence.as_of_time:
        raise OddsWeatherAssemblyError(
            "DailySlate and Baseball Intelligence as_of_time values disagree"
        )
    slate_ids = tuple(game.source_game_id for game in slate.games)
    intelligence_ids = tuple(
        game.source_game_id for game in baseball_intelligence.games
    )
    if slate_ids != intelligence_ids:
        raise OddsWeatherAssemblyError(
            "DailySlate and Baseball Intelligence game ordering/set must match exactly"
        )
    for slate_game, intelligence_game in zip(
        slate.games, baseball_intelligence.games, strict=True
    ):
        _validate_upstream_game(slate_game, intelligence_game)

    odds_inventory = tuple(odds_events)
    weather_inventory = tuple(weather_evidence)
    source_warning_inventory = tuple(source_warnings)
    if any(
        not isinstance(warning, OddsWeatherWarningV1)
        for warning in source_warning_inventory
    ):
        raise OddsWeatherAssemblyError(
            "source_warnings must contain OddsWeatherWarningV1 values"
        )
    upstream_latest = max(slate.observed_at, baseball_intelligence.observed_at)
    if observed_at is None:
        evidence_times = [
            *(event.retrieved_at for event in odds_inventory),
            *(evidence.retrieved_at for evidence in weather_inventory),
        ]
        selected_observed_at = max([upstream_latest, *evidence_times])
    else:
        selected_observed_at = _aware_utc(observed_at, "observed_at")
        if selected_observed_at < upstream_latest:
            raise OddsWeatherAssemblyError(
                "phase observation time cannot precede upstream canonical evidence"
            )

    warnings: list[OddsWeatherWarningV1] = list(source_warning_inventory)
    selected_odds_revisions = _latest_odds_revisions(
        odds_inventory,
        observed_at=selected_observed_at,
        warnings=warnings,
    )
    matched_odds = _match_odds_events(
        tuple(slate.games),
        selected_odds_revisions,
        tolerance_minutes=odds_event_match_tolerance_minutes,
        warnings=warnings,
    )
    selected_weather = _latest_weather_revisions(
        weather_inventory,
        known_game_ids=set(slate_ids),
        observed_at=selected_observed_at,
        warnings=warnings,
    )
    effective_freshness = freshness_thresholds or FreshnessThresholds()
    effective_consensus = consensus_thresholds or ConsensusThresholds()

    games: list[OddsWeatherGameV1] = []
    raw_checksums: set[str] = set()
    for slate_game, intelligence_game in zip(
        slate.games, baseball_intelligence.games, strict=True
    ):
        odds = _build_odds_snapshot(
            slate_game=slate_game,
            selected=matched_odds.get(slate_game.source_game_id),
            freshness_thresholds=effective_freshness,
            consensus_thresholds=effective_consensus,
            warnings=warnings,
        )
        weather = _build_weather_snapshot(
            slate_game=slate_game,
            revisions=selected_weather,
            warnings=warnings,
        )
        raw_checksums.update(selected_raw_capture_checksums(odds, weather))
        games.append(
            OddsWeatherGameV1(
                edge_event_id=slate_game.edge_event_id,
                daily_mlb_game_id=slate_game.daily_mlb_game_id,
                source_game_id=slate_game.source_game_id,
                away_team_id=slate_game.away_team_id,
                home_team_id=slate_game.home_team_id,
                scheduled_start_time=slate_game.scheduled_start_time,
                upstream_daily_slate_game_checksum=slate_game.checksum,
                upstream_baseball_intelligence_game_checksum=intelligence_game.checksum,
                odds=odds,
                weather=weather,
            )
        )

    ordered_warnings = tuple(
        sorted(
            warnings,
            key=lambda item: (
                item.source_game_id or "",
                item.domain.value,
                item.code,
                item.provider or "",
                item.provider_event_id or "",
                item.message,
            ),
        )
    )
    try:
        snapshot = OddsWeatherV1(
            requested_date=slate.requested_date,
            as_of_time=slate.as_of_time,
            observed_at=selected_observed_at,
            upstream_daily_slate_checksum=slate.checksum,
            upstream_baseball_intelligence_checksum=baseball_intelligence.checksum,
            source_raw_capture_checksums=tuple(sorted(raw_checksums)),
            games=tuple(games),
            warnings=ordered_warnings,
        )
    except OddsWeatherContractError as exc:
        raise OddsWeatherAssemblyError(
            "assembled Odds + Weather snapshot violates V1 contract"
        ) from exc
    return OddsWeatherAssemblyResultV1(snapshot=snapshot)
