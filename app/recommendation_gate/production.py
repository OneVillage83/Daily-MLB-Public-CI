from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass, replace
from datetime import datetime, timezone

from app.data_quality.contracts import DataQualityGameV1, QualityDomain, QualityIssueSeverity
from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.predictions.production import MoneylinePredictionV1
from app.redaction import redact_value
from app.value_engine.production import MoneylineOutcomeValueV1, ProductionValueEngineV1

RECOMMENDATION_GATE_PRODUCTION_CONTRACT = "DSE_MLB_ML_RECOMMENDATION_GATE_V1"
RECOMMENDATION_GATE_PHASE_INPUT_CONTRACT = "DSE_MLB_ML_RECOMMENDATION_GATE_PHASE_INPUT_V1"
RECOMMENDATION_GATE_POLICY_VERSION = "DSE_MLB_ML_RECOMMENDATION_POLICY_V1"
GATE_CODES = (
    "prediction_valid",
    "prediction_market_independent",
    "prediction_identity_valid",
    "feature_lineage_valid",
    "market_supported",
    "minimum_bookmaker_count",
    "odds_fresh",
    "best_price_available",
    "data_quality_model_ready",
    "minimum_edge",
    "minimum_ev",
    "lower_bound_clears_market",
    "uncertainty_acceptable",
    "lineup_and_starter_risk",
    "weather_evidence_acceptable",
    "event_pregame",
    "opposing_side_not_selected",
)
RISK_GATE_CODES = frozenset(
    {
        "data_quality_model_ready",
        "uncertainty_acceptable",
        "lineup_and_starter_risk",
        "weather_evidence_acceptable",
        "event_pregame",
    }
)
OPPORTUNITY_GATE_CODES = frozenset(GATE_CODES) - RISK_GATE_CODES - {
    "prediction_valid",
    "prediction_market_independent",
    "prediction_identity_valid",
    "feature_lineage_valid",
}


class RecommendationGateProductionError(ValueError):
    pass


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RecommendationGateProductionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RecommendationPolicyV1:
    minimum_fresh_bookmakers: int = 4
    minimum_edge: float = 0.03
    minimum_ev: float = 0.02
    minimum_lower_bound_clearance: float = 0.01
    maximum_interval_width: float = 0.25
    policy_version: str = RECOMMENDATION_GATE_POLICY_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_fresh_bookmakers, bool)
            or not isinstance(self.minimum_fresh_bookmakers, int)
            or self.minimum_fresh_bookmakers < 1
        ):
            raise RecommendationGateProductionError("minimum_fresh_bookmakers must be positive")
        for name in ("minimum_edge", "minimum_ev", "minimum_lower_bound_clearance", "maximum_interval_width"):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise RecommendationGateProductionError(f"{name} is invalid")
            object.__setattr__(self, name, value)

    def identity_dict(self) -> dict[str, object]:
        return {
            "maximum_interval_width": self.maximum_interval_width,
            "minimum_edge": self.minimum_edge,
            "minimum_ev": self.minimum_ev,
            "minimum_fresh_bookmakers": self.minimum_fresh_bookmakers,
            "minimum_lower_bound_clearance": self.minimum_lower_bound_clearance,
            "policy_version": self.policy_version,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GateResultV1:
    code: str
    threshold: object
    observed_value: object
    passed: bool
    reason: str
    source_checksum: str
    evaluated_at: datetime
    policy_checksum: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.code not in GATE_CODES or self.ordinal != GATE_CODES.index(self.code) + 1:
            raise RecommendationGateProductionError("gate code/ordinal is invalid")
        if not isinstance(self.passed, bool):
            raise RecommendationGateProductionError("gate passed state must be Boolean")
        if not self.reason or self.reason != self.reason.strip():
            raise RecommendationGateProductionError("gate reason must be nonblank")
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))

    def identity_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "evaluated_at": self.evaluated_at.isoformat(),
            "observed_value": self.observed_value,
            "ordinal": self.ordinal,
            "passed": self.passed,
            "policy_checksum": self.policy_checksum,
            "reason": self.reason,
            "source_checksum": self.source_checksum,
            "threshold": self.threshold,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GateSideEvaluationV1:
    side: str
    outcome_team_id: str
    decision: str
    value_checksum: str
    results: tuple[GateResultV1, ...]
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.side not in {"home", "away"} or self.decision not in {"recommend", "pass", "avoid"}:
            raise RecommendationGateProductionError("gate side or decision is invalid")
        if tuple(result.code for result in self.results) != GATE_CODES:
            raise RecommendationGateProductionError("gate result inventory is incomplete")
        failed_codes = tuple(result.code for result in self.results if not result.passed)
        if self.reason_codes != failed_codes:
            raise RecommendationGateProductionError("reason codes must exactly equal failed gate codes")
        risk_failures = RISK_GATE_CODES.intersection(failed_codes)
        if self.decision == "recommend" and failed_codes:
            raise RecommendationGateProductionError("recommended side cannot contain a failed gate")
        if self.decision == "pass" and (not failed_codes or risk_failures):
            raise RecommendationGateProductionError("pass requires only opportunity or selection failures")
        if self.decision == "avoid" and not risk_failures:
            raise RecommendationGateProductionError("avoid requires at least one risk gate failure")

    def identity_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "outcome_team_id": self.outcome_team_id,
            "reason_codes": list(self.reason_codes),
            "results": [result.as_dict() for result in self.results],
            "side": self.side,
            "value_checksum": self.value_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class GateGameV1:
    ordinal: int
    source_game_id: str
    decision: str
    selected_side: str | None
    selected_team_id: str | None
    value_game_checksum: str
    prediction_checksum: str
    quality_disposition: str
    sides: tuple[GateSideEvaluationV1, GateSideEvaluationV1]

    def __post_init__(self) -> None:
        if self.decision not in {"recommend", "pass", "avoid"}:
            raise RecommendationGateProductionError("game decision is invalid")
        if tuple(side.side for side in self.sides) != ("home", "away"):
            raise RecommendationGateProductionError("gate game must retain both sides")
        if (self.decision == "recommend") != (self.selected_side is not None and self.selected_team_id is not None):
            raise RecommendationGateProductionError("selected recommendation identity mismatch")
        recommended = tuple(side for side in self.sides if side.decision == "recommend")
        if len(recommended) != (1 if self.decision == "recommend" else 0):
            raise RecommendationGateProductionError("game decision must agree with its side decisions")
        if recommended and (
            self.selected_side != recommended[0].side or self.selected_team_id != recommended[0].outcome_team_id
        ):
            raise RecommendationGateProductionError("selected recommendation does not match the recommended side")
        if self.decision == "avoid" and not any(side.decision == "avoid" for side in self.sides):
            raise RecommendationGateProductionError("avoid game requires an avoided side")
        if self.decision == "pass" and any(side.decision != "pass" for side in self.sides):
            raise RecommendationGateProductionError("pass game requires both sides to pass")
        selection = {
            side.side: next(result for result in side.results if result.code == "opposing_side_not_selected")
            for side in self.sides
        }
        if recommended:
            if not selection[recommended[0].side].passed or any(
                result.passed for side, result in selection.items() if side != recommended[0].side
            ):
                raise RecommendationGateProductionError("selection gates disagree with the sole recommendation")
        elif any(result.passed for result in selection.values()):
            raise RecommendationGateProductionError("unrecommended game cannot claim a selected side")

    def identity_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "ordinal": self.ordinal,
            "prediction_checksum": self.prediction_checksum,
            "quality_disposition": self.quality_disposition,
            "selected_side": self.selected_side,
            "selected_team_id": self.selected_team_id,
            "sides": [side.as_dict() for side in self.sides],
            "source_game_id": self.source_game_id,
            "value_game_checksum": self.value_game_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ProductionRecommendationGateV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    evaluated_at: datetime
    policy: RecommendationPolicyV1
    upstream_value_snapshot_id: str
    upstream_value_checksum: str
    upstream_predictions_snapshot_id: str
    upstream_predictions_checksum: str
    upstream_data_quality_snapshot_id: str
    upstream_data_quality_checksum: str
    games: tuple[GateGameV1, ...]
    warnings: tuple[Mapping[str, object], ...] = ()
    contract_version: str = RECOMMENDATION_GATE_PRODUCTION_CONTRACT
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "evaluated_at", _utc(self.evaluated_at, "evaluated_at"))
        if [game.ordinal for game in self.games] != list(range(1, len(self.games) + 1)):
            raise RecommendationGateProductionError("gate game ordinals must be contiguous")
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(self.identity_dict(), configured) != self.identity_dict():
            raise RecommendationGateProductionError("gate snapshot contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "games": [game.as_dict() for game in self.games],
            "policy": self.policy.as_dict(),
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_data_quality_checksum": self.upstream_data_quality_checksum,
            "upstream_data_quality_snapshot_id": self.upstream_data_quality_snapshot_id,
            "upstream_predictions_checksum": self.upstream_predictions_checksum,
            "upstream_predictions_snapshot_id": self.upstream_predictions_snapshot_id,
            "upstream_value_checksum": self.upstream_value_checksum,
            "upstream_value_snapshot_id": self.upstream_value_snapshot_id,
            "warnings": [dict(value) for value in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def _gate_results(
    value: MoneylineOutcomeValueV1,
    *,
    prediction: MoneylinePredictionV1,
    quality_game: DataQualityGameV1,
    policy: RecommendationPolicyV1,
    evaluated_at: datetime,
    pregame: bool,
) -> tuple[GateResultV1, ...]:
    interval_width = value.probability_upper - value.probability_lower
    prediction_valid = prediction.checksum == value.prediction_checksum
    prediction_identity_valid = value.outcome_team_id == (
        prediction.home_team_id if value.side == "home" else prediction.away_team_id
    )
    feature_lineage_valid = bool(
        prediction.predictive_feature_checksum and prediction.upstream_model_feature_game_checksum
    )
    relevant_severities = {QualityIssueSeverity.WARNING, QualityIssueSeverity.CRITICAL}
    lineup_issues = tuple(
        issue.code
        for issue in quality_game.issues
        if issue.domain in {QualityDomain.STARTER, QualityDomain.LINEUP} and issue.severity in relevant_severities
    )
    weather_issues = tuple(
        issue.code
        for issue in quality_game.issues
        if issue.domain is QualityDomain.WEATHER and issue.severity in relevant_severities
    )
    model_ready = quality_game.disposition.value not in {"degraded", "insufficient"}
    observed: dict[str, tuple[object, object, bool]] = {
        "prediction_valid": (value.prediction_checksum, prediction.checksum, prediction_valid),
        "prediction_market_independent": (True, prediction.market_independence_attested, prediction.market_independence_attested),
        "prediction_identity_valid": (value.outcome_team_id, value.outcome_team_id if prediction_identity_valid else None, prediction_identity_valid),
        "feature_lineage_valid": (
            prediction.upstream_model_feature_game_checksum,
            prediction.predictive_feature_checksum,
            feature_lineage_valid,
        ),
        "market_supported": ("available", value.availability, value.availability == "available"),
        "minimum_bookmaker_count": (
            policy.minimum_fresh_bookmakers,
            len(value.eligible_pairs),
            len(value.eligible_pairs) >= policy.minimum_fresh_bookmakers,
        ),
        "odds_fresh": ("fresh", value.freshness_state, value.freshness_state == "fresh"),
        "best_price_available": (True, value.best_price is not None, value.best_price is not None),
        "data_quality_model_ready": (True, model_ready, model_ready),
        "minimum_edge": (policy.minimum_edge, value.edge, value.edge is not None and value.edge >= policy.minimum_edge),
        "minimum_ev": (
            policy.minimum_ev,
            value.expected_value_per_unit,
            value.expected_value_per_unit is not None and value.expected_value_per_unit >= policy.minimum_ev,
        ),
        "lower_bound_clears_market": (
            policy.minimum_lower_bound_clearance,
            value.lower_bound_clearance,
            value.lower_bound_clearance is not None
            and value.lower_bound_clearance >= policy.minimum_lower_bound_clearance,
        ),
        "uncertainty_acceptable": (
            policy.maximum_interval_width,
            interval_width,
            interval_width <= policy.maximum_interval_width,
        ),
        "lineup_and_starter_risk": ([], list(lineup_issues), not lineup_issues),
        "weather_evidence_acceptable": ([], list(weather_issues), not weather_issues),
        "event_pregame": (True, pregame, pregame),
        "opposing_side_not_selected": ("sole_recommendation", None, False),
    }
    return tuple(
        GateResultV1(
            code,
            threshold,
            seen,
            passed,
            "passed" if passed else f"{code} failed",
            prediction.checksum
            if code in {
                "prediction_valid",
                "prediction_market_independent",
                "prediction_identity_valid",
                "feature_lineage_valid",
            }
            else quality_game.checksum
            if code in {"data_quality_model_ready", "lineup_and_starter_risk", "weather_evidence_acceptable"}
            else value.checksum,
            evaluated_at,
            policy.checksum,
            ordinal,
        )
        for ordinal, code in enumerate(GATE_CODES, 1)
        for threshold, seen, passed in (observed[code],)
    )


def evaluate_recommendation_gate(
    value: ProductionValueEngineV1,
    *,
    predictions_by_game: Mapping[str, MoneylinePredictionV1],
    quality_games_by_id: Mapping[str, DataQualityGameV1],
    scheduled_start_by_game: Mapping[str, datetime],
    policy: RecommendationPolicyV1,
    evaluated_at: datetime,
    secret_values: Iterable[str] = (),
) -> ProductionRecommendationGateV1:
    evaluated = _utc(evaluated_at, "evaluated_at")
    games: list[GateGameV1] = []
    for game in value.games:
        prediction = predictions_by_game[game.source_game_id]
        quality_game = quality_games_by_id[game.source_game_id]
        quality = quality_game.disposition.value
        pregame = evaluated < _utc(scheduled_start_by_game[game.source_game_id], "scheduled_start")
        result_rows: list[tuple[MoneylineOutcomeValueV1, tuple[GateResultV1, ...]]] = []
        candidates: list[tuple[float, float, float, str, str]] = []
        for outcome in game.outcomes:
            results = _gate_results(
                outcome,
                prediction=prediction,
                quality_game=quality_game,
                policy=policy,
                evaluated_at=evaluated,
                pregame=pregame,
            )
            independent_failures = tuple(
                result.code
                for result in results
                if not result.passed and result.code != "opposing_side_not_selected"
            )
            if not independent_failures:
                if (
                    outcome.expected_value_per_unit is None
                    or outcome.edge is None
                    or outcome.lower_bound_clearance is None
                ):
                    raise RecommendationGateProductionError("qualified side is missing value metrics")
                candidates.append(
                    (
                        outcome.expected_value_per_unit,
                        outcome.edge,
                        outcome.lower_bound_clearance,
                        outcome.outcome_team_id,
                        outcome.side,
                    )
                )
            result_rows.append((outcome, results))
        winner = max(candidates)[-1] if candidates else None
        side_rows: list[GateSideEvaluationV1] = []
        selected_team = next(
            (outcome.outcome_team_id for outcome, _ in result_rows if outcome.side == winner), None
        )
        for outcome, results in result_rows:
            selected = outcome.side == winner
            finalized = tuple(
                replace(
                    result,
                    passed=selected,
                    threshold="sole_recommendation",
                    observed_value=selected_team,
                    reason="passed" if selected else "opposing side selected" if winner else "no side selected",
                )
                if result.code == "opposing_side_not_selected"
                else result
                for result in results
            )
            failures = tuple(result.code for result in finalized if not result.passed)
            decision = "avoid" if RISK_GATE_CODES.intersection(failures) else "recommend" if not failures else "pass"
            side_rows.append(
                GateSideEvaluationV1(
                    outcome.side,
                    outcome.outcome_team_id,
                    decision,
                    outcome.checksum,
                    finalized,
                    failures,
                )
            )
        recommended = next((side for side in side_rows if side.decision == "recommend"), None)
        decision = (
            "recommend" if recommended else "avoid" if any(side.decision == "avoid" for side in side_rows) else "pass"
        )
        prediction_checksum = game.outcomes[0].prediction_checksum
        games.append(
            GateGameV1(
                game.ordinal,
                game.source_game_id,
                decision,
                None if recommended is None else recommended.side,
                None if recommended is None else recommended.outcome_team_id,
                game.checksum,
                prediction_checksum,
                quality,
                (side_rows[0], side_rows[1]),
            )
        )
    return ProductionRecommendationGateV1(
        value.run_id,
        value.requested_date,
        value.as_of_time,
        evaluated,
        policy,
        f"value-engine:{value.checksum}",
        value.checksum,
        value.upstream_predictions_snapshot_id,
        value.upstream_predictions_checksum,
        value.upstream_data_quality_snapshot_id,
        value.upstream_data_quality_checksum,
        tuple(games),
        secret_values=secret_values,
    )
