from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from app.analysis.models import checksum_payload, json_value, utc_datetime
from app.identifiers import validate_run_id

REVIEWED_ANALYST_VERSION = "DSE_REVIEWED_ANALYST_V1"
MINIMUM_MANUAL_INTERVAL_WIDTH = 0.10
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class EvidenceState(str, Enum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"


class LineupInformationState(str, Enum):
    CONFIRMED = "confirmed"
    PROJECTED = "projected"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_id: str
    source_name: str
    reference: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    state: EvidenceState
    assessment: str | None
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalystEvidenceBundle:
    analyst_identity: str
    method_version: str
    starting_pitching: EvidenceAssessment
    bullpen: EvidenceAssessment
    offensive_matchup: EvidenceAssessment
    lineup_information_state: LineupInformationState
    lineup_notes: str | None
    lineup_source_ids: tuple[str, ...]
    venue_context: EvidenceAssessment
    weather_context: EvidenceAssessment
    schedule_rest_context: EvidenceAssessment
    material_unknowns: tuple[str, ...]
    source_provenance: tuple[SourceReference, ...]


@dataclass(frozen=True, slots=True)
class ReviewedPredictionInput:
    run_id: str
    event_id: str
    home_team_key: str
    away_team_key: str
    home_probability: float
    home_probability_lower: float
    home_probability_upper: float
    generated_at: datetime
    feature_checksum: str
    market_independence_attested: bool
    evidence: AnalystEvidenceBundle

    def analyst_input_view(self) -> dict[str, Any]:
        """Serialize prediction authoring data without any market evaluation fields."""
        return {
            "contract_version": REVIEWED_ANALYST_VERSION,
            "run_id": self.run_id,
            "event_id": self.event_id,
            "home_team_key": self.home_team_key,
            "away_team_key": self.away_team_key,
            "home_probability": self.home_probability,
            "home_probability_lower": self.home_probability_lower,
            "home_probability_upper": self.home_probability_upper,
            "generated_at": utc_datetime(self.generated_at, field="generated_at").isoformat(),
            "feature_checksum": self.feature_checksum,
            "market_independence_attested": self.market_independence_attested,
            "evidence": json_value(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SealedPrediction:
    contract_version: str
    prediction_id: str
    run_id: str
    event_id: str
    home_team_key: str
    away_team_key: str
    home_probability: float
    away_probability: float
    home_probability_lower: float
    home_probability_upper: float
    away_probability_lower: float
    away_probability_upper: float
    generated_at: datetime
    sealed_at: datetime
    feature_checksum: str
    market_independence_attested: bool
    evidence: AnalystEvidenceBundle
    evidence_checksum: str
    checksum: str

    def as_dict(self) -> dict[str, Any]:
        payload = dict(json_value(self))
        payload["lower_bound"] = self.home_probability_lower
        payload["upper_bound"] = self.home_probability_upper
        payload["analyst_id"] = self.evidence.analyst_identity
        payload["method_version"] = self.evidence.method_version
        payload["prediction_checksum"] = self.checksum
        return payload


def _nonempty(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be empty")


def _validate_assessment(
    value: EvidenceAssessment,
    field: str,
    source_ids: set[str],
) -> None:
    unknown_sources = set(value.source_ids) - source_ids
    if unknown_sources:
        raise ValueError(f"{field} references unknown source IDs")
    if value.state is EvidenceState.AVAILABLE:
        if value.assessment is None or not value.assessment.strip():
            raise ValueError(f"{field} available assessment must include details")
        if not value.source_ids:
            raise ValueError(f"{field} available assessment must cite a source")
    elif value.assessment is not None and value.assessment.strip():
        raise ValueError(f"{field} unknown assessment must not claim known details")


def _validate_evidence(value: AnalystEvidenceBundle) -> AnalystEvidenceBundle:
    _nonempty(value.analyst_identity, "analyst_identity")
    if value.method_version != REVIEWED_ANALYST_VERSION:
        raise ValueError(f"method_version must be {REVIEWED_ANALYST_VERSION}")
    if not value.source_provenance:
        raise ValueError("source_provenance must contain at least one source")
    identifiers = [source.source_id for source in value.source_provenance]
    if any(not source_id.strip() for source_id in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("source IDs must be nonempty and unique")
    source_ids = set(identifiers)
    for source in value.source_provenance:
        _nonempty(source.source_name, "source_name")
        _nonempty(source.reference, "source reference")
        utc_datetime(source.observed_at, field="source observed_at")
    for field in (
        "starting_pitching",
        "bullpen",
        "offensive_matchup",
        "venue_context",
        "weather_context",
        "schedule_rest_context",
    ):
        _validate_assessment(getattr(value, field), field, source_ids)
    if set(value.lineup_source_ids) - source_ids:
        raise ValueError("lineup information references unknown source IDs")
    if value.lineup_information_state in {
        LineupInformationState.CONFIRMED,
        LineupInformationState.PROJECTED,
    }:
        if value.lineup_notes is None or not value.lineup_notes.strip() or not value.lineup_source_ids:
            raise ValueError("known lineup information must include notes and source IDs")
    elif value.lineup_notes is not None and value.lineup_notes.strip():
        raise ValueError("unknown or unavailable lineup information must not claim known details")
    if any(not item.strip() for item in value.material_unknowns):
        raise ValueError("material_unknowns must not contain empty values")
    return value


def _prediction_content(
    value: ReviewedPredictionInput,
    *,
    evidence_checksum: str,
) -> dict[str, Any]:
    payload = value.analyst_input_view()
    payload["evidence_checksum"] = evidence_checksum
    return payload


def seal_prediction(
    value: ReviewedPredictionInput,
    *,
    sealed_at: datetime | str,
) -> SealedPrediction:
    safe_run_id = validate_run_id(value.run_id)
    _nonempty(value.event_id, "event_id")
    _nonempty(value.home_team_key, "home_team_key")
    _nonempty(value.away_team_key, "away_team_key")
    if value.home_team_key == value.away_team_key:
        raise ValueError("home and away team keys must differ")
    if _SHA256_PATTERN.fullmatch(value.feature_checksum) is None:
        raise ValueError("feature_checksum must be a 64-character lowercase SHA-256 checksum")
    probabilities = (
        value.home_probability,
        value.home_probability_lower,
        value.home_probability_upper,
    )
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in probabilities):
        raise ValueError("probability and bounds must be finite values from 0.0 through 1.0")
    if not value.home_probability_lower <= value.home_probability <= value.home_probability_upper:
        raise ValueError("home probability must fall within its bounds")
    interval_width = value.home_probability_upper - value.home_probability_lower
    if interval_width + 1e-12 < MINIMUM_MANUAL_INTERVAL_WIDTH:
        raise ValueError("DSE_REVIEWED_ANALYST_V1 requires a probability interval at least 0.10 wide")
    generated = utc_datetime(value.generated_at, field="generated_at")
    sealed = utc_datetime(sealed_at, field="sealed_at")
    if generated > sealed:
        raise ValueError("generated_at cannot be after sealed_at")
    evidence = _validate_evidence(value.evidence)
    normalized = ReviewedPredictionInput(
        run_id=safe_run_id,
        event_id=value.event_id.strip(),
        home_team_key=value.home_team_key.strip(),
        away_team_key=value.away_team_key.strip(),
        home_probability=float(value.home_probability),
        home_probability_lower=float(value.home_probability_lower),
        home_probability_upper=float(value.home_probability_upper),
        generated_at=generated,
        feature_checksum=value.feature_checksum,
        market_independence_attested=bool(value.market_independence_attested),
        evidence=evidence,
    )
    evidence_checksum = checksum_payload(
        {
            "run_id": normalized.run_id,
            "event_id": normalized.event_id,
            "evidence": evidence,
        }
    )
    content = _prediction_content(normalized, evidence_checksum=evidence_checksum)
    checksum = checksum_payload(content)
    return SealedPrediction(
        contract_version=REVIEWED_ANALYST_VERSION,
        prediction_id=f"pred_{checksum[:24]}",
        run_id=normalized.run_id,
        event_id=normalized.event_id,
        home_team_key=normalized.home_team_key,
        away_team_key=normalized.away_team_key,
        home_probability=normalized.home_probability,
        away_probability=1.0 - normalized.home_probability,
        home_probability_lower=normalized.home_probability_lower,
        home_probability_upper=normalized.home_probability_upper,
        away_probability_lower=1.0 - normalized.home_probability_upper,
        away_probability_upper=1.0 - normalized.home_probability_lower,
        generated_at=generated,
        sealed_at=sealed,
        feature_checksum=normalized.feature_checksum,
        market_independence_attested=normalized.market_independence_attested,
        evidence=evidence,
        evidence_checksum=evidence_checksum,
        checksum=checksum,
    )


def verify_prediction_checksum(value: SealedPrediction) -> bool:
    try:
        validate_run_id(value.run_id)
        if _SHA256_PATTERN.fullmatch(value.feature_checksum) is None:
            return False
        if value.generated_at > value.sealed_at:
            return False
        expected_away_probability = 1.0 - value.home_probability
        expected_away_lower = 1.0 - value.home_probability_upper
        expected_away_upper = 1.0 - value.home_probability_lower
        if not (
            math.isclose(
                value.away_probability,
                expected_away_probability,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            and math.isclose(
                value.away_probability_lower,
                expected_away_lower,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            and math.isclose(
                value.away_probability_upper,
                expected_away_upper,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return False
        _nonempty(value.event_id, "event_id")
        _nonempty(value.home_team_key, "home_team_key")
        _nonempty(value.away_team_key, "away_team_key")
        if value.home_team_key == value.away_team_key:
            return False
        candidate = ReviewedPredictionInput(
            run_id=value.run_id,
            event_id=value.event_id,
            home_team_key=value.home_team_key,
            away_team_key=value.away_team_key,
            home_probability=value.home_probability,
            home_probability_lower=value.home_probability_lower,
            home_probability_upper=value.home_probability_upper,
            generated_at=value.generated_at,
            feature_checksum=value.feature_checksum,
            market_independence_attested=value.market_independence_attested,
            evidence=value.evidence,
        )
        _validate_evidence(value.evidence)
        probabilities = (
            value.home_probability,
            value.home_probability_lower,
            value.home_probability_upper,
        )
        if any(
            not math.isfinite(item) or item < 0.0 or item > 1.0
            for item in probabilities
        ):
            return False
        if not value.home_probability_lower <= value.home_probability <= value.home_probability_upper:
            return False
        if (
            value.home_probability_upper - value.home_probability_lower
            + 1e-12
            < MINIMUM_MANUAL_INTERVAL_WIDTH
        ):
            return False
        evidence_checksum = checksum_payload(
            {
                "run_id": value.run_id,
                "event_id": value.event_id,
                "evidence": value.evidence,
            }
        )
        expected_checksum = checksum_payload(
            _prediction_content(candidate, evidence_checksum=evidence_checksum)
        )
        return (
            value.contract_version == REVIEWED_ANALYST_VERSION
            and evidence_checksum == value.evidence_checksum
            and expected_checksum == value.checksum
            and value.prediction_id == f"pred_{expected_checksum[:24]}"
        )
    except (TypeError, ValueError):
        return False
