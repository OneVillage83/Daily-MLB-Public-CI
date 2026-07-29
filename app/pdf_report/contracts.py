from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.data_quality.contracts import DataQualityDisposition
from app.daily_slate.contracts import (
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.pdf_report.policy import PdfReportPolicyV1
from app.recommendation_gate.contracts import RecommendationDecision
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS
from app.value_engine.contracts import ValueMarket, ValueSide

PDF_REPORT_DOCUMENT_CONTRACT_VERSION = "DSE_PDF_REPORT_DOCUMENT_V1"
PDF_REPORT_ROW_CONTRACT_VERSION = "DSE_PDF_REPORT_DECISION_ROW_V1"
PDF_REPORT_GAME_CONTRACT_VERSION = "DSE_PDF_REPORT_GAME_DOSSIER_V1"
PDF_REPORT_FACT_CONTRACT_VERSION = "DSE_PDF_REPORT_FACT_V1"
PDF_REPORT_STATUS = "pre_review"


class PdfReportContractError(ValueError):
    """Raised when PDF Report V1 semantic evidence is inconsistent."""


class ReportBoardType(StrEnum):
    TOP_CONFIDENCE = "top_confidence"
    BEST_VALUE = "best_value"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PdfReportContractError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PdfReportContractError(f"{name} must be lowercase SHA-256")
    return text


def _utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise PdfReportContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PdfReportContractError(f"{name} must be finite numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PdfReportContractError(f"{name} must be finite numeric")
    return numeric


def _optional_number(value: object, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _optional_rank(value: object, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PdfReportContractError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class ReportFactV1:
    label: str
    value: str
    status: str = "available"
    contract_version: str = PDF_REPORT_FACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "label", _text(self.label, "fact label"))
        object.__setattr__(self, "value", _text(self.value, "fact value"))
        object.__setattr__(self, "status", _text(self.status, "fact status"))
        if self.contract_version != PDF_REPORT_FACT_CONTRACT_VERSION:
            raise PdfReportContractError("unsupported report-fact contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "label": self.label,
            "status": self.status,
            "value": self.value,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class PdfReportDecisionRowV1:
    upstream_ranking_entry_checksum: str
    upstream_recommendation_checksum: str
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    market: ValueMarket
    line_key: str
    side: ValueSide
    market_line: float | None
    american_price: float
    decision: RecommendationDecision
    top_confidence_rank: int | None
    best_value_rank: int | None
    conditional_model_probability: float
    no_vig_probability: float | None
    no_vig_probability_edge: float | None
    expected_value_per_unit: float
    expected_roi_percent: float
    evidence_confidence_score: int
    operational_risk_score: int
    bookmaker_count: int
    freshness_status: str
    quality_disposition: DataQualityDisposition
    primary_reason_code: str
    reason_codes: tuple[str, ...]
    customer_explanation: str
    odds_retrieved_at: datetime | None
    review_required: bool
    publication_candidate: bool
    contract_version: str = PDF_REPORT_ROW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for name in (
            "upstream_ranking_entry_checksum",
            "upstream_recommendation_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise PdfReportContractError("decision-row edge_event_id mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise PdfReportContractError("decision-row daily_mlb_game_id mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise PdfReportContractError("decision-row teams are invalid")
        object.__setattr__(self, "line_key", _text(self.line_key, "line_key"))
        object.__setattr__(
            self,
            "market_line",
            _optional_number(self.market_line, "market_line"),
        )
        if self.market is ValueMarket.MONEYLINE and self.market_line is not None:
            raise PdfReportContractError("moneyline cannot have a point")
        if self.market is not ValueMarket.MONEYLINE and self.market_line is None:
            raise PdfReportContractError("spread/total requires a point")
        price = _number(self.american_price, "american_price")
        if abs(price) < 100.0:
            raise PdfReportContractError("invalid American price")
        object.__setattr__(self, "american_price", price)
        top_rank = _optional_rank(self.top_confidence_rank, "top_confidence_rank")
        value_rank = _optional_rank(self.best_value_rank, "best_value_rank")
        object.__setattr__(self, "top_confidence_rank", top_rank)
        object.__setattr__(self, "best_value_rank", value_rank)
        actionable = self.decision in {
            RecommendationDecision.BET,
            RecommendationDecision.LEAN,
        }
        if actionable != (top_rank is not None and value_rank is not None):
            raise PdfReportContractError("actionable decision must retain both ranks")
        if self.review_required != actionable:
            raise PdfReportContractError("review_required mismatch")
        if self.publication_candidate != (self.decision is RecommendationDecision.BET):
            raise PdfReportContractError("publication_candidate mismatch")
        probability = _number(
            self.conditional_model_probability,
            "conditional_model_probability",
        )
        if not 0.0 <= probability <= 1.0:
            raise PdfReportContractError("model probability must be in [0,1]")
        object.__setattr__(self, "conditional_model_probability", probability)
        for name in ("no_vig_probability", "no_vig_probability_edge"):
            object.__setattr__(self, name, _optional_number(getattr(self, name), name))
        if self.no_vig_probability is not None and not 0.0 <= self.no_vig_probability <= 1.0:
            raise PdfReportContractError("no_vig_probability must be in [0,1]")
        for name in ("expected_value_per_unit", "expected_roi_percent"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if not math.isclose(
            self.expected_roi_percent,
            self.expected_value_per_unit * 100.0,
            abs_tol=1e-10,
        ):
            raise PdfReportContractError("ROI disagrees with EV")
        for name in ("evidence_confidence_score", "operational_risk_score"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 100
            ):
                raise PdfReportContractError(f"{name} must be in [0,100]")
        if self.operational_risk_score != 100 - self.evidence_confidence_score:
            raise PdfReportContractError("risk score mismatch")
        if (
            isinstance(self.bookmaker_count, bool)
            or not isinstance(self.bookmaker_count, int)
            or self.bookmaker_count <= 0
        ):
            raise PdfReportContractError("bookmaker_count must be positive")
        object.__setattr__(
            self,
            "freshness_status",
            _text(self.freshness_status, "freshness_status"),
        )
        reason_codes = tuple(_text(item, "reason code") for item in self.reason_codes)
        if not reason_codes or len(set(reason_codes)) != len(reason_codes):
            raise PdfReportContractError("reason_codes must be non-empty and unique")
        object.__setattr__(self, "reason_codes", reason_codes)
        primary = _text(self.primary_reason_code, "primary_reason_code")
        if primary not in reason_codes:
            raise PdfReportContractError("primary reason must be retained in reason_codes")
        object.__setattr__(self, "primary_reason_code", primary)
        object.__setattr__(
            self,
            "customer_explanation",
            _text(self.customer_explanation, "customer_explanation"),
        )
        if self.odds_retrieved_at is not None:
            object.__setattr__(
                self,
                "odds_retrieved_at",
                _utc(self.odds_retrieved_at, "odds_retrieved_at"),
            )
        if self.contract_version != PDF_REPORT_ROW_CONTRACT_VERSION:
            raise PdfReportContractError("unsupported report decision-row contract")

    @property
    def matchup_label(self) -> str:
        return f"{self.away_team_id.upper()} at {self.home_team_id.upper()}"

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def _content_dict(self) -> dict[str, object]:
        return {
            "american_price": self.american_price,
            "away_team_id": self.away_team_id,
            "best_value_rank": self.best_value_rank,
            "bookmaker_count": self.bookmaker_count,
            "conditional_model_probability": self.conditional_model_probability,
            "contract_version": self.contract_version,
            "customer_explanation": self.customer_explanation,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "decision": self.decision.value,
            "edge_event_id": self.edge_event_id,
            "evidence_confidence_score": self.evidence_confidence_score,
            "expected_roi_percent": self.expected_roi_percent,
            "expected_value_per_unit": self.expected_value_per_unit,
            "freshness_status": self.freshness_status,
            "home_team_id": self.home_team_id,
            "line_key": self.line_key,
            "market": self.market.value,
            "market_line": self.market_line,
            "matchup_label": self.matchup_label,
            "no_vig_probability": self.no_vig_probability,
            "no_vig_probability_edge": self.no_vig_probability_edge,
            "odds_retrieved_at": (
                None
                if self.odds_retrieved_at is None
                else self.odds_retrieved_at.isoformat()
            ),
            "operational_risk_score": self.operational_risk_score,
            "primary_reason_code": self.primary_reason_code,
            "publication_candidate": self.publication_candidate,
            "quality_disposition": self.quality_disposition.value,
            "reason_codes": list(self.reason_codes),
            "review_required": self.review_required,
            "side": self.side.value,
            "source_game_id": self.source_game_id,
            "top_confidence_rank": self.top_confidence_rank,
            "upstream_ranking_entry_checksum": self.upstream_ranking_entry_checksum,
            "upstream_recommendation_checksum": self.upstream_recommendation_checksum,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PdfReportGameDossierV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    title: str
    quality_disposition: DataQualityDisposition
    game_facts: tuple[ReportFactV1, ...]
    starter_facts: tuple[ReportFactV1, ...]
    lineup_facts: tuple[ReportFactV1, ...]
    bullpen_facts: tuple[ReportFactV1, ...]
    weather_facts: tuple[ReportFactV1, ...]
    odds_facts: tuple[ReportFactV1, ...]
    model_facts: tuple[ReportFactV1, ...]
    decisions: tuple[PdfReportDecisionRowV1, ...]
    analysis: tuple[str, ...]
    limitations: tuple[str, ...]
    upstream_matchup_packet_game_checksum: str
    upstream_prediction_game_checksum: str
    upstream_recommendation_game_checksum: str
    upstream_ranking_game_checksum: str
    contract_version: str = PDF_REPORT_GAME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise PdfReportContractError("dossier edge_event_id mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise PdfReportContractError("dossier daily_mlb_game_id mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise PdfReportContractError("dossier teams are invalid")
        object.__setattr__(self, "title", _text(self.title, "dossier title"))
        for name in (
            "game_facts",
            "starter_facts",
            "lineup_facts",
            "bullpen_facts",
            "weather_facts",
            "odds_facts",
            "model_facts",
        ):
            facts = tuple(getattr(self, name))
            if len({fact.label for fact in facts}) != len(facts):
                raise PdfReportContractError(f"{name} contains duplicate labels")
            object.__setattr__(self, name, facts)
        decisions = tuple(self.decisions)
        if any(item.source_game_id != source_game_id for item in decisions):
            raise PdfReportContractError("dossier decision belongs to another game")
        if len({item.upstream_ranking_entry_checksum for item in decisions}) != len(decisions):
            raise PdfReportContractError("dossier contains duplicate decisions")
        object.__setattr__(self, "decisions", decisions)
        analysis = tuple(_text(item, "analysis sentence") for item in self.analysis)
        if not analysis:
            raise PdfReportContractError("dossier requires customer analysis")
        object.__setattr__(self, "analysis", analysis)
        limitations = tuple(_text(item, "limitation") for item in self.limitations)
        object.__setattr__(self, "limitations", limitations)
        for name in (
            "upstream_matchup_packet_game_checksum",
            "upstream_prediction_game_checksum",
            "upstream_recommendation_game_checksum",
            "upstream_ranking_game_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        if self.contract_version != PDF_REPORT_GAME_CONTRACT_VERSION:
            raise PdfReportContractError("unsupported report game-dossier contract")

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def _content_dict(self) -> dict[str, object]:
        return {
            "analysis": list(self.analysis),
            "away_team_id": self.away_team_id,
            "bullpen_facts": [item.as_dict() for item in self.bullpen_facts],
            "contract_version": self.contract_version,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "decisions": [item.as_dict() for item in self.decisions],
            "edge_event_id": self.edge_event_id,
            "game_facts": [item.as_dict() for item in self.game_facts],
            "home_team_id": self.home_team_id,
            "limitations": list(self.limitations),
            "lineup_facts": [item.as_dict() for item in self.lineup_facts],
            "model_facts": [item.as_dict() for item in self.model_facts],
            "odds_facts": [item.as_dict() for item in self.odds_facts],
            "quality_disposition": self.quality_disposition.value,
            "source_game_id": self.source_game_id,
            "starter_facts": [item.as_dict() for item in self.starter_facts],
            "title": self.title,
            "upstream_matchup_packet_game_checksum": (
                self.upstream_matchup_packet_game_checksum
            ),
            "upstream_prediction_game_checksum": self.upstream_prediction_game_checksum,
            "upstream_ranking_game_checksum": self.upstream_ranking_game_checksum,
            "upstream_recommendation_game_checksum": (
                self.upstream_recommendation_game_checksum
            ),
            "weather_facts": [item.as_dict() for item in self.weather_facts],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class PdfReportDocumentV1:
    requested_date: str
    as_of_time: datetime
    generated_at: datetime
    policy: PdfReportPolicyV1
    upstream_rankings_checksum: str
    upstream_recommendation_gate_checksum: str
    upstream_predictions_checksum: str
    upstream_matchup_packet_checksum: str
    top_confidence_row_checksums: tuple[str, ...]
    best_value_row_checksums: tuple[str, ...]
    full_slate_rows: tuple[PdfReportDecisionRowV1, ...]
    slate_health: tuple[ReportFactV1, ...]
    games: tuple[PdfReportGameDossierV1, ...]
    methodology: tuple[str, ...]
    responsible_use_notice: str
    secret_values: InitVar[Iterable[str]] = ()
    report_status: str = PDF_REPORT_STATUS
    contract_version: str = PDF_REPORT_DOCUMENT_CONTRACT_VERSION
    sport: str = "MLB"
    league: str = "MLB"

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _utc(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "generated_at", _utc(self.generated_at, "generated_at"))
        if self.generated_at < self.as_of_time:
            raise PdfReportContractError("generated_at cannot precede as_of_time")
        for name in (
            "upstream_rankings_checksum",
            "upstream_recommendation_gate_checksum",
            "upstream_predictions_checksum",
            "upstream_matchup_packet_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        rows = tuple(self.full_slate_rows)
        if len({row.upstream_ranking_entry_checksum for row in rows}) != len(rows):
            raise PdfReportContractError("full slate contains duplicate ranking entries")
        object.__setattr__(self, "full_slate_rows", rows)
        by_checksum = {row.checksum: row for row in rows}
        top = tuple(_sha(item, "top confidence row checksum") for item in self.top_confidence_row_checksums)
        best = tuple(_sha(item, "best value row checksum") for item in self.best_value_row_checksums)
        if len(set(top)) != len(top) or len(set(best)) != len(best):
            raise PdfReportContractError("rank board contains duplicate rows")
        if any(item not in by_checksum for item in (*top, *best)):
            raise PdfReportContractError("rank board references unknown report row")
        if set(top) != set(best):
            raise PdfReportContractError("Top Confidence and Best Value inventory mismatch")
        eligible = {row.checksum for row in rows if row.top_confidence_rank is not None}
        if set(top) != eligible:
            raise PdfReportContractError("ranked-board inventory disagrees with actionable rows")
        for index, checksum in enumerate(top, start=1):
            if by_checksum[checksum].top_confidence_rank != index:
                raise PdfReportContractError("Top Confidence rank order mismatch")
        for index, checksum in enumerate(best, start=1):
            if by_checksum[checksum].best_value_rank != index:
                raise PdfReportContractError("Best Value rank order mismatch")
        object.__setattr__(self, "top_confidence_row_checksums", top)
        object.__setattr__(self, "best_value_row_checksums", best)
        health = tuple(self.slate_health)
        if not health or len({fact.label for fact in health}) != len(health):
            raise PdfReportContractError("slate health facts must be non-empty and unique")
        object.__setattr__(self, "slate_health", health)
        games = tuple(self.games)
        if len({game.source_game_id for game in games}) != len(games):
            raise PdfReportContractError("report contains duplicate game dossiers")
        object.__setattr__(self, "games", games)
        dossier_rows = {
            row.upstream_ranking_entry_checksum
            for game in games
            for row in game.decisions
        }
        full_rows = {row.upstream_ranking_entry_checksum for row in rows}
        if dossier_rows != full_rows:
            raise PdfReportContractError("game dossiers do not retain the full slate inventory")
        if any(row.source_game_id not in {game.source_game_id for game in games} for row in rows):
            raise PdfReportContractError("full-slate row has no game dossier")
        methodology = tuple(_text(item, "methodology statement") for item in self.methodology)
        if not methodology:
            raise PdfReportContractError("methodology appendix cannot be empty")
        object.__setattr__(self, "methodology", methodology)
        object.__setattr__(
            self,
            "responsible_use_notice",
            _text(self.responsible_use_notice, "responsible_use_notice"),
        )
        if self.report_status != PDF_REPORT_STATUS:
            raise PdfReportContractError("PDF Report V1 must remain pre_review")
        if self.contract_version != PDF_REPORT_DOCUMENT_CONTRACT_VERSION:
            raise PdfReportContractError("unsupported PDF Report document contract")
        if self.sport != "MLB" or self.league != "MLB":
            raise PdfReportContractError("sport and league must be MLB")
        payload = self._content_dict()
        configured = tuple(str(item) for item in secret_values if str(item))
        if redact_value(payload, configured, preserve_field_names=("key",)) != payload:
            raise PdfReportContractError("PDF Report document contains credentials")

    @property
    def row_count(self) -> int:
        return len(self.full_slate_rows)

    @property
    def game_count(self) -> int:
        return len(self.games)

    @property
    def actionable_count(self) -> int:
        return len(self.top_confidence_row_checksums)

    def _content_dict(self) -> dict[str, object]:
        return {
            "actionable_count": self.actionable_count,
            "as_of_time": self.as_of_time.isoformat(),
            "best_value_row_checksums": list(self.best_value_row_checksums),
            "contract_version": self.contract_version,
            "full_slate_rows": [row.as_dict() for row in self.full_slate_rows],
            "game_count": self.game_count,
            "games": [game.as_dict() for game in self.games],
            "generated_at": self.generated_at.isoformat(),
            "league": self.league,
            "methodology": list(self.methodology),
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "report_status": self.report_status,
            "requested_date": self.requested_date,
            "responsible_use_notice": self.responsible_use_notice,
            "row_count": self.row_count,
            "slate_health": [fact.as_dict() for fact in self.slate_health],
            "sport": self.sport,
            "top_confidence_row_checksums": list(self.top_confidence_row_checksums),
            "upstream_matchup_packet_checksum": self.upstream_matchup_packet_checksum,
            "upstream_predictions_checksum": self.upstream_predictions_checksum,
            "upstream_rankings_checksum": self.upstream_rankings_checksum,
            "upstream_recommendation_gate_checksum": (
                self.upstream_recommendation_gate_checksum
            ),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
