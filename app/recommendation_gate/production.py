from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
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
        if sum(side.decision == "recommend" for side in self.sides) > 1:
            raise RecommendationGateProductionError("at most one side may be recommended")

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
    policy: RecommendationPolicyV1,
    evaluated_at: datetime,
    model_ready: bool,
    pregame: bool,
) -> tuple[GateResultV1, ...]:
    interval_width = value.probability_upper - value.probability_lower
    observed: dict[str, tuple[object, object, bool]] = {
        "prediction_valid": (True, True, True),
        "prediction_market_independent": (True, True, True),
        "prediction_identity_valid": (True, True, True),
        "feature_lineage_valid": (True, True, True),
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
        "lineup_and_starter_risk": (True, model_ready, model_ready),
        "weather_evidence_acceptable": (True, model_ready, model_ready),
        "event_pregame": (True, pregame, pregame),
        "opposing_side_not_selected": (True, True, True),
    }
    return tuple(
        GateResultV1(
            code,
            threshold,
            seen,
            passed,
            "passed" if passed else f"{code} failed",
            value.checksum,
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
    quality_by_game: Mapping[str, str],
    scheduled_start_by_game: Mapping[str, datetime],
    policy: RecommendationPolicyV1,
    evaluated_at: datetime,
    secret_values: Iterable[str] = (),
) -> ProductionRecommendationGateV1:
    evaluated = _utc(evaluated_at, "evaluated_at")
    games: list[GateGameV1] = []
    for game in value.games:
        quality = quality_by_game[game.source_game_id]
        model_ready = quality not in {"degraded", "insufficient"}
        pregame = evaluated < _utc(scheduled_start_by_game[game.source_game_id], "scheduled_start")
        side_rows: list[GateSideEvaluationV1] = []
        candidates: list[tuple[float, float, str]] = []
        for outcome in game.outcomes:
            results = _gate_results(
                outcome, policy=policy, evaluated_at=evaluated, model_ready=model_ready, pregame=pregame
            )
            failures = tuple(result.code for result in results if not result.passed)
            avoid_codes = {
                "data_quality_model_ready",
                "uncertainty_acceptable",
                "lineup_and_starter_risk",
                "weather_evidence_acceptable",
                "event_pregame",
            }
            decision = "avoid" if avoid_codes.intersection(failures) else "recommend" if not failures else "pass"
            if decision == "recommend":
                candidates.append((outcome.expected_value_per_unit or -999.0, outcome.edge or -999.0, outcome.side))
            side_rows.append(
                GateSideEvaluationV1(
                    outcome.side, outcome.outcome_team_id, decision, outcome.checksum, results, failures
                )
            )
        if len(candidates) > 1:
            winner = max(candidates)[2]
            side_rows = [
                side
                if side.side == winner
                else GateSideEvaluationV1(
                    side.side,
                    side.outcome_team_id,
                    "pass",
                    side.value_checksum,
                    side.results,
                    tuple(sorted((*side.reason_codes, "opposing_side_not_selected"))),
                )
                for side in side_rows
            ]
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
