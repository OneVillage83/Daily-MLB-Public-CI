from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from app.baseball_intelligence.contracts import (
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceGameV1,
    TeamBaseballIntelligenceV1,
)
from app.daily_slate.contracts import DailySlateGameStatus, DailySlateGameV1, DailySlateV1
from app.data_quality.contracts import (
    DataQualityContractError,
    DataQualityDisposition,
    DataQualityGameV1,
    DataQualityPolicyV1,
    DataQualityV1,
    QualityDomain,
    QualityIssueSeverity,
    QualityIssueV1,
)
from app.game_state.contracts import (
    GameStateGameV1,
    GameStateV1,
    LineupAvailability,
    StarterCertainty,
    TeamGameStateV1,
)
from app.odds_weather.contracts import (
    OddsAvailability,
    OddsWeatherGameV1,
    OddsWeatherV1,
    WeatherProvider,
    WeatherRelevance,
    WeatherStatus,
)


class DataQualityAssessmentError(RuntimeError):
    """Fail-closed error for structurally inconsistent upstream canonical evidence."""


@dataclass(frozen=True, slots=True)
class DataQualityAssessmentResultV1:
    snapshot: DataQualityV1


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DataQualityAssessmentError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _issue(
    code: str,
    domain: QualityDomain,
    severity: QualityIssueSeverity,
    message: str,
    *,
    team_id: str | None = None,
    provider: str | None = None,
    expected: object | None = None,
    actual: object | None = None,
) -> QualityIssueV1:
    return QualityIssueV1(
        code=code,
        domain=domain,
        severity=severity,
        message=message,
        team_id=team_id,
        provider=provider,
        expected=None if expected is None else str(expected),
        actual=None if actual is None else str(actual),
    )


def _disposition(issues: list[QualityIssueV1]) -> DataQualityDisposition:
    if any(issue.severity is QualityIssueSeverity.CRITICAL for issue in issues):
        return DataQualityDisposition.INSUFFICIENT
    if any(issue.severity is QualityIssueSeverity.WARNING for issue in issues):
        return DataQualityDisposition.DEGRADED
    return DataQualityDisposition.READY


def _validate_snapshot_chain(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_weather: OddsWeatherV1,
) -> None:
    requested_dates = {
        slate.requested_date,
        game_state.requested_date,
        baseball_intelligence.requested_date,
        odds_weather.requested_date,
    }
    if len(requested_dates) != 1:
        raise DataQualityAssessmentError("upstream requested_date values disagree")
    if not (
        slate.as_of_time
        == game_state.as_of_time
        == baseball_intelligence.as_of_time
        == odds_weather.as_of_time
    ):
        raise DataQualityAssessmentError("upstream as_of_time values disagree")
    if game_state.upstream_daily_slate_checksum != slate.checksum:
        raise DataQualityAssessmentError(
            "GameState does not reference the supplied DailySlate checksum"
        )
    if baseball_intelligence.upstream_daily_slate_checksum != slate.checksum:
        raise DataQualityAssessmentError(
            "Baseball Intelligence does not reference the supplied DailySlate checksum"
        )
    if baseball_intelligence.upstream_game_state_checksum != game_state.checksum:
        raise DataQualityAssessmentError(
            "Baseball Intelligence does not reference the supplied GameState checksum"
        )
    if odds_weather.upstream_daily_slate_checksum != slate.checksum:
        raise DataQualityAssessmentError(
            "Odds + Weather does not reference the supplied DailySlate checksum"
        )
    if (
        odds_weather.upstream_baseball_intelligence_checksum
        != baseball_intelligence.checksum
    ):
        raise DataQualityAssessmentError(
            "Odds + Weather does not reference the supplied Baseball Intelligence checksum"
        )
    inventories = (
        tuple(game.source_game_id for game in slate.games),
        tuple(game.source_game_id for game in game_state.games),
        tuple(game.source_game_id for game in baseball_intelligence.games),
        tuple(game.source_game_id for game in odds_weather.games),
    )
    if len(set(inventories)) != 1:
        raise DataQualityAssessmentError(
            "upstream game ordering/set must match exactly across phases 1 through 4"
        )


def _validate_game_chain(
    *,
    slate_game: DailySlateGameV1,
    state_game: GameStateGameV1,
    intelligence_game: BaseballIntelligenceGameV1,
    odds_weather_game: OddsWeatherGameV1,
) -> None:
    identities = (
        (
            slate_game.edge_event_id,
            slate_game.daily_mlb_game_id,
            slate_game.source_game_id,
            slate_game.away_team_id,
            slate_game.home_team_id,
        ),
        (
            state_game.edge_event_id,
            state_game.daily_mlb_game_id,
            state_game.source_game_id,
            state_game.away_team_id,
            state_game.home_team_id,
        ),
        (
            intelligence_game.edge_event_id,
            intelligence_game.daily_mlb_game_id,
            intelligence_game.source_game_id,
            intelligence_game.away_team_id,
            intelligence_game.home_team_id,
        ),
        (
            odds_weather_game.edge_event_id,
            odds_weather_game.daily_mlb_game_id,
            odds_weather_game.source_game_id,
            odds_weather_game.away_team_id,
            odds_weather_game.home_team_id,
        ),
    )
    if len(set(identities)) != 1:
        raise DataQualityAssessmentError(
            "upstream canonical game identities disagree across phases 1 through 4"
        )
    if intelligence_game.upstream_daily_slate_game_checksum != slate_game.checksum:
        raise DataQualityAssessmentError(
            "Baseball Intelligence game DailySlate checksum mismatch"
        )
    if intelligence_game.upstream_game_state_game_checksum != state_game.checksum:
        raise DataQualityAssessmentError(
            "Baseball Intelligence game GameState checksum mismatch"
        )
    if odds_weather_game.upstream_daily_slate_game_checksum != slate_game.checksum:
        raise DataQualityAssessmentError(
            "Odds + Weather game DailySlate checksum mismatch"
        )
    if (
        odds_weather_game.upstream_baseball_intelligence_game_checksum
        != intelligence_game.checksum
    ):
        raise DataQualityAssessmentError(
            "Odds + Weather game Baseball Intelligence checksum mismatch"
        )
    if intelligence_game.game_status is not state_game.game_status:
        raise DataQualityAssessmentError(
            "Baseball Intelligence game_status must match GameState"
        )
    if odds_weather_game.scheduled_start_time != slate_game.scheduled_start_time:
        raise DataQualityAssessmentError(
            "Odds + Weather scheduled_start_time must match DailySlate"
        )


def _schedule_issues(
    slate_game: DailySlateGameV1,
    state_game: GameStateGameV1,
) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    if slate_game.scheduled_start_time is None:
        issues.append(
            _issue(
                "scheduled_start_time_missing",
                QualityDomain.SCHEDULE,
                QualityIssueSeverity.CRITICAL,
                "The canonical slate has no scheduled first-pitch time.",
                expected="timezone-aware first-pitch timestamp",
                actual="unavailable",
            )
        )
    status = state_game.game_status
    critical_status_codes = {
        DailySlateGameStatus.CANCELLED: "game_cancelled",
        DailySlateGameStatus.POSTPONED: "game_postponed",
        DailySlateGameStatus.SUSPENDED: "game_suspended",
        DailySlateGameStatus.IN_PROGRESS: "game_already_in_progress",
        DailySlateGameStatus.FINAL: "game_already_final",
    }
    if status in critical_status_codes:
        issues.append(
            _issue(
                critical_status_codes[status],
                QualityDomain.SCHEDULE,
                QualityIssueSeverity.CRITICAL,
                "Current authoritative game status is not a normal pregame state.",
                expected="scheduled or pregame",
                actual=status.value,
            )
        )
    elif status is DailySlateGameStatus.DELAYED:
        issues.append(
            _issue(
                "game_delayed",
                QualityDomain.SCHEDULE,
                QualityIssueSeverity.WARNING,
                "Current authoritative game status is delayed.",
                expected="scheduled or pregame",
                actual=status.value,
            )
        )
    elif status is DailySlateGameStatus.UNKNOWN:
        issues.append(
            _issue(
                "game_status_unknown",
                QualityDomain.SCHEDULE,
                QualityIssueSeverity.WARNING,
                "Current authoritative game status is unknown.",
                expected="known MLB game status",
                actual=status.value,
            )
        )
    return issues


def _team_state_issues(state_team: TeamGameStateV1) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    team_id = state_team.team_id
    certainty = state_team.starter.certainty
    if certainty is StarterCertainty.UNAVAILABLE:
        issues.append(
            _issue(
                "starter_unavailable",
                QualityDomain.STARTER,
                QualityIssueSeverity.CRITICAL,
                "No authoritative starting pitcher is available for the team.",
                team_id=team_id,
                expected="probable, announced, or confirmed starter",
                actual=certainty.value,
            )
        )
    elif certainty is StarterCertainty.PROBABLE:
        issues.append(
            _issue(
                "starter_probable_not_confirmed",
                QualityDomain.STARTER,
                QualityIssueSeverity.INFO,
                "The authoritative starting pitcher is probable but not confirmed.",
                team_id=team_id,
                expected="confirmed",
                actual=certainty.value,
            )
        )
    elif certainty is StarterCertainty.ANNOUNCED:
        issues.append(
            _issue(
                "starter_announced_not_confirmed",
                QualityDomain.STARTER,
                QualityIssueSeverity.INFO,
                "The authoritative starting pitcher is announced but not yet confirmed.",
                team_id=team_id,
                expected="confirmed",
                actual=certainty.value,
            )
        )

    lineup = state_team.lineup
    if lineup.availability is LineupAvailability.UNAVAILABLE:
        issues.append(
            _issue(
                "lineup_unavailable",
                QualityDomain.LINEUP,
                QualityIssueSeverity.WARNING,
                "The authoritative batting order is not yet available.",
                team_id=team_id,
                expected="posted batting order",
                actual=lineup.availability.value,
            )
        )
    elif lineup.availability is LineupAvailability.PARTIAL:
        issues.append(
            _issue(
                "lineup_partial",
                QualityDomain.LINEUP,
                QualityIssueSeverity.WARNING,
                "Only a partial authoritative batting order is available.",
                team_id=team_id,
                expected="9 lineup entries",
                actual=len(lineup.entries),
            )
        )
    if not state_team.personnel.available:
        issues.append(
            _issue(
                "gameday_personnel_unavailable",
                QualityDomain.GAME_STATE,
                QualityIssueSeverity.WARNING,
                "Authoritative gameday personnel buckets are unavailable.",
                team_id=team_id,
                expected="available",
                actual="unavailable",
            )
        )
    return issues


def _team_intelligence_issues(
    state_team: TeamGameStateV1,
    intelligence_team: TeamBaseballIntelligenceV1,
) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    team_id = state_team.team_id
    coverage = intelligence_team.coverage

    if state_team.starter.player is not None and not coverage.starter_feature_available:
        issues.append(
            _issue(
                "starter_feature_missing",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.CRITICAL,
                "The identified starting pitcher lacks accepted V3 feature intelligence.",
                team_id=team_id,
                expected="accepted starter V3 feature",
                actual="unavailable",
            )
        )

    if coverage.gameday_player_count == 0:
        issues.append(
            _issue(
                "team_player_intelligence_unavailable",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.CRITICAL,
                "No gameday player intelligence records are available for the team.",
                team_id=team_id,
                expected="one or more gameday player records",
                actual=0,
            )
        )
    elif coverage.player_feature_count == 0:
        issues.append(
            _issue(
                "team_player_features_unavailable",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.CRITICAL,
                "Gameday personnel are known but none have accepted V3 feature intelligence.",
                team_id=team_id,
                expected="one or more player feature records",
                actual=0,
            )
        )

    unresolved_count = coverage.gameday_player_count - coverage.resolved_player_count
    if unresolved_count > 0:
        issues.append(
            _issue(
                "player_identity_coverage_incomplete",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.WARNING,
                "Some gameday players lack verified canonical player identity.",
                team_id=team_id,
                expected=coverage.gameday_player_count,
                actual=coverage.resolved_player_count,
            )
        )
    missing_feature_count = coverage.resolved_player_count - coverage.player_feature_count
    if missing_feature_count > 0:
        issues.append(
            _issue(
                "player_feature_coverage_incomplete",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.WARNING,
                "Some resolved gameday players lack accepted V3 feature intelligence.",
                team_id=team_id,
                expected=coverage.resolved_player_count,
                actual=coverage.player_feature_count,
            )
        )

    if coverage.lineup_player_count > 0:
        if coverage.lineup_feature_count == 0:
            issues.append(
                _issue(
                    "lineup_features_unavailable",
                    QualityDomain.BASEBALL_INTELLIGENCE,
                    QualityIssueSeverity.CRITICAL,
                    "Known lineup players have no accepted V3 feature intelligence.",
                    team_id=team_id,
                    expected=coverage.lineup_player_count,
                    actual=coverage.lineup_feature_count,
                )
            )
        elif coverage.lineup_feature_count < coverage.lineup_player_count:
            issues.append(
                _issue(
                    "lineup_feature_coverage_incomplete",
                    QualityDomain.BASEBALL_INTELLIGENCE,
                    QualityIssueSeverity.WARNING,
                    "Known lineup players have incomplete accepted V3 feature coverage.",
                    team_id=team_id,
                    expected=coverage.lineup_player_count,
                    actual=coverage.lineup_feature_count,
                )
            )

    if coverage.bullpen_player_count > 0:
        if coverage.bullpen_feature_count == 0:
            issues.append(
                _issue(
                    "bullpen_features_unavailable",
                    QualityDomain.BASEBALL_INTELLIGENCE,
                    QualityIssueSeverity.WARNING,
                    "Known bullpen personnel have no accepted V3 feature intelligence.",
                    team_id=team_id,
                    expected=coverage.bullpen_player_count,
                    actual=coverage.bullpen_feature_count,
                )
            )
        elif coverage.bullpen_feature_count < coverage.bullpen_player_count:
            issues.append(
                _issue(
                    "bullpen_feature_coverage_incomplete",
                    QualityDomain.BASEBALL_INTELLIGENCE,
                    QualityIssueSeverity.WARNING,
                    "Known bullpen personnel have incomplete accepted V3 feature coverage.",
                    team_id=team_id,
                    expected=coverage.bullpen_player_count,
                    actual=coverage.bullpen_feature_count,
                )
            )

    incomplete_states = sorted(
        {
            player.feature.completeness_state
            for player in intelligence_team.players
            if player.feature is not None
            and player.feature.completeness_state.casefold() != "complete"
        }
    )
    if incomplete_states:
        issues.append(
            _issue(
                "feature_snapshot_completeness_limited",
                QualityDomain.BASEBALL_INTELLIGENCE,
                QualityIssueSeverity.WARNING,
                "One or more accepted V3 player features report a non-complete source state.",
                team_id=team_id,
                expected="complete",
                actual=",".join(incomplete_states),
            )
        )
    return issues


def _odds_issues(
    game: OddsWeatherGameV1, policy: DataQualityPolicyV1 = DataQualityPolicyV1()
) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    odds = game.odds
    if odds.availability is OddsAvailability.UNAVAILABLE:
        return [
            _issue(
                "odds_unavailable",
                QualityDomain.ODDS,
                QualityIssueSeverity.WARNING,
                "No canonical sportsbook evidence is available for this game.",
                provider="the_odds_api",
                expected="matched supported market evidence",
                actual="unavailable",
            )
        ]

    if odds.normalized_market_count == 0:
        issues.append(
            _issue(
                "odds_supported_markets_unavailable",
                QualityDomain.ODDS,
                QualityIssueSeverity.WARNING,
                "A sportsbook event was matched but no supported normalized market was retained.",
                provider="the_odds_api",
                expected="one or more h2h/spreads/totals markets",
                actual=0,
            )
        )
    summary = odds.summary
    markets: Mapping[str, object] = {}
    if summary is not None:
        raw_markets = summary.get("markets")
        if isinstance(raw_markets, Mapping):
            markets = raw_markets
    for market_key in policy.supported_markets:
        if market_key not in markets:
            issues.append(
                _issue(
                    f"{market_key}_market_missing",
                    QualityDomain.ODDS,
                    QualityIssueSeverity.WARNING,
                    "A supported sportsbook market is absent from the canonical odds summary.",
                    provider="the_odds_api",
                    expected=market_key,
                    actual="missing",
                )
            )
    stale_count = odds.freshness_counts.get("stale")
    if isinstance(stale_count, int) and stale_count > 0:
        issues.append(
            _issue(
                "stale_odds_markets_present",
                QualityDomain.ODDS,
                QualityIssueSeverity.WARNING,
                "One or more retained sportsbook markets exceed the configured freshness threshold.",
                provider="the_odds_api",
                expected=0,
                actual=stale_count,
            )
        )
    return issues


def _weather_issues(game: OddsWeatherGameV1) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    weather = game.weather
    if weather.status is WeatherStatus.INDOOR_FIXED_ROOF:
        return issues
    if weather.status is WeatherStatus.UNAVAILABLE:
        return [
            _issue(
                "weather_unavailable",
                QualityDomain.WEATHER,
                QualityIssueSeverity.WARNING,
                "Game-time weather is unavailable for a weather-relevant game.",
                expected="usable game-time weather or verified fixed roof",
                actual="unavailable",
            )
        ]

    if weather.primary_source is WeatherProvider.OPENWEATHER:
        issues.append(
            _issue(
                "nws_primary_unavailable",
                QualityDomain.WEATHER,
                QualityIssueSeverity.WARNING,
                "OpenWeather is the selected primary evidence because NWS evidence is unavailable.",
                provider="openweather",
                expected="nws primary",
                actual="openweather",
            )
        )
    agreement = weather.comparison.get("agreement")
    if agreement == "weak":
        issues.append(
            _issue(
                "weather_provider_agreement_weak",
                QualityDomain.WEATHER,
                QualityIssueSeverity.WARNING,
                "NWS and OpenWeather materially disagree on game-time conditions.",
                expected="strong or moderate",
                actual="weak",
            )
        )
    elif isinstance(agreement, str) and agreement.startswith("not_comparable"):
        issues.append(
            _issue(
                "weather_provider_comparison_unavailable",
                QualityDomain.WEATHER,
                QualityIssueSeverity.WARNING,
                "Retained weather sources cannot be compared at aligned forecast times.",
                expected="comparable provider timestamps",
                actual=agreement,
            )
        )
    elif agreement == "moderate":
        issues.append(
            _issue(
                "weather_provider_agreement_moderate",
                QualityDomain.WEATHER,
                QualityIssueSeverity.INFO,
                "NWS and OpenWeather show moderate disagreement on game-time conditions.",
                actual="moderate",
            )
        )

    if weather.relevance is WeatherRelevance.CONTEXTUAL_ROOF_STATUS_UNKNOWN:
        issues.append(
            _issue(
                "retractable_roof_status_unknown",
                QualityDomain.WEATHER,
                QualityIssueSeverity.INFO,
                "Outside weather is retained contextually because retractable-roof operational status is unknown.",
                actual=weather.relevance.value,
            )
        )
    elif weather.relevance is WeatherRelevance.CONTEXTUAL_ROOF_TYPE_UNVERIFIED:
        issues.append(
            _issue(
                "roof_type_unverified",
                QualityDomain.WEATHER,
                QualityIssueSeverity.WARNING,
                "Outside weather relevance is uncertain because roof type is not verified.",
                actual=weather.relevance.value,
            )
        )

    classification = weather.baseball_wind_impact.get("classification")
    if classification == "unknown":
        issues.append(
            _issue(
                "field_relative_wind_unavailable",
                QualityDomain.WEATHER,
                QualityIssueSeverity.INFO,
                "Field-relative wind impact is unavailable from the retained verified evidence.",
                actual=str(weather.baseball_wind_impact.get("reason_code") or "unknown"),
            )
        )
    return issues


def _game_issues(
    *,
    slate_game: DailySlateGameV1,
    state_game: GameStateGameV1,
    intelligence_game: BaseballIntelligenceGameV1,
    odds_weather_game: OddsWeatherGameV1,
    policy: DataQualityPolicyV1 = DataQualityPolicyV1(),
) -> list[QualityIssueV1]:
    issues: list[QualityIssueV1] = []
    issues.extend(_schedule_issues(slate_game, state_game))
    for state_team, intelligence_team in (
        (state_game.away, intelligence_game.away),
        (state_game.home, intelligence_game.home),
    ):
        if state_team.team_id != intelligence_team.team_id:
            raise DataQualityAssessmentError(
                "GameState/Baseball Intelligence nested team identity mismatch"
            )
        issues.extend(_team_state_issues(state_team))
        issues.extend(_team_intelligence_issues(state_team, intelligence_team))
    issues.extend(_odds_issues(odds_weather_game, policy))
    issues.extend(_weather_issues(odds_weather_game))
    return issues


def assess_data_quality(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    baseball_intelligence: BaseballIntelligenceAssemblyV1,
    odds_weather: OddsWeatherV1,
    observed_at: datetime | None = None,
    policy: DataQualityPolicyV1 = DataQualityPolicyV1(),
) -> DataQualityAssessmentResultV1:
    """Evaluate canonical phases 1-4 without deleting or filtering any game."""

    _validate_snapshot_chain(
        slate=slate,
        game_state=game_state,
        baseball_intelligence=baseball_intelligence,
        odds_weather=odds_weather,
    )
    upstream_latest = max(
        slate.observed_at,
        game_state.observed_at,
        baseball_intelligence.observed_at,
        odds_weather.observed_at,
    )
    selected_observed_at = (
        upstream_latest
        if observed_at is None
        else _aware_utc(observed_at, "observed_at")
    )
    if selected_observed_at < upstream_latest:
        raise DataQualityAssessmentError(
            "Data Quality observed_at cannot precede upstream canonical evidence"
        )

    games: list[DataQualityGameV1] = []
    for slate_game, state_game, intelligence_game, odds_weather_game in zip(
        slate.games,
        game_state.games,
        baseball_intelligence.games,
        odds_weather.games,
        strict=True,
    ):
        _validate_game_chain(
            slate_game=slate_game,
            state_game=state_game,
            intelligence_game=intelligence_game,
            odds_weather_game=odds_weather_game,
        )
        issues = _game_issues(
            slate_game=slate_game,
            state_game=state_game,
            intelligence_game=intelligence_game,
            odds_weather_game=odds_weather_game,
            policy=policy,
        )
        games.append(
            DataQualityGameV1(
                edge_event_id=slate_game.edge_event_id,
                daily_mlb_game_id=slate_game.daily_mlb_game_id,
                source_game_id=slate_game.source_game_id,
                away_team_id=slate_game.away_team_id,
                home_team_id=slate_game.home_team_id,
                scheduled_start_time=slate_game.scheduled_start_time,
                upstream_daily_slate_game_checksum=slate_game.checksum,
                upstream_game_state_game_checksum=state_game.checksum,
                upstream_baseball_intelligence_game_checksum=intelligence_game.checksum,
                upstream_odds_weather_game_checksum=odds_weather_game.checksum,
                disposition=_disposition(issues),
                issues=tuple(issues),
            )
        )
    try:
        snapshot = DataQualityV1(
            requested_date=slate.requested_date,
            as_of_time=slate.as_of_time,
            observed_at=selected_observed_at,
            upstream_daily_slate_checksum=slate.checksum,
            upstream_game_state_checksum=game_state.checksum,
            upstream_baseball_intelligence_checksum=baseball_intelligence.checksum,
            upstream_odds_weather_checksum=odds_weather.checksum,
            games=tuple(games),
            policy_version=policy.policy_version,
        )
    except DataQualityContractError as exc:
        raise DataQualityAssessmentError(
            "assembled Data Quality snapshot violates V1 contract"
        ) from exc
    return DataQualityAssessmentResultV1(snapshot=snapshot)
