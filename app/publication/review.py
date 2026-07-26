from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import json
from typing import Any, Mapping, Sequence
from uuid import uuid4

from app.analysis.policy import REQUIRED_CANDIDATE_GATE_CODES
from app.identifiers import parse_requested_date
from app.publication.checksums import canonical_json_bytes, normalize_payload, payload_checksum


DAILY_CARD_CONTRACT_VERSION = "daily-card-v1"
PUBLICATION_PACKAGE_CONTRACT_VERSION = "publication-package-v1"
CANDIDATE_STATUS = "CANDIDATE_REQUIRES_REVIEW"
PASS_STATUS = "PASS"
_REQUIRED_EVIDENCE_FIELDS = {
    "analyst_identity",
    "method_version",
    "starting_pitching",
    "bullpen",
    "offensive_matchup",
    "lineup_information_state",
    "lineup_notes",
    "lineup_source_ids",
    "venue_context",
    "weather_context",
    "schedule_rest_context",
    "material_unknowns",
    "source_provenance",
}


class ApprovalFailureCode(StrEnum):
    DRAFT_CHECKSUM_CHANGED = "draft_checksum_changed"
    POLICY_VERSION_CHANGED = "policy_version_changed"
    MISSING_CURRENT_EVENT_STATE = "missing_current_event_state"
    EVENT_NOT_PREGAME = "event_not_pregame"
    FIRST_PITCH_REACHED = "first_pitch_reached"
    ODDS_NOT_FRESH = "odds_not_fresh"
    MARKET_PRICE_CHANGED = "market_price_changed"
    PREDICTION_CHECKSUM_CHANGED = "prediction_checksum_changed"
    EVIDENCE_CHECKSUM_CHANGED = "evidence_checksum_changed"
    EVALUATION_CHECKSUM_CHANGED = "evaluation_checksum_changed"
    SOURCE_EVIDENCE_CHANGED = "source_evidence_changed"
    FAILED_AUTOMATED_GATE = "failed_automated_gate"
    MISSING_REVIEW_DECISION = "missing_review_decision"
    INVALID_REVIEW_DECISION = "invalid_review_decision"
    AUTOMATED_PASS_CANNOT_BE_PROMOTED = "automated_pass_cannot_be_promoted"


class ApprovalFailure(ValueError):
    def __init__(self, code: ApprovalFailureCode, message: str, *, event_id: str | None = None) -> None:
        self.code = code
        self.event_id = event_id
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ImmutablePublicationPackage:
    """Checksum-backed immutable JSON bytes for a completed local review."""

    _canonical_bytes: bytes
    checksum: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ImmutablePublicationPackage:
        normalized = normalize_payload(payload)
        checksum = payload_checksum(normalized)
        with_checksum = {**normalized, "publication_checksum": checksum}
        return cls(canonical_json_bytes(with_checksum), checksum)

    def as_dict(self) -> dict[str, Any]:
        parsed = json.loads(self._canonical_bytes)
        if not isinstance(parsed, dict):
            raise RuntimeError("publication package is not a JSON object")
        return parsed

    def to_json_bytes(self, *, pretty: bool = False) -> bytes:
        if not pretty:
            return self._canonical_bytes
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    normalized = normalize_payload(value)
    if not isinstance(normalized, dict):
        raise TypeError(f"{label} must be a mapping or expose as_dict()")
    return normalized


def _aware_datetime(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO timestamp") from exc
    else:
        raise TypeError(f"{label} must be a datetime or ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _canonical_date(value: date | str) -> str:
    if isinstance(value, datetime):
        raise TypeError("requested_date must be a date or canonical date string")
    if isinstance(value, date):
        return value.isoformat()
    return parse_requested_date(value).isoformat()


def _index(records: Sequence[Any], key: str, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        item = _mapping(record, label)
        identity = item.get(key)
        if not isinstance(identity, str) or not identity:
            raise ValueError(f"{label} requires non-empty {key}")
        if identity in result:
            raise ValueError(f"duplicate {label} {key}: {identity}")
        result[identity] = item
    return result


def _status(evaluation: Mapping[str, Any]) -> str:
    raw = evaluation.get("decision", evaluation.get("outcome"))
    status = str(raw)
    if status not in {PASS_STATUS, CANDIDATE_STATUS}:
        raise ValueError("policy evaluation outcome must be PASS or CANDIDATE_REQUIRES_REVIEW")
    return status


def _gates(evaluation: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = evaluation.get("gate_results", evaluation.get("gates", []))
    if not isinstance(raw, list):
        raise ValueError("policy evaluation gate_results must be a list")
    gates = [_mapping(gate, "gate result") for gate in raw]
    codes: list[str] = []
    for gate in gates:
        if not isinstance(gate.get("passed"), bool):
            raise ValueError("each gate result must contain a boolean passed value")
        code = gate.get("code", gate.get("gate_code"))
        if not isinstance(code, str) or not code:
            raise ValueError("each gate result must contain a nonempty code")
        reason = gate.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("each gate result must contain a nonempty reason")
        gate["code"] = code
        gate["reason"] = reason.strip()
        codes.append(code)
    if (
        len(codes) != len(REQUIRED_CANDIDATE_GATE_CODES)
        or set(codes) != set(REQUIRED_CANDIDATE_GATE_CODES)
    ):
        raise ValueError("policy evaluation does not contain the exact required candidate gate set")
    return gates


def _record_checksum(record: Mapping[str, Any]) -> str:
    checksum = record.get("checksum")
    if isinstance(checksum, str) and checksum:
        return checksum
    return payload_checksum(record)


def create_daily_card_draft(
    *,
    run_id: str,
    requested_date: date | str,
    policy_version: str,
    canonical_games: Sequence[Any],
    features: Sequence[Any],
    sealed_predictions: Sequence[Any],
    policy_evaluations: Sequence[Any],
    generated_at: datetime | str,
) -> dict[str, Any]:
    """Build a review-required draft; this function never authorizes publication."""
    if not run_id or not policy_version:
        raise ValueError("run_id and policy_version are required")
    generated = _aware_datetime(generated_at, "generated_at")
    games = _index(canonical_games, "event_id", "canonical game")
    features_by_event = _index(features, "event_id", "feature record")
    predictions = _index(sealed_predictions, "prediction_id", "sealed prediction")

    items: list[dict[str, Any]] = []
    for raw_evaluation in policy_evaluations:
        evaluation = _mapping(raw_evaluation, "policy evaluation")
        event_id = evaluation.get("event_id")
        prediction_id = evaluation.get("prediction_id")
        if not isinstance(event_id, str) or event_id not in games:
            raise ValueError("policy evaluation references an unknown event_id")
        if not isinstance(prediction_id, str) or prediction_id not in predictions:
            raise ValueError("policy evaluation references an unknown prediction_id")
        if event_id not in features_by_event:
            raise ValueError("policy evaluation event has no feature record")
        game = games[event_id]
        feature = features_by_event[event_id]
        prediction = predictions[prediction_id]
        if prediction.get("event_id") != event_id:
            raise ValueError("sealed prediction event_id does not match policy evaluation")
        if evaluation.get("policy_version") != policy_version:
            raise ValueError("policy evaluation policy_version does not match the draft")

        prediction_checksum = _record_checksum(prediction)
        feature_checksum = _record_checksum(feature)
        if prediction.get("feature_checksum") != feature_checksum:
            raise ValueError("sealed prediction feature_checksum does not match current feature record")
        expected_prediction_checksum = evaluation.get("prediction_checksum")
        if expected_prediction_checksum is not None and expected_prediction_checksum != prediction_checksum:
            raise ValueError("policy evaluation prediction_checksum does not match sealed prediction")
        evidence = prediction.get("evidence", prediction.get("evidence_bundle", {}))
        evidence = _mapping(evidence, "analyst evidence")
        missing_evidence = sorted(_REQUIRED_EVIDENCE_FIELDS - set(evidence))
        if missing_evidence:
            raise ValueError(
                "sealed prediction analyst evidence is missing: "
                + ", ".join(missing_evidence)
            )
        evidence_checksum = prediction.get("evidence_checksum")
        if not isinstance(evidence_checksum, str) or not evidence_checksum:
            evidence_checksum = payload_checksum(evidence)
        source_checksums = _mapping(game.get("source_checksums", {}), "source checksums")
        required_source_checksums = {
            **{f"canonical_source:{key}": value for key, value in source_checksums.items()},
            "canonical_game": _record_checksum(game),
            "features": feature_checksum,
        }
        evaluation_checksum = _record_checksum(evaluation)
        gates = _gates(evaluation)
        status = _status(evaluation)
        all_gates_passed = bool(gates) and all(gate["passed"] is True for gate in gates)
        if status == CANDIDATE_STATUS and not all_gates_passed:
            raise ValueError("candidate policy evaluation contains a failed automated gate")

        scheduled = game.get("commence_time", game.get("scheduled_first_pitch_utc"))
        scheduled_at = _aware_datetime(scheduled, "scheduled first pitch")
        items.append(
            {
                "event_id": event_id,
                "prediction_id": prediction_id,
                "policy_evaluation_id": evaluation.get("evaluation_id", evaluation.get("policy_evaluation_id")),
                "scheduled_first_pitch_utc": scheduled_at.isoformat(),
                "home_team_key": game.get("home_team_key"),
                "away_team_key": game.get("away_team_key"),
                "selected_team_key": evaluation.get("selected_team_key"),
                "automated_status": status,
                "all_gates_passed": all_gates_passed,
                "gate_results": gates,
                "prediction": {
                    "probability": evaluation.get("prediction_probability"),
                    "lower_bound": evaluation.get("prediction_lower_bound", evaluation.get("probability_lower")),
                    "upper_bound": evaluation.get("prediction_upper_bound", evaluation.get("probability_upper")),
                    "confidence_grade": evaluation.get("confidence_grade"),
                },
                "analyst_evidence": evidence,
                "market_evaluation": {
                    "market_no_vig_probability": evaluation.get("market_no_vig_probability"),
                    "best_price": evaluation.get("best_price"),
                    "best_price_books": evaluation.get("best_price_books", []),
                    "break_even_probability": evaluation.get("break_even_probability"),
                    "edge_percentage_points": evaluation.get("edge_percentage_points"),
                    "expected_value_per_unit_risk": evaluation.get("expected_value_per_unit_risk"),
                    "eligible_bookmaker_count": evaluation.get(
                        "eligible_bookmaker_count", evaluation.get("bookmaker_count")
                    ),
                },
                "prediction_checksum": prediction_checksum,
                "evidence_checksum": evidence_checksum,
                "policy_evaluation_checksum": evaluation_checksum,
                "required_source_checksums": required_source_checksums,
            }
        )

    items.sort(key=lambda item: (item["scheduled_first_pitch_utc"], item["event_id"]))
    unsigned: dict[str, Any] = {
        "contract_version": DAILY_CARD_CONTRACT_VERSION,
        "draft_id": f"draft_{uuid4().hex}",
        "run_id": run_id,
        "requested_date": _canonical_date(requested_date),
        "policy_version": policy_version,
        "generated_at": generated.isoformat(),
        "review_status": "REVIEW_REQUIRED",
        "automatic_publication": False,
        "candidate_count": sum(item["automated_status"] == CANDIDATE_STATUS for item in items),
        "zero_candidate_day": not any(item["automated_status"] == CANDIDATE_STATUS for item in items),
        "policy_evaluation_ids": [
            item["policy_evaluation_id"] for item in items if item["automated_status"] == CANDIDATE_STATUS
        ],
        "source_checksum": payload_checksum(
            [
                {
                    "policy_evaluation_checksum": item["policy_evaluation_checksum"],
                    "prediction_checksum": item["prediction_checksum"],
                    "required_source_checksums": item["required_source_checksums"],
                }
                for item in items
            ]
        ),
        "items": items,
    }
    return {**unsigned, "draft_checksum": payload_checksum(unsigned)}


def _verify_draft_checksum(draft: Mapping[str, Any]) -> None:
    expected = draft.get("draft_checksum")
    unsigned = {key: value for key, value in draft.items() if key != "draft_checksum"}
    if not isinstance(expected, str) or payload_checksum(unsigned) != expected:
        raise ApprovalFailure(ApprovalFailureCode.DRAFT_CHECKSUM_CHANGED, "daily card draft checksum changed")


def _review_decision(value: Any) -> tuple[str, str | None, str | None]:
    if not isinstance(value, Mapping):
        raise ValueError("review decision must be a mapping with a nonempty reason")
    decision = _mapping(value, "review decision")
    raw = decision.get("decision")
    reason = decision.get("reason")
    bookmaker = decision.get("bookmaker_key")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("review decision must include a nonempty reason")
    return (
        str(raw).upper(),
        reason.strip(),
        str(bookmaker) if bookmaker is not None else None,
    )


def _batch_review_signoff(
    value: Any,
    *,
    zero_candidate_day: bool,
) -> tuple[str, bool]:
    if not isinstance(value, Mapping):
        raise ValueError("batch_review must be a SIGN_OFF mapping with a nonempty reason")
    review = _mapping(value, "batch review")
    if str(review.get("decision") or "").upper() != "SIGN_OFF":
        raise ValueError("batch_review decision must be SIGN_OFF")
    reason = review.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("batch_review must include a nonempty reason")
    acknowledged = review.get("zero_candidate_day_acknowledged", False)
    if not isinstance(acknowledged, bool):
        raise ValueError("batch_review zero_candidate_day_acknowledged must be boolean")
    if zero_candidate_day and acknowledged is not True:
        raise ValueError(
            "batch_review zero_candidate_day_acknowledged must be true for a zero-candidate day"
        )
    return reason.strip(), acknowledged


def _fail(code: ApprovalFailureCode, event_id: str, detail: str) -> None:
    raise ApprovalFailure(code, f"{event_id}: {detail}", event_id=event_id)


def approve_daily_card(
    draft: Mapping[str, Any],
    *,
    reviewer_id: str,
    decisions: Mapping[str, Any],
    batch_review: Mapping[str, Any],
    current_event_states: Mapping[str, Mapping[str, Any]],
    current_policy_version: str,
    approved_at: datetime | str,
) -> ImmutablePublicationPackage:
    """Complete mandatory local review after revalidating every candidate."""
    normalized_draft = _mapping(draft, "daily card draft")
    _verify_draft_checksum(normalized_draft)
    if not reviewer_id.strip():
        raise ValueError("reviewer_id is required")
    if normalized_draft.get("policy_version") != current_policy_version:
        raise ApprovalFailure(ApprovalFailureCode.POLICY_VERSION_CHANGED, "candidate policy version changed")
    approval_time = _aware_datetime(approved_at, "approved_at")
    items = normalized_draft.get("items")
    if not isinstance(items, list):
        raise ValueError("daily card items must be a list")
    candidate_count = sum(
        isinstance(item, dict) and item.get("automated_status") == CANDIDATE_STATUS
        for item in items
    )
    batch_reason, zero_candidate_day_acknowledged = _batch_review_signoff(
        batch_review,
        zero_candidate_day=candidate_count == 0,
    )
    known_events = {str(item.get("event_id")) for item in items if isinstance(item, dict)}
    if set(decisions) - known_events:
        raise ApprovalFailure(ApprovalFailureCode.INVALID_REVIEW_DECISION, "decision references an unknown event")

    review_records: list[dict[str, Any]] = []
    published_plays: list[dict[str, Any]] = []
    for raw_item in items:
        item = _mapping(raw_item, "daily card item")
        event_id = item.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("daily card item requires event_id")
        automated_status = item.get("automated_status")
        supplied_decision = decisions.get(event_id)

        if automated_status == PASS_STATUS:
            if supplied_decision is not None:
                decision, _reason, _bookmaker = _review_decision(supplied_decision)
                if decision != PASS_STATUS:
                    _fail(
                        ApprovalFailureCode.AUTOMATED_PASS_CANNOT_BE_PROMOTED,
                        event_id,
                        "an automated PASS cannot be promoted by human review",
                    )
            continue
        if automated_status != CANDIDATE_STATUS:
            _fail(ApprovalFailureCode.INVALID_REVIEW_DECISION, event_id, "unknown automated status")
        if supplied_decision is None:
            _fail(ApprovalFailureCode.MISSING_REVIEW_DECISION, event_id, "candidate requires a review decision")
        decision, reason, selected_bookmaker = _review_decision(supplied_decision)
        if decision not in {"APPROVE", PASS_STATUS}:
            _fail(ApprovalFailureCode.INVALID_REVIEW_DECISION, event_id, "decision must be APPROVE or PASS")

        state_raw = current_event_states.get(event_id)
        if state_raw is None:
            _fail(ApprovalFailureCode.MISSING_CURRENT_EVENT_STATE, event_id, "current event state is required")
        state = _mapping(state_raw, "current event state")
        required_state_fields = {
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
            "current_policy_evaluation",
        }
        missing_state_fields = sorted(required_state_fields - set(state))
        if missing_state_fields:
            _fail(
                ApprovalFailureCode.MISSING_CURRENT_EVENT_STATE,
                event_id,
                "current event state is missing: " + ", ".join(missing_state_fields),
            )
        current_first_pitch = _aware_datetime(
            state.get("scheduled_first_pitch_utc"), "current scheduled first pitch"
        )
        draft_first_pitch = _aware_datetime(item.get("scheduled_first_pitch_utc"), "draft scheduled first pitch")
        if current_first_pitch != draft_first_pitch:
            _fail(ApprovalFailureCode.SOURCE_EVIDENCE_CHANGED, event_id, "scheduled first pitch changed")
        if approval_time >= current_first_pitch:
            _fail(ApprovalFailureCode.FIRST_PITCH_REACHED, event_id, "review closed at scheduled first pitch")
        if state.get("event_status") != "pregame":
            _fail(ApprovalFailureCode.EVENT_NOT_PREGAME, event_id, "event is no longer pregame")
        if state["best_price_fresh"] is not True:
            _fail(ApprovalFailureCode.ODDS_NOT_FRESH, event_id, "best offered odds are not fresh")
        if state.get("policy_version") != normalized_draft.get("policy_version"):
            _fail(ApprovalFailureCode.POLICY_VERSION_CHANGED, event_id, "candidate policy version changed")
        if state.get("prediction_checksum") != item.get("prediction_checksum"):
            _fail(ApprovalFailureCode.PREDICTION_CHECKSUM_CHANGED, event_id, "sealed prediction changed")
        if state.get("evidence_checksum") != item.get("evidence_checksum"):
            _fail(ApprovalFailureCode.EVIDENCE_CHECKSUM_CHANGED, event_id, "analyst evidence changed")
        if state.get("policy_evaluation_checksum") != item.get("policy_evaluation_checksum"):
            _fail(ApprovalFailureCode.EVALUATION_CHECKSUM_CHANGED, event_id, "market evaluation changed")
        market = _mapping(item.get("market_evaluation", {}), "market evaluation")
        if state["best_price"] != market.get("best_price"):
            _fail(ApprovalFailureCode.MARKET_PRICE_CHANGED, event_id, "best offered price changed")
        best_books = market.get("best_price_books", [])
        if not isinstance(best_books, list):
            raise ValueError("best_price_books must be a list")
        current_best_books = state["best_price_books"]
        if not isinstance(current_best_books, list):
            raise ValueError("current best_price_books must be a list")
        if sorted(str(book) for book in current_best_books) != sorted(str(book) for book in best_books):
            _fail(ApprovalFailureCode.MARKET_PRICE_CHANGED, event_id, "best-price bookmakers changed")
        current_sources = _mapping(state["required_source_checksums"], "current source checksums")
        if current_sources != item.get("required_source_checksums"):
            _fail(ApprovalFailureCode.SOURCE_EVIDENCE_CHANGED, event_id, "required source evidence changed")
        current_evaluation = _mapping(
            state["current_policy_evaluation"], "current policy evaluation"
        )
        current_gates = _gates(current_evaluation)
        if (
            _status(current_evaluation) != CANDIDATE_STATUS
            or not all(gate["passed"] is True for gate in current_gates)
        ):
            _fail(
                ApprovalFailureCode.FAILED_AUTOMATED_GATE,
                event_id,
                "a current automated candidate gate failed",
            )
        current_market = {
            "market_no_vig_probability": current_evaluation.get(
                "market_no_vig_probability"
            ),
            "best_price": current_evaluation.get("best_price"),
            "best_price_books": current_evaluation.get("best_price_books", []),
            "break_even_probability": current_evaluation.get(
                "break_even_probability"
            ),
            "edge_percentage_points": current_evaluation.get(
                "edge_percentage_points"
            ),
            "expected_value_per_unit_risk": current_evaluation.get(
                "expected_value_per_unit_risk"
            ),
            "eligible_bookmaker_count": current_evaluation.get(
                "eligible_bookmaker_count", current_evaluation.get("bookmaker_count")
            ),
        }
        if (
            current_evaluation.get("selected_team_key")
            != item.get("selected_team_key")
            or current_market != market
        ):
            _fail(
                ApprovalFailureCode.EVALUATION_CHECKSUM_CHANGED,
                event_id,
                "current market evaluation differs from the reviewed draft",
            )
        gate_results = item.get("gate_results", [])
        if (
            item.get("all_gates_passed") is not True
            or not isinstance(gate_results, list)
            or not gate_results
            or not all(isinstance(gate, dict) and gate.get("passed") is True for gate in gate_results)
        ):
            _fail(ApprovalFailureCode.FAILED_AUTOMATED_GATE, event_id, "an automated candidate gate failed")

        if selected_bookmaker is None and best_books:
            selected_bookmaker = sorted(str(book) for book in best_books)[0]
        if selected_bookmaker is not None and selected_bookmaker not in best_books:
            _fail(
                ApprovalFailureCode.INVALID_REVIEW_DECISION,
                event_id,
                "selected bookmaker does not offer the evaluated best price",
            )
        persistence_decision = "approved" if decision == "APPROVE" else "rejected_to_pass"
        record = {
            "decision_id": f"decision_{uuid4().hex}",
            "event_id": event_id,
            "policy_evaluation_id": item.get("policy_evaluation_id"),
            "decision": persistence_decision,
            "reason": reason,
            "reviewer_id": reviewer_id,
            "decided_at": approval_time.isoformat(),
            "draft_checksum": normalized_draft["draft_checksum"],
            "prediction_checksum": item["prediction_checksum"],
            "evidence_checksum": item["evidence_checksum"],
            "policy_version": normalized_draft["policy_version"],
            "required_source_checksums": item["required_source_checksums"],
            "source_checksum": payload_checksum(item["required_source_checksums"]),
        }
        review_records.append(record)
        if decision == "APPROVE":
            published_plays.append(
                {
                    **item,
                    "play_id": f"play_{uuid4().hex}",
                    "market_key": "h2h",
                    "selection": item.get("selected_team_key"),
                    "publication_price": market.get("best_price"),
                    "bookmaker_key": selected_bookmaker,
                    "market_no_vig_probability": market.get("market_no_vig_probability"),
                    "break_even_probability": market.get("break_even_probability"),
                    "edge_percentage_points": market.get("edge_percentage_points"),
                    "expected_value_per_unit_risk": market.get("expected_value_per_unit_risk"),
                    "confidence_grade": item.get("prediction", {}).get("confidence_grade"),
                    "source_checksum": record["source_checksum"],
                    "canonical_game_checksum": item["required_source_checksums"].get("canonical_game"),
                    "feature_checksum": item["required_source_checksums"].get("features"),
                    "market_evaluation_checksum": item["policy_evaluation_checksum"],
                    "policy_version": normalized_draft["policy_version"],
                    "published_at": approval_time.isoformat(),
                    "review": record,
                }
            )

    batch_review_record = {
        "decision": "signed_off",
        "reason": batch_reason,
        "reviewer_id": reviewer_id,
        "reviewed_at": approval_time.isoformat(),
        "candidate_count": candidate_count,
        "published_play_count": len(published_plays),
        "zero_candidate_day_acknowledged": zero_candidate_day_acknowledged,
    }
    payload = {
        "contract_version": PUBLICATION_PACKAGE_CONTRACT_VERSION,
        "batch_id": f"publication_{uuid4().hex}",
        "draft_id": normalized_draft.get("draft_id"),
        "draft_checksum": normalized_draft["draft_checksum"],
        "run_id": normalized_draft.get("run_id"),
        "requested_date": normalized_draft.get("requested_date"),
        "policy_version": normalized_draft.get("policy_version"),
        "source_checksum": normalized_draft.get("source_checksum"),
        "approved_at": approval_time.isoformat(),
        "reviewer_id": reviewer_id,
        "review_status": "HUMAN_REVIEW_COMPLETE",
        "automatic_publication": False,
        "public_release_performed": False,
        "published_play_count": len(published_plays),
        "zero_published_play_day": len(published_plays) == 0,
        "batch_review": batch_review_record,
        "review_decisions": review_records,
        "published_plays": published_plays,
    }
    return ImmutablePublicationPackage.from_payload(payload)
