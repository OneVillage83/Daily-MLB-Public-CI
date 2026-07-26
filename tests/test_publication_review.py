from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.analysis import REQUIRED_CANDIDATE_GATE_CODES
from app.publication import (
    ApprovalFailure,
    ApprovalFailureCode,
    approve_daily_card,
    create_daily_card_draft,
    payload_checksum,
)


NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)
FIRST_PITCH = NOW + timedelta(hours=3)
POLICY_VERSION = "DSE_MLB_ML_CANDIDATE_V1"


def _analyst_evidence() -> dict[str, Any]:
    unknown: dict[str, Any] = {
        "state": "unknown",
        "assessment": None,
        "source_ids": [],
    }
    return {
        "analyst_identity": "owner-1",
        "method_version": "DSE_REVIEWED_ANALYST_V1",
        "starting_pitching": dict(unknown),
        "bullpen": dict(unknown),
        "offensive_matchup": dict(unknown),
        "lineup_information_state": "unknown",
        "lineup_notes": None,
        "lineup_source_ids": [],
        "venue_context": dict(unknown),
        "weather_context": dict(unknown),
        "schedule_rest_context": dict(unknown),
        "material_unknowns": ["lineup confirmation pending"],
        "source_provenance": [
            {
                "source_id": "source-1",
                "source_name": "Owner review fixture",
                "reference": "local-test",
                "observed_at": NOW.isoformat(),
            }
        ],
    }


def _inputs(*, status: str = "CANDIDATE_REQUIRES_REVIEW") -> dict[str, Any]:
    gates = [
        {
            "code": code,
            "passed": status == "CANDIDATE_REQUIRES_REVIEW",
            "threshold": True,
            "observed_value": status == "CANDIDATE_REQUIRES_REVIEW",
            "reason": f"{code} evaluated",
        }
        for code in REQUIRED_CANDIDATE_GATE_CODES
    ]
    return {
        "run_id": "run_20260716_0123456789abcdef0123456789abcdef",
        "requested_date": "2026-07-16",
        "policy_version": POLICY_VERSION,
        "canonical_games": [
            {
                "contract_version": "canonical-game-v1",
                "event_id": "event-1",
                "commence_time": FIRST_PITCH.isoformat(),
                "home_team_key": "LAD",
                "away_team_key": "SFG",
                "source_checksums": {"odds": "odds-source", "weather": "weather-source"},
                "checksum": "canonical-checksum",
            }
        ],
        "features": [
            {
                "contract_version": "mlb-features-v1",
                "event_id": "event-1",
                "checksum": "feature-checksum",
            }
        ],
        "sealed_predictions": [
            {
                "contract_version": "DSE_REVIEWED_ANALYST_V1",
                "prediction_id": "prediction-1",
                "event_id": "event-1",
                "feature_checksum": "feature-checksum",
                "evidence": _analyst_evidence(),
                "evidence_checksum": "evidence-checksum",
                "checksum": "prediction-checksum",
            }
        ],
        "policy_evaluations": [
            {
                "evaluation_id": "evaluation-1",
                "event_id": "event-1",
                "prediction_id": "prediction-1",
                "prediction_checksum": "prediction-checksum",
                "policy_version": POLICY_VERSION,
                "selected_team_key": "LAD",
                "prediction_probability": 0.58,
                "prediction_lower_bound": 0.52,
                "prediction_upper_bound": 0.63,
                "confidence_grade": "B",
                "market_no_vig_probability": 0.53,
                "best_price": -110,
                "best_price_books": ["book-a"],
                "break_even_probability": 0.5238095238,
                "edge_percentage_points": 5.0,
                "expected_value_per_unit_risk": 0.1072727273,
                "eligible_bookmaker_count": 5,
                "gate_results": gates,
                "decision": status,
                "checksum": "evaluation-checksum",
            }
        ],
        "generated_at": NOW,
    }


def _draft(*, status: str = "CANDIDATE_REQUIRES_REVIEW") -> dict[str, Any]:
    return create_daily_card_draft(**_inputs(status=status))


def _current_state(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    item = draft["items"][0]
    return {
        "event-1": {
            "event_status": "pregame",
            "best_price_fresh": True,
            "best_price": item["market_evaluation"]["best_price"],
            "best_price_books": item["market_evaluation"]["best_price_books"],
            "scheduled_first_pitch_utc": FIRST_PITCH.isoformat(),
            "policy_version": POLICY_VERSION,
            "prediction_checksum": item["prediction_checksum"],
            "evidence_checksum": item["evidence_checksum"],
            "policy_evaluation_checksum": item["policy_evaluation_checksum"],
            "required_source_checksums": item["required_source_checksums"],
            "current_policy_evaluation": {
                "decision": item["automated_status"],
                "selected_team_key": item["selected_team_key"],
                **item["market_evaluation"],
                "gate_results": deepcopy(item["gate_results"]),
            },
        }
    }


def _approve(
    draft: dict[str, Any],
    *,
    state: dict[str, dict[str, Any]] | None = None,
    decision: str = "APPROVE",
    approved_at: datetime = NOW + timedelta(minutes=1),
    policy_version: str = POLICY_VERSION,
) -> dict[str, Any]:
    package = approve_daily_card(
        draft,
        reviewer_id="owner-1",
        decisions={"event-1": {"decision": decision, "reason": "owner review"}},
        batch_review={
            "decision": "SIGN_OFF",
            "reason": "daily card reviewed",
            "zero_candidate_day_acknowledged": bool(draft["zero_candidate_day"]),
        },
        current_event_states=state if state is not None else _current_state(draft),
        current_policy_version=policy_version,
        approved_at=approved_at,
    )
    return package.as_dict()


def test_draft_is_review_required_and_automated_candidate_is_not_publication() -> None:
    draft = _draft()

    assert draft["review_status"] == "REVIEW_REQUIRED"
    assert draft["automatic_publication"] is False
    assert draft["candidate_count"] == 1
    assert "publication_checksum" not in draft


def test_draft_rejects_prediction_sealed_against_different_features() -> None:
    inputs = _inputs()
    inputs["sealed_predictions"][0]["feature_checksum"] = "superseded-feature-checksum"

    with pytest.raises(ValueError, match="feature_checksum"):
        create_daily_card_draft(**inputs)


def test_draft_accepts_release_candidate_domain_dataclasses() -> None:
    from app.analysis.canonical import CanonicalGame, MlbFeatures
    from app.analysis.models import CandidateDecision, DataQualityState, GateResult, UncertaintyGrade
    from app.analysis.policy import MoneylineSideEvaluation, PolicyEvaluation
    from app.analysis.predictions import (
        AnalystEvidenceBundle,
        EvidenceAssessment,
        EvidenceState,
        LineupInformationState,
        SealedPrediction,
        SourceReference,
    )

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
    game = CanonicalGame(
        contract_version="canonical-game-v1",
        run_id="run-1",
        event_id="event-1",
        requested_date=NOW.date(),
        commence_time=FIRST_PITCH,
        raw_home_team="Los Angeles Dodgers",
        raw_away_team="San Francisco Giants",
        home_team_key="LAD",
        away_team_key="SFG",
        venue_context={},
        roof_context="outdoor",
        odds_consensus={},
        weather_context={},
        weather_gate_clear=True,
        quality_state=DataQualityState.READY,
        quality_issues=(),
        source_checksums={"odds": "odds-source"},
        assembled_at=NOW,
        checksum="canonical-checksum",
    )
    features = MlbFeatures(
        contract_version="mlb-features-v1",
        event_id="event-1",
        canonical_game_checksum="canonical-checksum",
        schedule_context={},
        team_context={},
        venue_context={},
        weather_context={},
        market_context={},
        data_quality_state=DataQualityState.READY,
        uncertainty_flags=(),
        generated_at=NOW,
        checksum="f" * 64,
    )
    prediction = SealedPrediction(
        contract_version="DSE_REVIEWED_ANALYST_V1",
        prediction_id="prediction-1",
        run_id="run_20260715_0123456789abcdef0123456789abcdef",
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
        feature_checksum="f" * 64,
        market_independence_attested=True,
        evidence=evidence,
        evidence_checksum="evidence-checksum",
        checksum="prediction-checksum",
    )
    side = MoneylineSideEvaluation("LAD", 0.58, 0.52, 0.63, 0.53, -110, ("book-a",), 0.5238, 5.0, 0.107)
    evaluation = PolicyEvaluation(
        policy_version=POLICY_VERSION,
        evaluation_id="evaluation-1",
        event_id="event-1",
        prediction_id="prediction-1",
        prediction_checksum="prediction-checksum",
        canonical_game_checksum="canonical-checksum",
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
        checksum="evaluation-checksum",
    )

    draft = create_daily_card_draft(
        run_id="run-1",
        requested_date=NOW.date(),
        policy_version=POLICY_VERSION,
        canonical_games=[game],
        features=[features],
        sealed_predictions=[prediction],
        policy_evaluations=[evaluation],
        generated_at=NOW,
    )

    assert draft["items"][0]["prediction"]["lower_bound"] == 0.52
    assert draft["items"][0]["prediction"]["upper_bound"] == 0.63
    assert draft["items"][0]["analyst_evidence"]["method_version"] == "DSE_REVIEWED_ANALYST_V1"
    assert draft["items"][0]["market_evaluation"]["best_price_books"] == ["book-a"]


def test_approval_creates_checksum_backed_local_package() -> None:
    draft = _draft()
    package = approve_daily_card(
        draft,
        reviewer_id="owner-1",
        decisions={"event-1": {"decision": "APPROVE", "reason": "owner review"}},
        batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
        current_event_states=_current_state(draft),
        current_policy_version=POLICY_VERSION,
        approved_at=NOW + timedelta(minutes=1),
    )
    payload = package.as_dict()

    assert payload["published_play_count"] == 1
    assert payload["automatic_publication"] is False
    assert payload["public_release_performed"] is False
    assert payload["publication_checksum"] == package.checksum
    assert payload["published_plays"][0]["publication_price"] == -110
    assert payload["published_plays"][0]["bookmaker_key"] == "book-a"
    assert payload["batch_review"]["decision"] == "signed_off"

    detached = package.as_dict()
    detached["published_plays"].clear()
    assert package.as_dict()["published_play_count"] == 1


@pytest.mark.parametrize(
    ("state_change", "code"),
    [
        ({"event_status": "in_progress"}, ApprovalFailureCode.EVENT_NOT_PREGAME),
        ({"best_price_fresh": False}, ApprovalFailureCode.ODDS_NOT_FRESH),
        ({"best_price": 105}, ApprovalFailureCode.MARKET_PRICE_CHANGED),
        ({"best_price_books": ["book-b"]}, ApprovalFailureCode.MARKET_PRICE_CHANGED),
        ({"prediction_checksum": "changed"}, ApprovalFailureCode.PREDICTION_CHECKSUM_CHANGED),
        ({"evidence_checksum": "changed"}, ApprovalFailureCode.EVIDENCE_CHECKSUM_CHANGED),
        ({"policy_evaluation_checksum": "changed"}, ApprovalFailureCode.EVALUATION_CHECKSUM_CHANGED),
        ({"required_source_checksums": {"canonical_game": "changed"}}, ApprovalFailureCode.SOURCE_EVIDENCE_CHANGED),
        ({"policy_version": "DSE_MLB_ML_CANDIDATE_V2"}, ApprovalFailureCode.POLICY_VERSION_CHANGED),
    ],
)
def test_approval_fails_closed_when_current_state_changed(
    state_change: dict[str, Any], code: ApprovalFailureCode
) -> None:
    draft = _draft()
    state = _current_state(draft)
    state["event-1"].update(state_change)

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, state=state)

    assert captured.value.code is code


@pytest.mark.parametrize(
    "field",
    [
        "scheduled_first_pitch_utc",
        "event_status",
        "best_price_fresh",
        "best_price",
        "best_price_books",
        "policy_version",
        "prediction_checksum",
        "evidence_checksum",
        "policy_evaluation_checksum",
        "required_source_checksums",
    ],
)
def test_approval_requires_every_explicit_current_state_field(field: str) -> None:
    draft = _draft()
    state = _current_state(draft)
    state["event-1"].pop(field)

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, state=state)

    assert captured.value.code is ApprovalFailureCode.MISSING_CURRENT_EVENT_STATE


def test_approval_fails_at_or_after_first_pitch() -> None:
    draft = _draft()

    for approved_at in (FIRST_PITCH, FIRST_PITCH + timedelta(seconds=1)):
        with pytest.raises(ApprovalFailure) as captured:
            _approve(draft, approved_at=approved_at)
        assert captured.value.code is ApprovalFailureCode.FIRST_PITCH_REACHED


def test_changed_scheduled_first_pitch_requires_draft_regeneration() -> None:
    draft = _draft()
    state = _current_state(draft)
    state["event-1"]["scheduled_first_pitch_utc"] = (FIRST_PITCH + timedelta(minutes=30)).isoformat()

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, state=state)

    assert captured.value.code is ApprovalFailureCode.SOURCE_EVIDENCE_CHANGED


def test_approval_fails_if_draft_checksum_changed() -> None:
    draft = _draft()
    draft["items"][0]["market_evaluation"]["best_price"] = 120

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft)

    assert captured.value.code is ApprovalFailureCode.DRAFT_CHECKSUM_CHANGED


def test_approval_fails_if_active_policy_version_changed() -> None:
    draft = _draft()

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, policy_version="DSE_MLB_ML_CANDIDATE_V2")

    assert captured.value.code is ApprovalFailureCode.POLICY_VERSION_CHANGED


def test_reviewer_may_convert_candidate_to_pass() -> None:
    payload = _approve(_draft(), decision="PASS")

    assert payload["published_play_count"] == 0
    assert payload["zero_published_play_day"] is True
    assert payload["review_decisions"][0]["decision"] == "rejected_to_pass"


def test_automated_pass_cannot_be_promoted() -> None:
    draft = _draft(status="PASS")

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, decision="APPROVE")

    assert captured.value.code is ApprovalFailureCode.AUTOMATED_PASS_CANNOT_BE_PROMOTED


def test_zero_candidate_day_is_valid_without_manufacturing_a_play() -> None:
    draft = _draft(status="PASS")
    package = approve_daily_card(
        draft,
        reviewer_id="owner-1",
        decisions={},
        batch_review={
            "decision": "SIGN_OFF",
            "reason": "reviewed zero-candidate day",
            "zero_candidate_day_acknowledged": True,
        },
        current_event_states={},
        current_policy_version=POLICY_VERSION,
        approved_at=NOW,
    ).as_dict()

    assert draft["zero_candidate_day"] is True
    assert package["zero_published_play_day"] is True
    assert package["published_plays"] == []
    assert package["batch_review"]["zero_candidate_day_acknowledged"] is True


def test_candidate_with_re_signed_failed_gate_still_cannot_be_approved() -> None:
    draft = _draft()
    draft["items"][0]["gate_results"][0]["passed"] = False
    unsigned = {key: value for key, value in draft.items() if key != "draft_checksum"}
    draft["draft_checksum"] = payload_checksum(unsigned)
    state = _current_state(draft)

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, state=state)

    assert captured.value.code is ApprovalFailureCode.FAILED_AUTOMATED_GATE


def test_candidate_cannot_be_approved_when_a_current_gate_is_revoked() -> None:
    draft = _draft()
    state = _current_state(draft)
    current = state["event-1"]["current_policy_evaluation"]
    current["decision"] = "PASS"
    phase2_gate = next(
        gate
        for gate in current["gate_results"]
        if gate["code"] == "phase2_live_weather_accepted"
    )
    phase2_gate["passed"] = False
    phase2_gate["reason"] = "Phase 2 live weather validation release gate remains pending"

    with pytest.raises(ApprovalFailure) as captured:
        _approve(draft, state=state)

    assert captured.value.code is ApprovalFailureCode.FAILED_AUTOMATED_GATE


def test_candidate_requires_explicit_review_decision() -> None:
    draft = _draft()

    with pytest.raises(ApprovalFailure) as captured:
        approve_daily_card(
            draft,
            reviewer_id="owner-1",
            decisions={},
            batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
            current_event_states=_current_state(draft),
            current_policy_version=POLICY_VERSION,
            approved_at=NOW,
        )

    assert captured.value.code is ApprovalFailureCode.MISSING_REVIEW_DECISION


def test_reviewer_may_choose_only_a_book_at_the_evaluated_best_price() -> None:
    draft = _draft()

    with pytest.raises(ApprovalFailure) as captured:
        approve_daily_card(
            draft,
            reviewer_id="owner-1",
            decisions={
                "event-1": {
                    "decision": "APPROVE",
                    "reason": "owner review",
                    "bookmaker_key": "not-best",
                }
            },
            batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
            current_event_states=_current_state(draft),
            current_policy_version=POLICY_VERSION,
            approved_at=NOW,
        )

    assert captured.value.code is ApprovalFailureCode.INVALID_REVIEW_DECISION


def test_draft_rejects_an_incomplete_or_duplicate_gate_set() -> None:
    inputs = _inputs()
    inputs["policy_evaluations"][0]["gate_results"].pop()

    with pytest.raises(ValueError, match="exact required candidate gate set"):
        create_daily_card_draft(**inputs)

    inputs = _inputs()
    inputs["policy_evaluations"][0]["gate_results"][-1]["code"] = (
        REQUIRED_CANDIDATE_GATE_CODES[0]
    )
    with pytest.raises(ValueError, match="exact required candidate gate set"):
        create_daily_card_draft(**inputs)


def test_draft_rejects_gate_without_a_machine_auditable_reason() -> None:
    inputs = _inputs()
    inputs["policy_evaluations"][0]["gate_results"][0]["reason"] = "   "

    with pytest.raises(ValueError, match="nonempty reason"):
        create_daily_card_draft(**inputs)


@pytest.mark.parametrize(
    "decision",
    ["APPROVE", {"decision": "APPROVE"}, {"decision": "APPROVE", "reason": "   "}],
)
def test_candidate_review_decision_requires_mapping_and_nonempty_reason(
    decision: object,
) -> None:
    draft = _draft()

    with pytest.raises(ValueError, match="nonempty reason"):
        approve_daily_card(
            draft,
            reviewer_id="owner-1",
            decisions={"event-1": decision},
            batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
            current_event_states=_current_state(draft),
            current_policy_version=POLICY_VERSION,
            approved_at=NOW,
        )


def test_zero_candidate_day_requires_explicit_batch_acknowledgement() -> None:
    draft = _draft(status="PASS")

    with pytest.raises(ValueError, match="zero_candidate_day_acknowledged"):
        approve_daily_card(
            draft,
            reviewer_id="owner-1",
            decisions={},
            batch_review={"decision": "SIGN_OFF", "reason": "daily card reviewed"},
            current_event_states={},
            current_policy_version=POLICY_VERSION,
            approved_at=NOW,
        )


@pytest.mark.parametrize(
    "batch_review",
    [
        {},
        {"decision": "REVIEW_COMPLETE", "reason": "daily card reviewed"},
        {"decision": "SIGN_OFF"},
        {"decision": "SIGN_OFF", "reason": "   "},
    ],
)
def test_batch_review_requires_explicit_reasoned_signoff(
    batch_review: dict[str, object],
) -> None:
    draft = _draft()

    with pytest.raises(ValueError, match="batch_review"):
        approve_daily_card(
            draft,
            reviewer_id="owner-1",
            decisions={"event-1": {"decision": "PASS", "reason": "owner review"}},
            batch_review=batch_review,
            current_event_states=_current_state(draft),
            current_policy_version=POLICY_VERSION,
            approved_at=NOW,
        )
