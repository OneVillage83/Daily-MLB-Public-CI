from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from app.infographic.contracts import (
    InfographicDocumentV1,
    InfographicSelectionV1,
    InfographicVariantType,
    InfographicVariantV1,
)
from app.infographic.policy import InfographicPolicyV1
from app.pdf_report.production import ProductionPdfReportV1


class InfographicAssemblyError(ValueError):
    pass


def _selection(game: object) -> InfographicSelectionV1:
    if getattr(game, "decision") != "recommend" or getattr(game, "recommendation_rank") is None:
        raise InfographicAssemblyError("only canonical RECOMMEND games may become cards")
    selected_side = getattr(game, "selected_side")
    outcome = next(item for item in getattr(game, "outcomes") if item.side == selected_side)
    return InfographicSelectionV1(
        recommendation_rank=getattr(game, "recommendation_rank"),
        source_game_id=getattr(game, "source_game_id"),
        matchup_label=f"{getattr(game, 'away_team_id').upper()} at {getattr(game, 'home_team_id').upper()}",
        selected_team_id=getattr(game, "selected_team_id"),
        selected_side=selected_side,
        prediction_probability=outcome.prediction_probability,
        market_probability=outcome.consensus_no_vig_probability,
        edge=outcome.edge,
        expected_value_per_unit=outcome.expected_value_per_unit,
        best_price=outcome.best_price,
        bookmaker_count=outcome.bookmaker_count,
        quality_disposition=getattr(game, "quality_disposition"),
        upstream_pdf_game_checksum=getattr(game, "checksum"),
        upstream_ranking_entry_checksum=getattr(game, "upstream_ranking_entry_checksum"),
        upstream_gate_game_checksum=getattr(game, "upstream_gate_game_checksum"),
        upstream_value_checksum=outcome.value_checksum,
    )


def _weather(report: ProductionPdfReportV1) -> tuple[str, str]:
    if not report.games:
        return "No games on the retained slate", "Weather Watch is inactive for the confirmed zero-game slate."
    for game in report.games:
        odds_weather = game.context.get("odds_weather")
        if not isinstance(odds_weather, Mapping):
            continue
        weather = odds_weather.get("weather")
        if isinstance(weather, Mapping):
            status = str(weather.get("status", "unavailable"))
            if status not in {"available", "indoor_fixed_roof", "not_applicable"}:
                return (
                    f"Weather Watch - {game.away_team_id.upper()} at {game.home_team_id.upper()}",
                    f"Retained weather status: {status}. See the full report for exact evidence.",
                )
    return "No material weather alert retained", "Weather status is derived only from the sealed PDF Report evidence."


def assemble_infographic_document(
    report: ProductionPdfReportV1,
    *,
    upstream_pdf_report_snapshot_id: str,
    generated_at: datetime,
    policy: InfographicPolicyV1 | None = None,
    secret_values: Iterable[str] = (),
) -> InfographicDocumentV1:
    if report.report_status != "pre_review":
        raise InfographicAssemblyError("Infographic requires a pre-review PDF Report")
    selected_policy = InfographicPolicyV1() if policy is None else policy
    ranked = tuple(
        _selection(game)
        for game in sorted(
            (game for game in report.games if game.decision == "recommend"),
            key=lambda game: game.recommendation_rank or 0,
        )
    )
    if [item.recommendation_rank for item in ranked] != list(range(1, len(ranked) + 1)):
        raise InfographicAssemblyError("report recommendations are not canonical rank order")
    weather_headline, weather_detail = _weather(report)
    variants: list[InfographicVariantV1] = []
    for variant in InfographicVariantType:
        if variant is InfographicVariantType.FEED_4X5:
            width, height, limit = (
                selected_policy.feed_width,
                selected_policy.feed_height,
                selected_policy.feed_recommendation_limit,
            )
        else:
            width, height, limit = (
                selected_policy.story_width,
                selected_policy.story_height,
                selected_policy.story_recommendation_limit,
            )
        cards = ranked[:limit]
        variants.append(
            InfographicVariantV1(
                variant=variant,
                width=width,
                height=height,
                pick_of_day=None if not cards else cards[0],
                recommendations=cards[1:],
                weather_headline=weather_headline,
                weather_detail=weather_detail,
                report_cta="Full research and every RECOMMEND, PASS, and AVOID are retained in the Daily MLB Report.",
            )
        )
    return InfographicDocumentV1(
        run_id=report.run_id,
        requested_date=report.requested_date,
        as_of_time=report.as_of_time,
        generated_at=generated_at,
        upstream_pdf_report_snapshot_id=upstream_pdf_report_snapshot_id,
        upstream_pdf_report_checksum=report.checksum,
        policy=selected_policy,
        variants=(variants[0], variants[1]),
        full_report_game_count=len(report.games),
        full_report_recommendation_count=len(ranked),
        warnings=report.warnings,
        secret_values=secret_values,
    )
