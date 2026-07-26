from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from app.analysis import (
    CANDIDATE_POLICY_VERSION,
    REQUIRED_CANDIDATE_GATE_CODES,
    REVIEWED_ANALYST_VERSION,
    AnalystEvidenceBundle,
    CandidateDecision,
    EvidenceAssessment,
    EvidenceState,
    GateResult,
    LineupInformationState,
    PolicyEvaluation,
    ReviewedPredictionInput,
    SourceReference,
    UncertaintyGrade,
    assemble_canonical_game,
    evaluate_moneyline_candidate,
    seal_prediction,
    verify_prediction_checksum,
)
from app.analysis.policy import american_to_probability

NOW = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc)
COMMENCE = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)
EFFECTIVE = NOW - timedelta(seconds=60)
RETRIEVED = NOW - timedelta(seconds=50)
RUN_ID = "run_20260715_0123456789abcdef0123456789abcdef"
FEATURE_CHECKSUM = "a" * 64


def _source() -> SourceReference:
    return SourceReference(
        source_id="analyst-source-1",
        source_name="Reviewed analyst notes",
        reference="internal-review-2026-07-15",
        observed_at=NOW - timedelta(minutes=20),
    )


def _assessment(
    state: EvidenceState = EvidenceState.AVAILABLE,
) -> EvidenceAssessment:
    return EvidenceAssessment(
        state=state,
        assessment="Reviewed and available" if state is EvidenceState.AVAILABLE else None,
        source_ids=("analyst-source-1",) if state is EvidenceState.AVAILABLE else (),
    )


def evidence(*, complete: bool = True) -> AnalystEvidenceBundle:
    state = EvidenceState.AVAILABLE if complete else EvidenceState.UNKNOWN
    return AnalystEvidenceBundle(
        analyst_identity="owner-analyst",
        method_version=REVIEWED_ANALYST_VERSION,
        starting_pitching=_assessment(state),
        bullpen=_assessment(state),
        offensive_matchup=_assessment(state),
        lineup_information_state=(
            LineupInformationState.CONFIRMED if complete else LineupInformationState.UNKNOWN
        ),
        lineup_notes="Confirmed lineup reviewed" if complete else None,
        lineup_source_ids=("analyst-source-1",) if complete else (),
        venue_context=_assessment(state),
        weather_context=_assessment(state),
        schedule_rest_context=_assessment(state),
        material_unknowns=() if complete else ("starting pitcher unavailable",),
        source_provenance=(_source(),),
    )


def prediction_input(
    *,
    probability: float = 0.55,
    lower: float = 0.49,
    upper: float = 0.61,
    complete_evidence: bool = True,
) -> ReviewedPredictionInput:
    return ReviewedPredictionInput(
        run_id=RUN_ID,
        event_id="event-rc-policy",
        home_team_key="ARI",
        away_team_key="LAD",
        home_probability=probability,
        home_probability_lower=lower,
        home_probability_upper=upper,
        generated_at=NOW - timedelta(minutes=10),
        feature_checksum=FEATURE_CHECKSUM,
        market_independence_attested=True,
        evidence=evidence(complete=complete_evidence),
    )


def _offer(
    bookmaker: str,
    price: int,
    *,
    order: int,
    effective: datetime = EFFECTIVE,
    retrieved: datetime = RETRIEVED,
) -> dict[str, object]:
    return {
        "bookmaker_key": bookmaker,
        "price": price,
        "effective_provider_timestamp": effective.isoformat(),
        "provider_retrieved_at": retrieved.isoformat(),
        "provider_order": order,
        "calculation_eligible": True,
    }


def _odds(
    *,
    prices: tuple[tuple[str, int, int], ...] = (
        ("book-a", 120, -130),
        ("book-b", 125, -135),
        ("book-c", 118, -128),
        ("book-d", 122, -132),
    ),
) -> dict[str, Any]:
    home = [_offer(book, home_price, order=index * 2 + 1) for index, (book, home_price, _) in enumerate(prices)]
    away = [_offer(book, away_price, order=index * 2 + 2) for index, (book, _, away_price) in enumerate(prices)]
    return {
        "contract_version": "odds-consensus-v2",
        "event_id": "event-rc-policy",
        "commence_time": COMMENCE.isoformat(),
        "raw_home_team": "Arizona Diamondbacks",
        "raw_away_team": "Los Angeles Dodgers",
        "home_team_key": "ARI",
        "away_team_key": "LAD",
        "markets": {
            "h2h": {
                "lines": {
                    "moneyline": {
                        "complete_two_way_market": True,
                        "outcomes": {
                            "ARI": {"offers": home},
                            "LAD": {"offers": away},
                        },
                    }
                }
            }
        },
    }


def canonical_game(*, odds: dict[str, Any] | None = None):
    verified = {
        "status": "VERIFIED",
        "source_ids": ["deterministic_fixture"],
        "verified_at": "2026-07-12",
        "notes": "Deterministic runtime fixture.",
        "conflicts": [],
    }
    return assemble_canonical_game(
        run_id=RUN_ID,
        requested_date=date(2026, 7, 15),
        odds_summary=odds or _odds(),
        weather_packet={
            "nws": {"forecast_time": COMMENCE.isoformat(), "temperature_f": 82.0},
        },
        venue={
            "physical_venue_key": "fixture-park",
            "venue": "Fixture Park",
            "team_key": "ARI",
            "active_club_association": True,
            "timezone": "America/Los_Angeles",
            "roof_type": "open",
            "latitude": 33.4453,
            "longitude": -112.0667,
            "outfield_bearing_degrees": None,
            "field_verification": {
                field: dict(verified)
                for field in (
                    "physical_venue_key",
                    "active_club_association",
                    "latitude",
                    "longitude",
                    "timezone",
                    "roof_type",
                )
            },
        },
        assembled_at=NOW,
    )


def _gate_map(evaluation: PolicyEvaluation) -> dict[str, GateResult]:
    return {gate.code: gate for gate in evaluation.gate_results}


def test_authoring_view_contains_no_market_derived_values() -> None:
    view = prediction_input().analyst_input_view()
    keys: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            keys.update(str(key) for key in value)
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(view)
    assert {
        "market_no_vig_probability",
        "edge_percentage_points",
        "expected_value_per_unit_risk",
        "candidate_status",
        "decision",
    }.isdisjoint(keys)
    assert "no_vig" not in json.dumps(view)


def test_seal_requires_minimum_manual_interval_width() -> None:
    with pytest.raises(ValueError, match="at least 0.10 wide"):
        seal_prediction(
            prediction_input(probability=0.55, lower=0.51, upper=0.60),
            sealed_at=NOW,
        )


def test_seal_validates_canonical_run_identity() -> None:
    with pytest.raises(ValueError, match="run_id must match"):
        seal_prediction(replace(prediction_input(), run_id="run-policy-fixture"), sealed_at=NOW)


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63])
def test_seal_validates_feature_checksum(value: str) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        seal_prediction(replace(prediction_input(), feature_checksum=value), sealed_at=NOW)


def test_structured_available_evidence_requires_provenance() -> None:
    broken = replace(evidence(), source_provenance=())
    with pytest.raises(ValueError, match="source_provenance"):
        seal_prediction(replace(prediction_input(), evidence=broken), sealed_at=NOW)


def test_unknown_evidence_is_preserved_without_fabricated_details() -> None:
    sealed = seal_prediction(
        prediction_input(complete_evidence=False),
        sealed_at=NOW,
    )

    assert sealed.evidence.starting_pitching.state is EvidenceState.UNKNOWN
    assert sealed.evidence.starting_pitching.assessment is None
    assert sealed.evidence.material_unknowns == ("starting pitcher unavailable",)


def test_prediction_checksum_is_stable_and_detects_change() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    repeated = seal_prediction(prediction_input(), sealed_at=NOW + timedelta(seconds=1))

    assert sealed.checksum == repeated.checksum
    assert sealed.evidence_checksum == repeated.evidence_checksum
    assert sealed.prediction_id == repeated.prediction_id
    assert sealed.as_dict()["evidence_checksum"] == sealed.evidence_checksum
    assert sealed.as_dict()["lower_bound"] == sealed.home_probability_lower
    assert sealed.as_dict()["run_id"] == RUN_ID
    assert sealed.as_dict()["feature_checksum"] == FEATURE_CHECKSUM
    assert verify_prediction_checksum(sealed)
    assert not verify_prediction_checksum(replace(sealed, home_probability=0.90))
    assert not verify_prediction_checksum(replace(sealed, evidence_checksum="0" * 64))


def test_evidence_checksum_is_scoped_to_run_and_event_identity() -> None:
    baseline = seal_prediction(prediction_input(), sealed_at=NOW)
    other_event = seal_prediction(
        replace(prediction_input(), event_id="event-rc-policy-other"),
        sealed_at=NOW,
    )
    other_run = seal_prediction(
        replace(
            prediction_input(),
            run_id="run_20260716_abcdef0123456789abcdef0123456789",
        ),
        sealed_at=NOW,
    )

    assert len(
        {
            baseline.evidence_checksum,
            other_event.evidence_checksum,
            other_run.evidence_checksum,
        }
    ) == 3


def test_policy_candidate_records_all_gate_results() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    result = evaluate_moneyline_candidate(
        canonical_game(),
        sealed,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert result.policy_version == CANDIDATE_POLICY_VERSION
    assert result.decision is CandidateDecision.CANDIDATE_REQUIRES_REVIEW
    assert result.selected_team_key == "ARI"
    assert result.eligible_bookmaker_count == 4
    assert result.best_price == 125
    assert result.best_price_books == ("book-b",)
    assert result.confidence_grade is UncertaintyGrade.B
    assert all(gate.passed and gate.reason for gate in result.gate_results)
    assert tuple(gate.code for gate in result.gate_results) == REQUIRED_CANDIDATE_GATE_CODES


def test_market_baseline_is_median_of_same_book_no_vig_pairs() -> None:
    prices = (
        ("book-a", 120, -130),
        ("book-b", 125, -135),
        ("book-c", 118, -128),
        ("book-d", 122, -132),
    )
    result = evaluate_moneyline_candidate(
        canonical_game(odds=_odds(prices=prices)),
        seal_prediction(prediction_input(), sealed_at=NOW),
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )
    expected_values = []
    for _, home_price, away_price in prices:
        home_raw = american_to_probability(home_price)
        away_raw = american_to_probability(away_price)
        assert home_raw is not None and away_raw is not None
        expected_values.append(home_raw / (home_raw + away_raw))

    assert result.market_no_vig_probability == pytest.approx(
        (sorted(expected_values)[1] + sorted(expected_values)[2]) / 2
    )


def test_cross_book_and_incompatible_capture_prices_are_not_paired() -> None:
    odds = _odds()
    outcomes = odds["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    outcomes["ARI"]["offers"].append(_offer("home-only", 500, order=20))
    outcomes["LAD"]["offers"].append(_offer("away-only", 500, order=21))
    outcomes["ARI"]["offers"].append(_offer("skewed", 400, order=22))
    outcomes["LAD"]["offers"].append(
        _offer("skewed", 400, order=23, retrieved=RETRIEVED - timedelta(seconds=6))
    )

    result = evaluate_moneyline_candidate(
        canonical_game(odds=odds),
        seal_prediction(prediction_input(), sealed_at=NOW),
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert result.eligible_bookmaker_count == 4
    assert "home-only" not in result.eligible_bookmakers
    assert "away-only" not in result.eligible_bookmakers
    assert "skewed" not in result.eligible_bookmakers
    assert result.best_price == 125


def test_stale_offer_is_filtered_before_book_count_and_best_price() -> None:
    odds = _odds()
    outcomes = odds["markets"]["h2h"]["lines"]["moneyline"]["outcomes"]
    stale = NOW - timedelta(seconds=121)
    for side in ("ARI", "LAD"):
        for offer in outcomes[side]["offers"]:
            if offer["bookmaker_key"] == "book-d":
                offer["effective_provider_timestamp"] = stale.isoformat()

    result = evaluate_moneyline_candidate(
        canonical_game(odds=odds),
        seal_prediction(prediction_input(), sealed_at=NOW),
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )
    gates = _gate_map(result)

    assert result.eligible_bookmaker_count == 3
    assert result.decision is CandidateDecision.PASS
    assert gates["minimum_book_count"].passed is False
    assert gates["minimum_book_count"].observed_value == 3


def test_tied_fresh_best_prices_preserve_all_books() -> None:
    odds = _odds(prices=(("book-a", 125, -135), ("book-b", 125, -135), ("book-c", 118, -128), ("book-d", 122, -132)))
    result = evaluate_moneyline_candidate(
        canonical_game(odds=odds),
        seal_prediction(prediction_input(), sealed_at=NOW),
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert result.best_price == 125
    assert result.best_price_books == ("book-a", "book-b")


def test_negative_best_price_selection_for_away_side() -> None:
    sealed = seal_prediction(
        prediction_input(probability=0.30, lower=0.24, upper=0.36),
        sealed_at=NOW,
    )
    result = evaluate_moneyline_candidate(
        canonical_game(),
        sealed,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert result.selected_team_key == "LAD"
    assert result.best_price == -128
    assert result.best_price_books == ("book-c",)


def test_low_edge_is_a_valid_zero_candidate_pass_result() -> None:
    sealed = seal_prediction(
        prediction_input(probability=0.45, lower=0.40, upper=0.50),
        sealed_at=NOW,
    )
    result = evaluate_moneyline_candidate(
        canonical_game(),
        sealed,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )
    gates = _gate_map(result)

    assert result.decision is CandidateDecision.PASS
    assert gates["minimum_edge"].passed is False
    assert gates["minimum_edge"].reason


def test_material_unknowns_cap_confidence_below_launch_minimum() -> None:
    sealed = seal_prediction(
        prediction_input(complete_evidence=False),
        sealed_at=NOW,
    )
    result = evaluate_moneyline_candidate(
        canonical_game(),
        sealed,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert result.confidence_grade is UncertaintyGrade.C
    assert _gate_map(result)["minimum_confidence"].passed is False
    assert result.decision is CandidateDecision.PASS


def test_evaluation_at_first_pitch_fails_closed() -> None:
    result = evaluate_moneyline_candidate(
        canonical_game(),
        seal_prediction(prediction_input(), sealed_at=NOW),
        evaluated_at=COMMENCE,
        phase2_live_weather_accepted=True,
    )

    assert result.decision is CandidateDecision.PASS
    assert _gate_map(result)["event_pregame"].passed is False


def test_tampered_prediction_fails_checksum_gate() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    tampered = replace(sealed, home_probability=0.75, away_probability=0.25)
    result = evaluate_moneyline_candidate(
        canonical_game(),
        tampered,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert _gate_map(result)["prediction_valid"].passed is False
    assert result.market_no_vig_probability is None
    assert result.best_price is None
    assert result.edge_percentage_points is None
    assert result.expected_value_per_unit_risk is None
    assert result.decision is CandidateDecision.PASS


def test_phase2_live_weather_gate_defaults_closed() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    result = evaluate_moneyline_candidate(canonical_game(), sealed, evaluated_at=NOW)

    gate = _gate_map(result)["phase2_live_weather_accepted"]
    assert gate.passed is False
    assert result.decision is CandidateDecision.PASS


def test_tampered_derived_away_values_fail_before_market_exposure() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    tampered = replace(
        sealed,
        away_probability=0.90,
        away_probability_lower=0.80,
        away_probability_upper=1.0,
    )
    result = evaluate_moneyline_candidate(
        canonical_game(),
        tampered,
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert _gate_map(result)["prediction_valid"].passed is False
    assert result.market_no_vig_probability is None
    assert result.edge_percentage_points is None


def test_prediction_id_is_verified_against_sealed_checksum() -> None:
    sealed = seal_prediction(prediction_input(), sealed_at=NOW)
    result = evaluate_moneyline_candidate(
        canonical_game(),
        replace(sealed, prediction_id="pred_not_the_checksum"),
        evaluated_at=NOW,
        phase2_live_weather_accepted=True,
    )

    assert _gate_map(result)["prediction_valid"].passed is False
    assert result.market_no_vig_probability is None
