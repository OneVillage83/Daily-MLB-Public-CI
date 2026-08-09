from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.data_quality.contracts import DataQualityV1
from app.identifiers import parse_requested_date, validate_run_id
from app.matchup_packet.contracts import MatchupPacketV1
from app.pdf_report.policy import PdfReportPolicyV1
from app.predictions.production import PredictionsV1
from app.rankings.production import ProductionRankingsV1
from app.recommendation_gate.production import ProductionRecommendationGateV1
from app.redaction import redact_value
from app.value_engine.production import ProductionValueEngineV1

PDF_REPORT_PRODUCTION_CONTRACT = "DSE_MLB_PDF_REPORT_V1"
PDF_REPORT_RENDER_VERSION = "DSE_MLB_PDF_RENDER_V1"
PDF_REPORT_PHASE_INPUT_CONTRACT = "DSE_MLB_PDF_REPORT_PHASE_INPUT_V1"
PDF_REPORT_ATTEMPT_MANIFEST_CONTRACT = "DSE_PDF_REPORT_ATTEMPT_MANIFEST_V1"


class PdfReportProductionError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PdfReportProductionError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise PdfReportProductionError(f"{name} must be lowercase SHA-256")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PdfReportProductionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _finite_optional(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PdfReportProductionError(f"{name} must be finite numeric")
    number = float(value)
    if not math.isfinite(number):
        raise PdfReportProductionError(f"{name} must be finite numeric")
    return number


@dataclass(frozen=True, slots=True)
class PdfReportOutcomeV1:
    side: str
    outcome_team_id: str
    prediction_probability: float
    probability_lower: float
    probability_upper: float
    availability: str
    bookmaker_count: int
    consensus_no_vig_probability: float | None
    best_price: float | None
    best_price_bookmakers: tuple[str, ...]
    edge: float | None
    expected_value_per_unit: float | None
    lower_bound_clearance: float | None
    freshness_state: str
    value_checksum: str
    prediction_checksum: str
    market_context_checksum: str

    def __post_init__(self) -> None:
        if self.side not in {"home", "away"}:
            raise PdfReportProductionError("report outcome side is invalid")
        if self.availability not in {"available", "unavailable"}:
            raise PdfReportProductionError("report outcome availability is invalid")
        for name in (
            "prediction_probability",
            "probability_lower",
            "probability_upper",
            "consensus_no_vig_probability",
            "best_price",
            "edge",
            "expected_value_per_unit",
            "lower_bound_clearance",
        ):
            object.__setattr__(self, name, _finite_optional(getattr(self, name), name))
        if (
            self.prediction_probability is None
            or self.probability_lower is None
            or self.probability_upper is None
            or not 0 <= self.probability_lower <= self.prediction_probability <= self.probability_upper <= 1
        ):
            raise PdfReportProductionError("report prediction interval is invalid")
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count < 0
        ):
            raise PdfReportProductionError("bookmaker_count must be nonnegative")
        object.__setattr__(self, "best_price_bookmakers", tuple(self.best_price_bookmakers))
        for name in ("value_checksum", "prediction_checksum", "market_context_checksum"):
            object.__setattr__(self, name, _sha(getattr(self, name), name))

    def identity_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability,
            "best_price": self.best_price,
            "best_price_bookmakers": list(self.best_price_bookmakers),
            "bookmaker_count": self.bookmaker_count,
            "consensus_no_vig_probability": self.consensus_no_vig_probability,
            "edge": self.edge,
            "expected_value_per_unit": self.expected_value_per_unit,
            "freshness_state": self.freshness_state,
            "lower_bound_clearance": self.lower_bound_clearance,
            "market_context_checksum": self.market_context_checksum,
            "outcome_team_id": self.outcome_team_id,
            "prediction_checksum": self.prediction_checksum,
            "prediction_probability": self.prediction_probability,
            "probability_lower": self.probability_lower,
            "probability_upper": self.probability_upper,
            "side": self.side,
            "value_checksum": self.value_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PdfReportGameV1:
    ordinal: int
    source_game_id: str
    away_team_id: str
    home_team_id: str
    scheduled_start_time: datetime | None
    decision: str
    selected_side: str | None
    selected_team_id: str | None
    recommendation_rank: int | None
    quality_disposition: str
    quality_issue_codes: tuple[str, ...]
    provider_kind: str
    provider_contract: str
    provider_version: str
    calibration_state: str
    market_independence_attested: bool
    outcomes: tuple[PdfReportOutcomeV1, PdfReportOutcomeV1]
    context: Mapping[str, object]
    upstream_ranking_entry_checksum: str
    upstream_gate_game_checksum: str
    upstream_value_game_checksum: str
    upstream_prediction_game_checksum: str
    upstream_matchup_packet_game_checksum: str
    upstream_data_quality_game_checksum: str

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise PdfReportProductionError("report game ordinal must be positive")
        if self.decision not in {"recommend", "pass", "avoid"}:
            raise PdfReportProductionError("report decision is invalid")
        recommended = self.decision == "recommend"
        if recommended != (
            self.recommendation_rank is not None
            and self.selected_side in {"home", "away"}
            and self.selected_team_id is not None
        ):
            raise PdfReportProductionError("report recommendation identity mismatch")
        if self.recommendation_rank is not None and (
            isinstance(self.recommendation_rank, bool)
            or not isinstance(self.recommendation_rank, int)
            or self.recommendation_rank < 1
        ):
            raise PdfReportProductionError("recommendation_rank must be positive")
        if tuple(outcome.side for outcome in self.outcomes) != ("home", "away"):
            raise PdfReportProductionError("report must retain both moneyline outcomes")
        if self.market_independence_attested is not True:
            raise PdfReportProductionError("report prediction requires retained market independence")
        object.__setattr__(
            self,
            "scheduled_start_time",
            None if self.scheduled_start_time is None else _utc(self.scheduled_start_time, "scheduled_start_time"),
        )
        object.__setattr__(self, "quality_issue_codes", tuple(self.quality_issue_codes))
        object.__setattr__(self, "context", dict(self.context))
        for name in (
            "upstream_ranking_entry_checksum",
            "upstream_gate_game_checksum",
            "upstream_value_game_checksum",
            "upstream_prediction_game_checksum",
            "upstream_matchup_packet_game_checksum",
            "upstream_data_quality_game_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))

    def identity_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "calibration_state": self.calibration_state,
            "context": dict(self.context),
            "decision": self.decision,
            "home_team_id": self.home_team_id,
            "market_independence_attested": self.market_independence_attested,
            "ordinal": self.ordinal,
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "provider_contract": self.provider_contract,
            "provider_kind": self.provider_kind,
            "provider_version": self.provider_version,
            "quality_disposition": self.quality_disposition,
            "quality_issue_codes": list(self.quality_issue_codes),
            "recommendation_rank": self.recommendation_rank,
            "scheduled_start_time": None
            if self.scheduled_start_time is None
            else self.scheduled_start_time.isoformat(),
            "selected_side": self.selected_side,
            "selected_team_id": self.selected_team_id,
            "source_game_id": self.source_game_id,
            "upstream_data_quality_game_checksum": self.upstream_data_quality_game_checksum,
            "upstream_gate_game_checksum": self.upstream_gate_game_checksum,
            "upstream_matchup_packet_game_checksum": self.upstream_matchup_packet_game_checksum,
            "upstream_prediction_game_checksum": self.upstream_prediction_game_checksum,
            "upstream_ranking_entry_checksum": self.upstream_ranking_entry_checksum,
            "upstream_value_game_checksum": self.upstream_value_game_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ProductionPdfReportV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    generated_at: datetime
    policy: PdfReportPolicyV1
    upstream_snapshot_ids: Mapping[str, str]
    upstream_checksums: Mapping[str, str]
    games: tuple[PdfReportGameV1, ...]
    warnings: tuple[Mapping[str, object], ...] = ()
    report_status: str = "pre_review"
    contract_version: str = PDF_REPORT_PRODUCTION_CONTRACT
    render_version: str = PDF_REPORT_RENDER_VERSION
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        if self.generated_at < self.as_of_time:
            raise PdfReportProductionError("report generated_at cannot precede as_of_time")
        if self.report_status != "pre_review" or self.contract_version != PDF_REPORT_PRODUCTION_CONTRACT:
            raise PdfReportProductionError("unsupported production report identity")
        expected = ("rankings", "recommendation_gate", "value_engine", "predictions", "matchup_packet", "data_quality")
        ids = dict(self.upstream_snapshot_ids)
        checksums = dict(self.upstream_checksums)
        if tuple(ids) != expected or tuple(checksums) != expected:
            raise PdfReportProductionError("report upstream lineage is not exact and ordered")
        for key, checksum in checksums.items():
            checksums[key] = _sha(checksum, f"{key} checksum")
        object.__setattr__(self, "upstream_snapshot_ids", ids)
        object.__setattr__(self, "upstream_checksums", checksums)
        games = tuple(self.games)
        if [game.ordinal for game in games] != list(range(1, len(games) + 1)):
            raise PdfReportProductionError("report games must retain contiguous slate order")
        ranks = sorted(game.recommendation_rank for game in games if game.recommendation_rank is not None)
        if ranks != list(range(1, len(ranks) + 1)):
            raise PdfReportProductionError("report recommendation ranks must be contiguous")
        object.__setattr__(self, "games", games)
        object.__setattr__(self, "warnings", tuple(dict(item) for item in self.warnings))
        payload = self.identity_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured, preserve_field_names=("bookmaker_key", "market_key")) != payload:
            raise PdfReportProductionError("PDF Report contains credential-bearing material")

    @property
    def recommendation_game_ids(self) -> tuple[str, ...]:
        return tuple(
            game.source_game_id
            for game in sorted(
                (item for item in self.games if item.recommendation_rank is not None),
                key=lambda item: item.recommendation_rank or 0,
            )
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "games": [game.as_dict() for game in self.games],
            "generated_at": self.generated_at.isoformat(),
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "recommendation_game_ids": list(self.recommendation_game_ids),
            "render_version": self.render_version,
            "report_status": self.report_status,
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_checksums": dict(self.upstream_checksums),
            "upstream_snapshot_ids": dict(self.upstream_snapshot_ids),
            "warnings": [dict(item) for item in self.warnings],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


def assemble_production_pdf_report(
    *,
    run_id: str,
    rankings_snapshot_id: str,
    rankings: ProductionRankingsV1,
    gate_snapshot_id: str,
    gate: ProductionRecommendationGateV1,
    value_snapshot_id: str,
    value: ProductionValueEngineV1,
    predictions_snapshot_id: str,
    predictions: PredictionsV1,
    matchup_packet_snapshot_id: str,
    matchup_packet: MatchupPacketV1,
    data_quality_snapshot_id: str,
    data_quality: DataQualityV1,
    generated_at: datetime,
    policy: PdfReportPolicyV1 | None = None,
    secret_values: Iterable[str] = (),
) -> ProductionPdfReportV1:
    if not (
        rankings.run_id == gate.run_id == value.run_id == predictions.run_id == run_id
        and len(
            {
                rankings.requested_date,
                gate.requested_date,
                value.requested_date,
                predictions.requested_date,
                matchup_packet.requested_date,
                data_quality.requested_date,
            }
        )
        == 1
        and len(
            {
                rankings.as_of_time,
                gate.as_of_time,
                value.as_of_time,
                predictions.as_of_time,
                matchup_packet.as_of_time,
                data_quality.as_of_time,
            }
        )
        == 1
    ):
        raise PdfReportProductionError("PDF Report run/date/as-of lineage mismatch")
    if rankings.upstream_gate_snapshot_id != gate_snapshot_id or rankings.upstream_gate_checksum != gate.checksum:
        raise PdfReportProductionError("Rankings does not bind the supplied Gate snapshot")
    if gate.upstream_value_snapshot_id != value_snapshot_id or gate.upstream_value_checksum != value.checksum:
        raise PdfReportProductionError("Gate does not bind the supplied Value snapshot")
    if (
        gate.upstream_predictions_snapshot_id != predictions_snapshot_id
        or gate.upstream_predictions_checksum != predictions.checksum
    ):
        raise PdfReportProductionError("Gate does not bind the supplied Predictions snapshot")
    if (
        gate.upstream_data_quality_snapshot_id != data_quality_snapshot_id
        or gate.upstream_data_quality_checksum != data_quality.checksum
    ):
        raise PdfReportProductionError("Gate does not bind the supplied Data Quality snapshot")
    ordered_ids = tuple(entry.source_game_id for entry in rankings.entries)
    inventories = (
        tuple(game.source_game_id for game in gate.games),
        tuple(game.source_game_id for game in value.games),
        tuple(game.source_game_id for game in predictions.games),
        tuple(game.source_game_id for game in matchup_packet.games),
        tuple(game.source_game_id for game in data_quality.games),
    )
    if any(items != ordered_ids for items in inventories):
        raise PdfReportProductionError("PDF Report game inventories or slate order disagree")
    gate_by_id = {game.source_game_id: game for game in gate.games}
    value_by_id = {game.source_game_id: game for game in value.games}
    prediction_by_id = {game.source_game_id: game for game in predictions.games}
    packet_by_id = {game.source_game_id: game for game in matchup_packet.games}
    quality_by_id = {game.source_game_id: game for game in data_quality.games}
    games: list[PdfReportGameV1] = []
    for entry in rankings.entries:
        gate_game = gate_by_id[entry.source_game_id]
        value_game = value_by_id[entry.source_game_id]
        prediction = prediction_by_id[entry.source_game_id]
        packet = packet_by_id[entry.source_game_id]
        quality = quality_by_id[entry.source_game_id]
        if entry.upstream_gate_game_checksum != gate_game.checksum:
            raise PdfReportProductionError("ranking entry changed Gate game evidence")
        if gate_game.value_game_checksum != value_game.checksum or gate_game.prediction_checksum != prediction.checksum:
            raise PdfReportProductionError("Gate game lineage does not reconcile")
        outcomes = tuple(
            PdfReportOutcomeV1(
                side=item.side,
                outcome_team_id=item.outcome_team_id,
                prediction_probability=item.prediction_probability,
                probability_lower=item.probability_lower,
                probability_upper=item.probability_upper,
                availability=item.availability,
                bookmaker_count=len(item.eligible_pairs),
                consensus_no_vig_probability=item.consensus_no_vig_probability,
                best_price=item.best_price,
                best_price_bookmakers=item.best_price_bookmakers,
                edge=item.edge,
                expected_value_per_unit=item.expected_value_per_unit,
                lower_bound_clearance=item.lower_bound_clearance,
                freshness_state=item.freshness_state,
                value_checksum=item.checksum,
                prediction_checksum=item.prediction_checksum,
                market_context_checksum=item.market_context_checksum,
            )
            for item in value_game.outcomes
        )
        schedule = packet.schedule.as_dict()
        packet_payload = packet.as_dict()
        context = {
            "baseball_intelligence": packet_payload["baseball_intelligence"],
            "data_quality": packet_payload["data_quality"],
            "game_state": packet_payload["game_state"],
            "odds_weather": packet_payload["odds_weather"],
            "schedule": schedule,
        }
        games.append(
            PdfReportGameV1(
                ordinal=entry.ordinal,
                source_game_id=entry.source_game_id,
                away_team_id=prediction.away_team_id,
                home_team_id=prediction.home_team_id,
                scheduled_start_time=prediction.scheduled_start_time,
                decision=entry.decision,
                selected_side=entry.selected_side,
                selected_team_id=entry.selected_team_id,
                recommendation_rank=entry.recommendation_rank,
                quality_disposition=quality.disposition.value,
                quality_issue_codes=tuple(issue.code for issue in quality.issues),
                provider_kind=prediction.provider_policy.provider_kind,
                provider_contract=prediction.provider_policy.provider_contract,
                provider_version=prediction.provider_policy.provider_version,
                calibration_state=prediction.provider_policy.calibration_state,
                market_independence_attested=prediction.market_independence_attested,
                outcomes=outcomes,  # type: ignore[arg-type]
                context=context,
                upstream_ranking_entry_checksum=entry.checksum,
                upstream_gate_game_checksum=gate_game.checksum,
                upstream_value_game_checksum=value_game.checksum,
                upstream_prediction_game_checksum=prediction.checksum,
                upstream_matchup_packet_game_checksum=packet.checksum,
                upstream_data_quality_game_checksum=quality.checksum,
            )
        )
    return ProductionPdfReportV1(
        run_id=run_id,
        requested_date=rankings.requested_date,
        as_of_time=rankings.as_of_time,
        generated_at=generated_at,
        policy=PdfReportPolicyV1() if policy is None else policy,
        upstream_snapshot_ids={
            "rankings": rankings_snapshot_id,
            "recommendation_gate": gate_snapshot_id,
            "value_engine": value_snapshot_id,
            "predictions": predictions_snapshot_id,
            "matchup_packet": matchup_packet_snapshot_id,
            "data_quality": data_quality_snapshot_id,
        },
        upstream_checksums={
            "rankings": rankings.checksum,
            "recommendation_gate": gate.checksum,
            "value_engine": value.checksum,
            "predictions": predictions.checksum,
            "matchup_packet": matchup_packet.checksum,
            "data_quality": data_quality.checksum,
        },
        games=tuple(games),
        warnings=tuple(rankings.warnings) + tuple(gate.warnings) + tuple(value.warnings) + tuple(predictions.warnings),
        secret_values=secret_values,
    )
