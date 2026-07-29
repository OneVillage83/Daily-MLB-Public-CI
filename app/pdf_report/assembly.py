from __future__ import annotations

from datetime import datetime

from app.baseball_intelligence.contracts import TeamBaseballIntelligenceV1
from app.data_quality.contracts import DataQualityDisposition
from app.matchup_packet.contracts import MatchupPacketGameV1, MatchupPacketV1
from app.odds_weather.contracts import OddsAvailability, WeatherStatus
from app.pdf_report.contracts import (
    PdfReportContractError,
    PdfReportDecisionRowV1,
    PdfReportDocumentV1,
    PdfReportGameDossierV1,
    ReportFactV1,
)
from app.pdf_report.policy import PdfReportPolicyV1
from app.predictions.contracts import GamePredictionV1, PredictionsV1
from app.rankings.contracts import RankingEntryV1, RankingsV1
from app.recommendation_gate.contracts import (
    RecommendationDecision,
    RecommendationGateV1,
    RecommendationReason,
)

_REASON_EXPLANATIONS: dict[RecommendationReason, str] = {
    RecommendationReason.QUALIFIED_BET: (
        "Meets the automated BET policy floors at the analyzed price, but still "
        "requires human review before publication."
    ),
    RecommendationReason.QUALIFIED_LEAN: (
        "Shows positive value with usable evidence, but does not meet every BET "
        "requirement and remains a LEAN."
    ),
    RecommendationReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE: (
        "The model produced a prediction, but its current manifest is not approved "
        "for betting recommendations."
    ),
    RecommendationReason.MODEL_NOT_PRODUCTION: (
        "The model is not deployed as a production recommendation model."
    ),
    RecommendationReason.MODEL_NOT_CALIBRATED: (
        "The model is not currently approved as calibrated for recommendation use."
    ),
    RecommendationReason.DATA_QUALITY_INSUFFICIENT: (
        "Critical evidence is missing or unsafe, so the market is marked AVOID."
    ),
    RecommendationReason.CALCULATION_INCOMPLETE: (
        "The value calculation is incomplete and cannot support an actionable call."
    ),
    RecommendationReason.INCOMPLETE_TWO_WAY_MARKET: (
        "A complete two-sided market was not available for a reliable no-vig comparison."
    ),
    RecommendationReason.MISSING_NO_VIG_PROBABILITY: (
        "The market could not produce a valid no-vig probability comparison."
    ),
    RecommendationReason.MARKET_STALE: (
        "The analyzed price is stale, so the market is marked AVOID rather than promoted."
    ),
    RecommendationReason.MARKET_FRESHNESS_UNKNOWN: (
        "Market freshness cannot be verified, so the evidence is not safe to act on."
    ),
    RecommendationReason.MARKET_AGING: (
        "The market is aging and carries additional timing risk."
    ),
    RecommendationReason.INSUFFICIENT_MARKET_COVERAGE: (
        "Too few books contributed to the market for a sufficiently reliable comparison."
    ),
    RecommendationReason.INSUFFICIENT_MODEL_MARKET_DISAGREEMENT: (
        "The model and no-vig market are too close to justify an edge-based recommendation."
    ),
    RecommendationReason.INSUFFICIENT_EXPECTED_VALUE: (
        "Expected value does not meet the policy floor for an actionable recommendation."
    ),
    RecommendationReason.INSUFFICIENT_EVIDENCE_CONFIDENCE: (
        "The retained evidence-confidence score is below the actionable threshold."
    ),
    RecommendationReason.BET_FLOOR_NOT_MET: (
        "At least one BET floor was not met, so the market cannot be promoted to BET."
    ),
}


def _fact(label: str, value: object, *, status: str = "available") -> ReportFactV1:
    text = "Unavailable" if value is None or value == "" else str(value)
    return ReportFactV1(label=label, value=text, status=status)


def _pct(value: float | None, digits: int = 1) -> str:
    return "Unavailable" if value is None else f"{value * 100:.{digits}f}%"


def _price(value: float) -> str:
    return f"+{int(value)}" if value > 0 else str(int(value))


def _line_label(entry: RankingEntryV1) -> str:
    if entry.market_line is None:
        return entry.market.value
    return f"{entry.market.value} {entry.market_line:+g}"


def _primary_reason(entry: RankingEntryV1) -> RecommendationReason:
    preferred = (
        RecommendationReason.QUALIFIED_BET,
        RecommendationReason.QUALIFIED_LEAN,
        RecommendationReason.DATA_QUALITY_INSUFFICIENT,
        RecommendationReason.MARKET_STALE,
        RecommendationReason.MARKET_FRESHNESS_UNKNOWN,
        RecommendationReason.INCOMPLETE_TWO_WAY_MARKET,
        RecommendationReason.INSUFFICIENT_MARKET_COVERAGE,
        RecommendationReason.INSUFFICIENT_MODEL_MARKET_DISAGREEMENT,
        RecommendationReason.INSUFFICIENT_EXPECTED_VALUE,
        RecommendationReason.MODEL_NOT_RECOMMENDATION_ELIGIBLE,
    )
    for reason in preferred:
        if reason in entry.reasons:
            return reason
    return entry.reasons[0]


def _customer_explanation(entry: RankingEntryV1) -> tuple[str, RecommendationReason]:
    primary = _primary_reason(entry)
    sentence = _REASON_EXPLANATIONS[primary]
    additional = tuple(reason for reason in entry.reasons if reason is not primary)
    if additional:
        labels = ", ".join(reason.value.replace("_", " ") for reason in additional)
        sentence = f"{sentence} Additional retained flags: {labels}."
    return sentence, primary


def _player_names(
    team: TeamBaseballIntelligenceV1,
    source_ids: tuple[str, ...],
) -> tuple[str, ...]:
    by_id = {player.source_player_id: player.full_name for player in team.players}
    return tuple(by_id.get(source_id, f"Player {source_id}") for source_id in source_ids)


def _decision_row(
    entry: RankingEntryV1,
    packet_game: MatchupPacketGameV1,
) -> PdfReportDecisionRowV1:
    explanation, primary = _customer_explanation(entry)
    return PdfReportDecisionRowV1(
        upstream_ranking_entry_checksum=entry.checksum,
        upstream_recommendation_checksum=entry.upstream_recommendation_checksum,
        edge_event_id=entry.edge_event_id,
        daily_mlb_game_id=entry.daily_mlb_game_id,
        source_game_id=entry.source_game_id,
        away_team_id=entry.away_team_id,
        home_team_id=entry.home_team_id,
        market=entry.market,
        line_key=entry.line_key,
        side=entry.side,
        market_line=entry.market_line,
        american_price=entry.american_price,
        decision=entry.decision,
        top_confidence_rank=entry.top_confidence_rank,
        best_value_rank=entry.best_value_rank,
        conditional_model_probability=entry.conditional_model_probability,
        no_vig_probability=entry.no_vig_probability,
        no_vig_probability_edge=entry.no_vig_probability_edge,
        expected_value_per_unit=entry.expected_value_per_unit,
        expected_roi_percent=entry.expected_roi_percent,
        evidence_confidence_score=entry.evidence_confidence_score,
        operational_risk_score=entry.operational_risk_score,
        bookmaker_count=entry.bookmaker_count,
        freshness_status=entry.freshness_status,
        quality_disposition=packet_game.data_quality.disposition,
        primary_reason_code=primary.value,
        reason_codes=tuple(reason.value for reason in entry.reasons),
        customer_explanation=explanation,
        odds_retrieved_at=packet_game.odds_weather.odds.retrieved_at,
        review_required=entry.review_required,
        publication_candidate=entry.publication_candidate,
    )


def _game_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    schedule = game.schedule
    context = game.odds_weather.weather.venue_context
    venue_name = (
        context.venue_name
        if context is not None and context.venue_name is not None
        else schedule.source_venue_name
        or schedule.venue_id
    )
    roof = None if context is None else context.roof_type
    roof_status = None if context is None else context.operational_roof_status
    return (
        _fact("Matchup", f"{game.away_team_id.upper()} at {game.home_team_id.upper()}"),
        _fact(
            "First pitch",
            None
            if schedule.scheduled_start_time is None
            else schedule.scheduled_start_time.isoformat(),
            status="unavailable" if schedule.scheduled_start_time is None else "available",
        ),
        _fact("Game status", schedule.game_status.value),
        _fact(
            "Park",
            venue_name,
            status="unavailable" if venue_name is None else "available",
        ),
        _fact("Roof type", roof, status="unavailable" if roof is None else "available"),
        _fact(
            "Operational roof status",
            roof_status,
            status="unavailable" if roof_status is None else "available",
        ),
        _fact("Data Quality", game.data_quality.disposition.value),
    )


def _starter_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    schedule = game.schedule
    away = schedule.away_probable_starter
    home = schedule.home_probable_starter
    away_available = game.baseball_intelligence.away.coverage.starter_feature_available
    home_available = game.baseball_intelligence.home.coverage.starter_feature_available
    return (
        _fact(
            f"{game.away_team_id.upper()} probable starter",
            None if away is None else away.full_name,
            status="unavailable" if away is None else "available",
        ),
        _fact(
            f"{game.away_team_id.upper()} starter feature",
            "Available" if away_available else "Unavailable",
            status="available" if away_available else "unavailable",
        ),
        _fact(
            f"{game.home_team_id.upper()} probable starter",
            None if home is None else home.full_name,
            status="unavailable" if home is None else "available",
        ),
        _fact(
            f"{game.home_team_id.upper()} starter feature",
            "Available" if home_available else "Unavailable",
            status="available" if home_available else "unavailable",
        ),
    )


def _lineup_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    away = game.baseball_intelligence.away
    home = game.baseball_intelligence.home
    away_names = _player_names(away, away.lineup_source_player_ids)
    home_names = _player_names(home, home.lineup_source_player_ids)
    return (
        _fact(
            f"{game.away_team_id.upper()} lineup",
            ", ".join(away_names) if away_names else None,
            status="unavailable" if not away_names else "available",
        ),
        _fact(
            f"{game.away_team_id.upper()} lineup feature coverage",
            f"{away.coverage.lineup_feature_count}/{away.coverage.lineup_player_count}",
            status=(
                "available"
                if away.coverage.lineup_feature_count == away.coverage.lineup_player_count
                else "degraded"
            ),
        ),
        _fact(
            f"{game.home_team_id.upper()} lineup",
            ", ".join(home_names) if home_names else None,
            status="unavailable" if not home_names else "available",
        ),
        _fact(
            f"{game.home_team_id.upper()} lineup feature coverage",
            f"{home.coverage.lineup_feature_count}/{home.coverage.lineup_player_count}",
            status=(
                "available"
                if home.coverage.lineup_feature_count == home.coverage.lineup_player_count
                else "degraded"
            ),
        ),
    )


def _bullpen_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    away = game.baseball_intelligence.away
    home = game.baseball_intelligence.home
    away_names = _player_names(away, away.bullpen_source_player_ids)
    home_names = _player_names(home, home.bullpen_source_player_ids)
    return (
        _fact(
            f"{game.away_team_id.upper()} bullpen",
            ", ".join(away_names) if away_names else None,
            status="unavailable" if not away_names else "available",
        ),
        _fact(
            f"{game.away_team_id.upper()} bullpen feature coverage",
            f"{away.coverage.bullpen_feature_count}/{away.coverage.bullpen_player_count}",
            status=(
                "available"
                if away.coverage.bullpen_feature_count == away.coverage.bullpen_player_count
                else "degraded"
            ),
        ),
        _fact(
            f"{game.home_team_id.upper()} bullpen",
            ", ".join(home_names) if home_names else None,
            status="unavailable" if not home_names else "available",
        ),
        _fact(
            f"{game.home_team_id.upper()} bullpen feature coverage",
            f"{home.coverage.bullpen_feature_count}/{home.coverage.bullpen_player_count}",
            status=(
                "available"
                if home.coverage.bullpen_feature_count == home.coverage.bullpen_player_count
                else "degraded"
            ),
        ),
    )


def _weather_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    weather = game.odds_weather.weather
    evidence = weather.nws if weather.nws is not None else weather.openweather
    forecast = None if evidence is None else evidence.forecast
    return (
        _fact("Weather status", weather.status.value),
        _fact("Weather relevance", weather.relevance.value),
        _fact(
            "Primary source",
            None if weather.primary_source is None else weather.primary_source.value,
            status="unavailable" if weather.primary_source is None else "available",
        ),
        _fact(
            "Temperature",
            None if forecast is None or forecast.get("temperature_f") is None else f"{forecast.get('temperature_f')} F",
            status="unavailable" if forecast is None else "available",
        ),
        _fact(
            "Precipitation probability",
            None
            if forecast is None or forecast.get("precipitation_probability_pct") is None
            else f"{forecast.get('precipitation_probability_pct')}%",
            status="unavailable" if forecast is None else "available",
        ),
        _fact(
            "Wind",
            None
            if forecast is None or forecast.get("wind_speed_mph") is None
            else f"{forecast.get('wind_speed_mph')} mph at {forecast.get('wind_direction_deg', 'unknown')} deg",
            status="unavailable" if forecast is None else "available",
        ),
    )


def _odds_facts(game: MatchupPacketGameV1) -> tuple[ReportFactV1, ...]:
    odds = game.odds_weather.odds
    freshness = ", ".join(
        f"{key}={value}" for key, value in sorted(odds.freshness_counts.items())
    )
    return (
        _fact("Odds availability", odds.availability.value),
        _fact(
            "Odds retrieved",
            None if odds.retrieved_at is None else odds.retrieved_at.isoformat(),
            status="unavailable" if odds.retrieved_at is None else "available",
        ),
        _fact("Normalized markets", odds.normalized_market_count),
        _fact("Raw snapshots", odds.raw_snapshot_count),
        _fact(
            "Freshness counts",
            freshness or None,
            status="unavailable" if not freshness else "available",
        ),
    )


def _model_facts(prediction: GamePredictionV1) -> tuple[ReportFactV1, ...]:
    return (
        _fact("Model", f"{prediction.model_id} {prediction.model_version}"),
        _fact("Deployment", prediction.deployment_status.value),
        _fact("Calibration", prediction.calibration_status.value),
        _fact("Recommendation eligible", str(prediction.recommendation_eligible).lower()),
        _fact("Input state", prediction.input_state.value),
        _fact("Projected away runs", f"{prediction.expected_away_runs:.2f}"),
        _fact("Projected home runs", f"{prediction.expected_home_runs:.2f}"),
        _fact("Projected total", f"{prediction.expected_total_runs:.2f}"),
        _fact("Away win probability", _pct(prediction.away_win_probability)),
        _fact("Home win probability", _pct(prediction.home_win_probability)),
    )


def _limitations(game: MatchupPacketGameV1, prediction: GamePredictionV1) -> tuple[str, ...]:
    limitations: list[str] = [
        f"{issue.severity.value.upper()}: {issue.message}"
        for issue in game.data_quality.issues
    ]
    if game.schedule.away_probable_starter is None:
        limitations.append("Away probable starter was unavailable at the report cutoff.")
    if game.schedule.home_probable_starter is None:
        limitations.append("Home probable starter was unavailable at the report cutoff.")
    if not game.baseball_intelligence.away.lineup_source_player_ids:
        limitations.append("Away lineup was unavailable at the report cutoff.")
    if not game.baseball_intelligence.home.lineup_source_player_ids:
        limitations.append("Home lineup was unavailable at the report cutoff.")
    if game.odds_weather.odds.availability is OddsAvailability.UNAVAILABLE:
        limitations.append("No usable sportsbook market snapshot was available.")
    if game.odds_weather.weather.status is WeatherStatus.UNAVAILABLE:
        limitations.append("Weather evidence was unavailable or not safely associated.")
    if prediction.imputed_feature_names:
        limitations.append(
            f"The model imputed {len(prediction.imputed_feature_names)} retained input features."
        )
    return tuple(dict.fromkeys(limitations))


def _analysis(
    game: MatchupPacketGameV1,
    prediction: GamePredictionV1,
    decisions: tuple[PdfReportDecisionRowV1, ...],
) -> tuple[str, ...]:
    favorite = (
        game.home_team_id.upper()
        if prediction.home_win_probability >= prediction.away_win_probability
        else game.away_team_id.upper()
    )
    favorite_probability = max(
        prediction.home_win_probability,
        prediction.away_win_probability,
    )
    counts = {
        decision: sum(row.decision is decision for row in decisions)
        for decision in RecommendationDecision
    }
    analysis = [
        (
            f"The model makes {favorite} the more likely winner at "
            f"{favorite_probability * 100:.1f}% and projects "
            f"{prediction.expected_total_runs:.2f} total runs."
        ),
        (
            f"The Recommendation Gate produced {counts[RecommendationDecision.BET]} BET, "
            f"{counts[RecommendationDecision.LEAN]} LEAN, "
            f"{counts[RecommendationDecision.PASS]} PASS, and "
            f"{counts[RecommendationDecision.AVOID]} AVOID decisions for this game."
        ),
        (
            f"Game evidence is classified {game.data_quality.disposition.value.upper()}; "
            "the detailed decision explanations below identify the exact policy reasons."
        ),
    ]
    if not decisions:
        analysis.append(
            "No evaluable market rows were available, so the game remains informational only."
        )
    return tuple(analysis)


def _validate_inputs(
    rankings: RankingsV1,
    recommendation_gate: RecommendationGateV1,
    predictions: PredictionsV1,
    matchup_packet: MatchupPacketV1,
) -> None:
    requested_dates = {
        rankings.requested_date,
        recommendation_gate.requested_date,
        predictions.requested_date,
        matchup_packet.requested_date,
    }
    if len(requested_dates) != 1:
        raise PdfReportContractError("PDF Report inputs have different requested dates")
    as_of_times = {
        rankings.as_of_time,
        recommendation_gate.as_of_time,
        predictions.as_of_time,
        matchup_packet.as_of_time,
    }
    if len(as_of_times) != 1:
        raise PdfReportContractError("PDF Report inputs have different as_of_time values")
    if rankings.upstream_recommendation_gate_checksum != recommendation_gate.checksum:
        raise PdfReportContractError("Rankings does not reference the supplied Recommendation Gate")
    game_sets = (
        {game.source_game_id for game in rankings.games},
        {game.source_game_id for game in recommendation_gate.games},
        {game.source_game_id for game in predictions.games},
        {game.source_game_id for game in matchup_packet.games},
    )
    if len({frozenset(items) for items in game_sets}) != 1:
        raise PdfReportContractError("PDF Report inputs have different game inventories")


def assemble_pdf_report_document(
    *,
    rankings: RankingsV1,
    recommendation_gate: RecommendationGateV1,
    predictions: PredictionsV1,
    matchup_packet: MatchupPacketV1,
    policy: PdfReportPolicyV1 | None = None,
    generated_at: datetime | None = None,
    secret_values: tuple[str, ...] = (),
) -> PdfReportDocumentV1:
    _validate_inputs(rankings, recommendation_gate, predictions, matchup_packet)
    selected_policy = PdfReportPolicyV1() if policy is None else policy
    packet_by_game = {game.source_game_id: game for game in matchup_packet.games}
    prediction_by_game = {game.source_game_id: game for game in predictions.games}
    gate_by_game = {game.source_game_id: game for game in recommendation_gate.games}
    ranking_by_game = {game.source_game_id: game for game in rankings.games}

    rows: list[PdfReportDecisionRowV1] = []
    rows_by_ranking_checksum: dict[str, PdfReportDecisionRowV1] = {}
    for ranking_game in rankings.games:
        packet_game = packet_by_game[ranking_game.source_game_id]
        gate_game = gate_by_game[ranking_game.source_game_id]
        if ranking_game.upstream_recommendation_game_checksum != gate_game.checksum:
            raise PdfReportContractError("Ranking game Recommendation lineage mismatch")
        recommendations = {
            item.checksum: item for item in gate_game.recommendations
        }
        for entry in ranking_game.entries:
            recommendation = recommendations.get(entry.upstream_recommendation_checksum)
            if recommendation is None:
                raise PdfReportContractError("Ranking entry has no matching Recommendation")
            if recommendation.decision is not entry.decision:
                raise PdfReportContractError("Ranking entry changed Recommendation decision")
            row = _decision_row(entry, packet_game)
            rows.append(row)
            rows_by_ranking_checksum[entry.checksum] = row

    rows.sort(
        key=lambda row: (
            int(row.source_game_id),
            row.market.value,
            row.line_key,
            row.side.value,
            row.upstream_ranking_entry_checksum,
        )
    )

    top_rows = tuple(
        rows_by_ranking_checksum[item.ranking_entry_checksum].checksum
        for item in rankings.top_confidence.placements
    )
    best_rows = tuple(
        rows_by_ranking_checksum[item.ranking_entry_checksum].checksum
        for item in rankings.best_value.placements
    )

    dossiers: list[PdfReportGameDossierV1] = []
    for source_game_id in sorted(packet_by_game, key=int):
        packet_game = packet_by_game[source_game_id]
        prediction = prediction_by_game[source_game_id]
        gate_game = gate_by_game[source_game_id]
        ranking_game = ranking_by_game[source_game_id]
        if prediction.model_manifest_checksum != gate_game.model_manifest_checksum:
            raise PdfReportContractError("Prediction and Recommendation model lineage mismatch")
        game_rows = tuple(row for row in rows if row.source_game_id == source_game_id)
        dossiers.append(
            PdfReportGameDossierV1(
                edge_event_id=packet_game.edge_event_id,
                daily_mlb_game_id=packet_game.daily_mlb_game_id,
                source_game_id=source_game_id,
                away_team_id=packet_game.away_team_id,
                home_team_id=packet_game.home_team_id,
                title=(
                    f"{packet_game.away_team_id.upper()} at "
                    f"{packet_game.home_team_id.upper()}"
                ),
                quality_disposition=packet_game.data_quality.disposition,
                game_facts=_game_facts(packet_game),
                starter_facts=_starter_facts(packet_game),
                lineup_facts=_lineup_facts(packet_game),
                bullpen_facts=_bullpen_facts(packet_game),
                weather_facts=_weather_facts(packet_game),
                odds_facts=_odds_facts(packet_game),
                model_facts=_model_facts(prediction),
                decisions=game_rows,
                analysis=_analysis(packet_game, prediction, game_rows),
                limitations=_limitations(packet_game, prediction),
                upstream_matchup_packet_game_checksum=packet_game.checksum,
                upstream_prediction_game_checksum=prediction.checksum,
                upstream_recommendation_game_checksum=gate_game.checksum,
                upstream_ranking_game_checksum=ranking_game.checksum,
            )
        )

    decision_counts = {
        decision: sum(row.decision is decision for row in rows)
        for decision in RecommendationDecision
    }
    quality_counts = {
        disposition: sum(
            game.data_quality.disposition is disposition for game in matchup_packet.games
        )
        for disposition in DataQualityDisposition
    }
    odds_available = sum(
        game.odds_weather.odds.availability is OddsAvailability.AVAILABLE
        for game in matchup_packet.games
    )
    weather_available = sum(
        game.odds_weather.weather.status is not WeatherStatus.UNAVAILABLE
        for game in matchup_packet.games
    )
    slate_health = (
        _fact("Data cutoff", rankings.as_of_time.isoformat()),
        _fact("Games", len(matchup_packet.games)),
        _fact("Evaluated market sides", len(rows)),
        _fact("Ranked candidates", len(top_rows)),
        _fact("BET decisions", decision_counts[RecommendationDecision.BET]),
        _fact("LEAN decisions", decision_counts[RecommendationDecision.LEAN]),
        _fact("PASS decisions", decision_counts[RecommendationDecision.PASS]),
        _fact("AVOID decisions", decision_counts[RecommendationDecision.AVOID]),
        _fact("READY games", quality_counts[DataQualityDisposition.READY]),
        _fact("DEGRADED games", quality_counts[DataQualityDisposition.DEGRADED]),
        _fact("INSUFFICIENT games", quality_counts[DataQualityDisposition.INSUFFICIENT]),
        _fact("Games with odds", f"{odds_available}/{len(matchup_packet.games)}"),
        _fact("Games with weather context", f"{weather_available}/{len(matchup_packet.games)}"),
        _fact(
            "Model manifest",
            f"{predictions.model_manifest.model_id} {predictions.model_manifest.model_version}",
        ),
        _fact("Model deployment", predictions.model_manifest.deployment_status.value),
        _fact("Model calibration", predictions.model_manifest.calibration_status.value),
    )
    methodology = (
        "Predictions are produced before sportsbook comparison and remain distinct from value, recommendation, ranking, and publication decisions.",
        "Top Confidence orders actionable candidates by recommendation tier and model probability; Best Value orders them by recommendation tier and expected value.",
        "Every evaluated BET, LEAN, PASS, and AVOID remains in the full-slate snapshot and in its game dossier.",
        "No report wording may promote a LEAN, PASS, or AVOID, rewrite a rank, fabricate missing evidence, approve publication, or assign a stake size.",
        "Odds, weather, starters, lineups, and model evidence are presented as of the report cutoff and may change afterward.",
    )
    responsible_use = (
        "This report is analytical information, not a guarantee of profit. Prices and game information can change after the stated cutoff. BET labels remain pre-review candidates until the later Human Review phase; use only legal, affordable wagering limits."
    )
    timestamp = (
        max(
            rankings.ranked_at,
            recommendation_gate.evaluated_at,
            predictions.observed_at,
            matchup_packet.observed_at,
        )
        if generated_at is None
        else generated_at
    )
    return PdfReportDocumentV1(
        requested_date=rankings.requested_date,
        as_of_time=rankings.as_of_time,
        generated_at=timestamp,
        policy=selected_policy,
        upstream_rankings_checksum=rankings.checksum,
        upstream_recommendation_gate_checksum=recommendation_gate.checksum,
        upstream_predictions_checksum=predictions.checksum,
        upstream_matchup_packet_checksum=matchup_packet.checksum,
        top_confidence_row_checksums=top_rows,
        best_value_row_checksums=best_rows,
        full_slate_rows=tuple(rows),
        slate_health=slate_health,
        games=tuple(dossiers),
        methodology=methodology,
        responsible_use_notice=responsible_use,
        secret_values=secret_values,
    )


def market_display(row: PdfReportDecisionRowV1) -> str:
    side = row.side.value.upper()
    return f"{side} {_line_label_from_row(row)} at {_price(row.american_price)}"


def _line_label_from_row(row: PdfReportDecisionRowV1) -> str:
    if row.market_line is None:
        return row.market.value
    return f"{row.market.value} {row.market_line:+g}"
