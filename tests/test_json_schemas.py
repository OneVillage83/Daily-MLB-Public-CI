from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

import pytest

from app.analysis.canonical import CanonicalGame, MlbFeatures
from app.analysis.models import CandidateDecision, DataQualityState, GateResult, UncertaintyGrade
from app.analysis.policy import (
    REQUIRED_CANDIDATE_GATE_CODES,
    MoneylineSideEvaluation,
    PolicyEvaluation,
)
from app.analysis.predictions import (
    AnalystEvidenceBundle,
    EvidenceAssessment,
    EvidenceState,
    LineupInformationState,
    SealedPrediction,
    SourceReference,
)
from app.publication import approve_daily_card, create_daily_card_draft


SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
SCHEMA_FILES = {
    "canonical": "canonical-game-v1.schema.json",
    "features": "mlb-features-v1.schema.json",
    "prediction": "DSE_REVIEWED_ANALYST_V1.schema.json",
    "policy": "DSE_MLB_ML_CANDIDATE_V1.schema.json",
    "card": "daily-card-v1.schema.json",
    "publication": "publication-package-v1.schema.json",
}
NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
FIRST_PITCH = NOW + timedelta(hours=3)
RUN_ID = "run_20260716_0123456789abcdef0123456789abcdef"
POLICY = "DSE_MLB_ML_CANDIDATE_V1"


def _load_schema(name: str) -> dict[str, Any]:
    parsed = json.loads((SCHEMA_ROOT / SCHEMA_FILES[name]).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _resolve_ref(root: Mapping[str, Any], reference: str) -> Mapping[str, Any]:
    assert reference.startswith("#/")
    current: Any = root
    for part in reference[2:].split("/"):
        assert isinstance(current, Mapping)
        current = current[part.replace("~1", "/").replace("~0", "~")]
    assert isinstance(current, Mapping)
    return current


def _is_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    return checks[expected]()


def _validate(instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any], path: str = "$") -> None:
    if "$ref" in schema:
        _validate(instance, _resolve_ref(root, str(schema["$ref"])), root, path)
        return
    if "const" in schema:
        assert instance == schema["const"], f"{path}: expected constant {schema['const']!r}"
    if "enum" in schema:
        assert instance in schema["enum"], f"{path}: value not in enum"
    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = [expected_type] if isinstance(expected_type, str) else expected_type
        assert any(_is_type(instance, item) for item in expected_types), f"{path}: wrong JSON type"
    if isinstance(instance, dict):
        required = schema.get("required", [])
        assert all(key in instance for key in required), f"{path}: missing required property"
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                _validate(value, properties[key], root, f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise AssertionError(f"{path}: unexpected property {key}")
            elif isinstance(schema.get("additionalProperties"), Mapping):
                _validate(value, schema["additionalProperties"], root, f"{path}.{key}")
    if isinstance(instance, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(instance):
            _validate(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(instance, str):
        if "minLength" in schema:
            assert len(instance) >= int(schema["minLength"]), f"{path}: string too short"
        if "pattern" in schema:
            assert re.fullmatch(str(schema["pattern"]), instance), f"{path}: pattern mismatch"
        if schema.get("format") == "date":
            date.fromisoformat(instance)
        if schema.get("format") == "date-time":
            parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema:
            assert instance >= schema["minimum"], f"{path}: below minimum"
        if "maximum" in schema:
            assert instance <= schema["maximum"], f"{path}: above maximum"


def _assert_schema_references_exist(schema: Mapping[str, Any], root: Mapping[str, Any]) -> None:
    reference = schema.get("$ref")
    if reference is not None:
        _resolve_ref(root, str(reference))
    for value in schema.values():
        if isinstance(value, Mapping):
            _assert_schema_references_exist(value, root)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    _assert_schema_references_exist(item, root)


def _representative_outputs() -> dict[str, dict[str, Any]]:
    canonical_checksum = "a" * 64
    feature_checksum = "b" * 64
    prediction_checksum = "d" * 64
    evaluation_checksum = "e" * 64
    evidence_checksum = "c" * 64
    unknown = EvidenceAssessment(EvidenceState.UNKNOWN, None)
    evidence = AnalystEvidenceBundle(
        analyst_identity="owner-1",
        method_version="DSE_REVIEWED_ANALYST_V1",
        starting_pitching=unknown,
        bullpen=unknown,
        offensive_matchup=unknown,
        lineup_information_state=LineupInformationState.UNKNOWN,
        lineup_notes=None,
        lineup_source_ids=(),
        venue_context=unknown,
        weather_context=unknown,
        schedule_rest_context=unknown,
        material_unknowns=("starting pitchers unconfirmed",),
        source_provenance=(SourceReference("source-1", "owner review", "local", NOW),),
    )
    canonical = CanonicalGame(
        contract_version="canonical-game-v1",
        run_id=RUN_ID,
        event_id="event-1",
        requested_date=NOW.date(),
        commence_time=FIRST_PITCH,
        raw_home_team="Los Angeles Dodgers",
        raw_away_team="San Francisco Giants",
        home_team_key="LAD",
        away_team_key="SFG",
        venue_context={"name": "Dodger Stadium"},
        roof_context="outdoor",
        odds_consensus={"markets": {}},
        weather_context={"temperature_f": 78.0},
        weather_gate_clear=True,
        quality_state=DataQualityState.READY,
        quality_issues=(),
        source_checksums={"odds": "f" * 64, "weather": "0" * 64},
        assembled_at=NOW,
        checksum=canonical_checksum,
    )
    features = MlbFeatures(
        contract_version="mlb-features-v1",
        event_id="event-1",
        canonical_game_checksum=canonical_checksum,
        schedule_context={"commence_time": FIRST_PITCH.isoformat()},
        team_context={"home_team_key": "LAD", "away_team_key": "SFG"},
        venue_context={"name": "Dodger Stadium"},
        weather_context={"temperature_f": 78.0},
        market_context={"market_key": "h2h"},
        data_quality_state=DataQualityState.READY,
        uncertainty_flags=("starting_pitcher_unknown",),
        generated_at=NOW,
        checksum=feature_checksum,
    )
    prediction = SealedPrediction(
        contract_version="DSE_REVIEWED_ANALYST_V1",
        prediction_id="prediction-1",
        run_id=RUN_ID,
        event_id="event-1",
        home_team_key="LAD",
        away_team_key="SFG",
        home_probability=0.58,
        away_probability=0.42,
        home_probability_lower=0.52,
        home_probability_upper=0.63,
        away_probability_lower=0.37,
        away_probability_upper=0.48,
        generated_at=NOW,
        sealed_at=NOW,
        feature_checksum=feature_checksum,
        market_independence_attested=True,
        evidence=evidence,
        evidence_checksum=evidence_checksum,
        checksum=prediction_checksum,
    )
    side = MoneylineSideEvaluation("LAD", 0.58, 0.52, 0.63, 0.53, -110, ("book-a",), 0.5238, 5.0, 0.107)
    evaluation = PolicyEvaluation(
        policy_version=POLICY,
        evaluation_id="evaluation-1",
        event_id="event-1",
        prediction_id="prediction-1",
        prediction_checksum=prediction_checksum,
        canonical_game_checksum=canonical_checksum,
        evaluated_at=NOW,
        selected_team_key="LAD",
        side_evaluations=(side,),
        prediction_probability=0.58,
        probability_lower=0.52,
        probability_upper=0.63,
        market_no_vig_probability=0.53,
        best_price=-110,
        best_price_books=("book-a",),
        break_even_probability=0.5238,
        edge_percentage_points=5.0,
        expected_value_per_unit_risk=0.107,
        eligible_bookmaker_count=5,
        eligible_bookmakers=("book-a", "book-b", "book-c", "book-d", "book-e"),
        confidence_grade=UncertaintyGrade.B,
        gate_results=tuple(
            GateResult(code, True, True, True, f"{code} passed")
            for code in REQUIRED_CANDIDATE_GATE_CODES
        ),
        decision=CandidateDecision.CANDIDATE_REQUIRES_REVIEW,
        checksum=evaluation_checksum,
    )
    draft = create_daily_card_draft(
        run_id=RUN_ID,
        requested_date=NOW.date(),
        policy_version=POLICY,
        canonical_games=[canonical],
        features=[features],
        sealed_predictions=[prediction],
        policy_evaluations=[evaluation],
        generated_at=NOW,
    )
    item = draft["items"][0]
    package = approve_daily_card(
        draft,
        reviewer_id="owner-1",
        decisions={"event-1": {"decision": "APPROVE", "reason": "reviewed"}},
        batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
        current_event_states={
            "event-1": {
                "event_status": "pregame",
                "best_price_fresh": True,
                "best_price": -110,
                "best_price_books": ["book-a"],
                "scheduled_first_pitch_utc": FIRST_PITCH.isoformat(),
                "policy_version": POLICY,
                "prediction_checksum": prediction_checksum,
                "evidence_checksum": evidence_checksum,
                "policy_evaluation_checksum": evaluation_checksum,
                "required_source_checksums": item["required_source_checksums"],
                "current_policy_evaluation": evaluation.as_dict(),
            }
        },
        current_policy_version=POLICY,
        approved_at=NOW + timedelta(minutes=1),
    ).as_dict()
    return {
        "canonical": canonical.as_dict(),
        "features": features.as_dict(),
        "prediction": prediction.as_dict(),
        "policy": evaluation.as_dict(),
        "card": draft,
        "publication": package,
    }


def test_schema_documents_are_valid_json_with_resolvable_local_references() -> None:
    assert set(path.name for path in SCHEMA_ROOT.glob("*.schema.json")) >= set(SCHEMA_FILES.values())
    for name in SCHEMA_FILES:
        schema = _load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["type"] == "object"
        _assert_schema_references_exist(schema, schema)


def test_representative_domain_and_publication_outputs_satisfy_contracts() -> None:
    for name, output in _representative_outputs().items():
        schema = _load_schema(name)
        _validate(output, schema, schema)


def test_contracts_reject_version_drift_and_formatted_numeric_values() -> None:
    outputs = _representative_outputs()
    prediction = deepcopy(outputs["prediction"])
    prediction["contract_version"] = "DSE_REVIEWED_ANALYST_V2"
    with pytest.raises(AssertionError):
        schema = _load_schema("prediction")
        _validate(prediction, schema, schema)

    policy = deepcopy(outputs["policy"])
    policy["edge_percentage_points"] = "5.0 pp"
    with pytest.raises(AssertionError):
        schema = _load_schema("policy")
        _validate(policy, schema, schema)
