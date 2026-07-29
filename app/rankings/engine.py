from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.rankings.contracts import (
    RankingBoardType,
    RankingBoardV1,
    RankingEntryV1,
    RankingExclusionReason,
    RankingGameV1,
    RankingGameWarning,
    RankingPlacementV1,
    RankingsContractError,
    RankingsV1,
)
from app.rankings.policy import DEFAULT_RANKINGS_POLICY_V1, RankingsPolicyV1
from app.recommendation_gate.contracts import (
    RecommendationDecision,
    RecommendationGameV1,
    RecommendationGateV1,
    RecommendationV1,
)


@dataclass(frozen=True, slots=True)
class _CandidateRef:
    game: RecommendationGameV1
    recommendation: RecommendationV1


_DECISION_TIER = {
    RecommendationDecision.BET: 0,
    RecommendationDecision.LEAN: 1,
}


def _is_actionable(recommendation: RecommendationV1) -> bool:
    return recommendation.decision in {
        RecommendationDecision.BET,
        RecommendationDecision.LEAN,
    }


def _required_edge(recommendation: RecommendationV1) -> float:
    edge = recommendation.no_vig_probability_edge
    if edge is None:
        raise RankingsContractError("actionable recommendation is missing no-vig edge")
    return edge


def _identity_key(candidate: _CandidateRef) -> tuple[str, str, str, str, str]:
    recommendation = candidate.recommendation
    return (
        candidate.game.source_game_id,
        recommendation.market.value,
        recommendation.line_key,
        recommendation.side.value,
        recommendation.checksum,
    )


def _top_confidence_key(candidate: _CandidateRef) -> tuple[object, ...]:
    recommendation = candidate.recommendation
    return (
        _DECISION_TIER[recommendation.decision],
        -recommendation.conditional_model_probability,
        -recommendation.evidence_confidence_score,
        -recommendation.expected_value_per_unit,
        -_required_edge(recommendation),
        -recommendation.bookmaker_count,
        recommendation.operational_risk_score,
        *_identity_key(candidate),
    )


def _best_value_key(candidate: _CandidateRef) -> tuple[object, ...]:
    recommendation = candidate.recommendation
    return (
        _DECISION_TIER[recommendation.decision],
        -recommendation.expected_value_per_unit,
        -_required_edge(recommendation),
        -recommendation.evidence_confidence_score,
        -recommendation.conditional_model_probability,
        -recommendation.bookmaker_count,
        recommendation.operational_risk_score,
        *_identity_key(candidate),
    )


def _entry(
    candidate: _CandidateRef,
    *,
    top_confidence_rank: int | None,
    best_value_rank: int | None,
    policy: RankingsPolicyV1,
) -> RankingEntryV1:
    game = candidate.game
    recommendation = candidate.recommendation
    actionable = _is_actionable(recommendation)
    return RankingEntryV1(
        upstream_recommendation_checksum=recommendation.checksum,
        upstream_recommendation_game_checksum=game.checksum,
        edge_event_id=game.edge_event_id,
        daily_mlb_game_id=game.daily_mlb_game_id,
        source_game_id=game.source_game_id,
        away_team_id=game.away_team_id,
        home_team_id=game.home_team_id,
        market=recommendation.market,
        line_key=recommendation.line_key,
        side=recommendation.side,
        market_line=recommendation.market_line,
        american_price=recommendation.american_price,
        bookmaker_count=recommendation.bookmaker_count,
        freshness_status=recommendation.freshness_status,
        decision=recommendation.decision,
        reasons=recommendation.reasons,
        conditional_model_probability=(
            recommendation.conditional_model_probability
        ),
        no_vig_probability=recommendation.no_vig_probability,
        no_vig_probability_edge=recommendation.no_vig_probability_edge,
        expected_value_per_unit=recommendation.expected_value_per_unit,
        expected_roi_percent=recommendation.expected_roi_percent,
        evidence_confidence_score=recommendation.evidence_confidence_score,
        evidence_confidence_band=recommendation.evidence_confidence_band,
        operational_risk_score=recommendation.operational_risk_score,
        operational_risk_level=recommendation.operational_risk_level,
        review_required=recommendation.review_required,
        publication_candidate=recommendation.publication_candidate,
        manual_promotion_permitted=recommendation.manual_promotion_permitted,
        ranking_eligible=actionable,
        exclusion_reason=(
            None
            if actionable
            else RankingExclusionReason.RECOMMENDATION_NOT_ACTIONABLE
        ),
        top_confidence_rank=top_confidence_rank,
        best_value_rank=best_value_rank,
        recommendation_evaluated_at=recommendation.evaluated_at,
        policy_checksum=policy.checksum,
    )


def _placement(
    entry: RankingEntryV1,
    *,
    board: RankingBoardType,
    rank: int,
    policy: RankingsPolicyV1,
) -> RankingPlacementV1:
    if board is RankingBoardType.TOP_CONFIDENCE:
        metric_name = "conditional_model_probability"
        metric_value = entry.conditional_model_probability
    else:
        metric_name = "expected_value_per_unit"
        metric_value = entry.expected_value_per_unit
    return RankingPlacementV1(
        board=board,
        rank=rank,
        ranking_entry_checksum=entry.checksum,
        upstream_recommendation_checksum=entry.upstream_recommendation_checksum,
        decision=entry.decision,
        primary_metric_name=metric_name,
        primary_metric_value=metric_value,
        policy_checksum=policy.checksum,
    )


def rank_recommendations(
    recommendation_gate: RecommendationGateV1,
    *,
    policy: RankingsPolicyV1 = DEFAULT_RANKINGS_POLICY_V1,
    ranked_at: datetime | None = None,
) -> RankingsV1:
    selected_time = (
        recommendation_gate.evaluated_at if ranked_at is None else ranked_at
    )
    if selected_time.tzinfo is None or selected_time.utcoffset() is None:
        raise RankingsContractError("ranked_at must be timezone-aware")
    selected_time = selected_time.astimezone(timezone.utc)
    if selected_time < recommendation_gate.evaluated_at:
        raise RankingsContractError(
            "Rankings cannot precede Recommendation Gate evaluation"
        )

    candidates = tuple(
        _CandidateRef(game=game, recommendation=recommendation)
        for game in recommendation_gate.games
        for recommendation in game.recommendations
    )
    checksums = [candidate.recommendation.checksum for candidate in candidates]
    if len(set(checksums)) != len(checksums):
        raise RankingsContractError("duplicate recommendation checksums")

    actionable = tuple(candidate for candidate in candidates if _is_actionable(candidate.recommendation))
    top_order = tuple(sorted(actionable, key=_top_confidence_key))
    value_order = tuple(sorted(actionable, key=_best_value_key))
    top_ranks = {
        candidate.recommendation.checksum: rank
        for rank, candidate in enumerate(top_order, start=1)
    }
    value_ranks = {
        candidate.recommendation.checksum: rank
        for rank, candidate in enumerate(value_order, start=1)
    }

    entries_by_recommendation: dict[str, RankingEntryV1] = {}
    games: list[RankingGameV1] = []
    for game in recommendation_gate.games:
        entries = tuple(
            _entry(
                _CandidateRef(game=game, recommendation=recommendation),
                top_confidence_rank=top_ranks.get(recommendation.checksum),
                best_value_rank=value_ranks.get(recommendation.checksum),
                policy=policy,
            )
            for recommendation in game.recommendations
        )
        for entry in entries:
            entries_by_recommendation[entry.upstream_recommendation_checksum] = entry
        games.append(
            RankingGameV1(
                edge_event_id=game.edge_event_id,
                daily_mlb_game_id=game.daily_mlb_game_id,
                source_game_id=game.source_game_id,
                away_team_id=game.away_team_id,
                home_team_id=game.home_team_id,
                upstream_recommendation_game_checksum=game.checksum,
                entries=entries,
                warnings=(
                    ()
                    if entries
                    else (RankingGameWarning.NO_RECOMMENDATION_ROWS,)
                ),
            )
        )

    top_placements = tuple(
        _placement(
            entries_by_recommendation[candidate.recommendation.checksum],
            board=RankingBoardType.TOP_CONFIDENCE,
            rank=rank,
            policy=policy,
        )
        for rank, candidate in enumerate(top_order, start=1)
    )
    value_placements = tuple(
        _placement(
            entries_by_recommendation[candidate.recommendation.checksum],
            board=RankingBoardType.BEST_VALUE,
            rank=rank,
            policy=policy,
        )
        for rank, candidate in enumerate(value_order, start=1)
    )

    return RankingsV1(
        requested_date=recommendation_gate.requested_date,
        as_of_time=recommendation_gate.as_of_time,
        ranked_at=selected_time,
        upstream_recommendation_gate_checksum=recommendation_gate.checksum,
        policy=policy,
        games=tuple(games),
        top_confidence=RankingBoardV1(
            board=RankingBoardType.TOP_CONFIDENCE,
            placements=top_placements,
            policy_checksum=policy.checksum,
        ),
        best_value=RankingBoardV1(
            board=RankingBoardType.BEST_VALUE,
            placements=value_placements,
            policy_checksum=policy.checksum,
        ),
    )
