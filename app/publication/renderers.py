from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from app.publication.checksums import canonical_json_bytes, normalize_payload, payload_checksum
from app.publication.review import CANDIDATE_STATUS, ImmutablePublicationPackage


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(timezone.utc).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _records_document(contract_version: str, records: Sequence[Any], generated_at: datetime | str | None) -> dict[str, Any]:
    normalized = [normalize_payload(record) for record in records]
    return {
        "contract_version": contract_version,
        "generated_at": _timestamp(generated_at),
        "record_count": len(normalized),
        "records": normalized,
    }


def render_canonical_games(records: Sequence[Any], *, generated_at: datetime | str | None = None) -> dict[str, Any]:
    return _records_document("canonical-games-export-v1", records, generated_at)


def render_analysis_features(records: Sequence[Any], *, generated_at: datetime | str | None = None) -> dict[str, Any]:
    return _records_document("analysis-features-export-v1", records, generated_at)


def render_sealed_predictions(records: Sequence[Any], *, generated_at: datetime | str | None = None) -> dict[str, Any]:
    return _records_document("sealed-predictions-export-v1", records, generated_at)


def render_prediction_evaluations(records: Sequence[Any], *, generated_at: datetime | str | None = None) -> dict[str, Any]:
    return _records_document("prediction-evaluations-export-v1", records, generated_at)


def render_candidate_policy_results(records: Sequence[Any], *, generated_at: datetime | str | None = None) -> dict[str, Any]:
    return _records_document("candidate-policy-results-export-v1", records, generated_at)


def render_daily_card(card: Mapping[str, Any] | ImmutablePublicationPackage) -> dict[str, Any]:
    if isinstance(card, ImmutablePublicationPackage):
        return card.as_dict()
    normalized = normalize_payload(card)
    if not isinstance(normalized, dict):
        raise TypeError("daily card must be a mapping")
    return normalized


def _pct(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value) * 100:.1f}%"
    return "unknown"


def _number(value: Any, suffix: str = "") -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}{suffix}"
    return "unknown"


def _assessment(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    state = str(value.get("state") or "unknown")
    detail = value.get("assessment")
    return f"{state}: {detail}" if isinstance(detail, str) and detail.strip() else state


def _material_unknowns(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "none recorded"
    return "; ".join(str(item) for item in value)


def render_daily_card_markdown(card: Mapping[str, Any] | ImmutablePublicationPackage) -> str:
    payload = render_daily_card(card)
    requested_date = payload.get("requested_date", "unknown date")
    is_package = "published_plays" in payload
    items = payload.get("published_plays" if is_package else "items", [])
    if not isinstance(items, list):
        raise ValueError("daily card records must be a list")
    title = "Daily Sports Edge - Daily MLB Card"
    lines = [f"# {title}", "", f"Date: {requested_date}", ""]
    lines.append("Human review complete." if is_package else "REVIEW REQUIRED - not approved for publication.")
    lines.append("")
    candidates = [item for item in items if isinstance(item, dict) and item.get("automated_status") == CANDIDATE_STATUS]
    if not candidates:
        lines.extend(["## Card", "", "PASS - no reviewed plays qualify.", ""])
    for item in candidates:
        event_id = item.get("event_id", "unknown-event")
        selected = item.get("selected_team_key", "unknown-team")
        market = item.get("market_evaluation", {})
        prediction = item.get("prediction", {})
        evidence = item.get("analyst_evidence", {})
        if not isinstance(evidence, Mapping):
            evidence = {}
        lineup_state = str(evidence.get("lineup_information_state") or "unknown")
        lineup_notes = evidence.get("lineup_notes")
        lineup = (
            f"{lineup_state}: {lineup_notes}"
            if isinstance(lineup_notes, str) and lineup_notes.strip()
            else lineup_state
        )
        lines.extend(
            [
                f"## {selected} ({event_id})",
                "",
                f"First pitch: {item.get('scheduled_first_pitch_utc', 'unknown')}",
                f"Prediction: {_pct(prediction.get('probability'))}",
                f"Prediction interval: {_pct(prediction.get('lower_bound'))} to {_pct(prediction.get('upper_bound'))}",
                f"Confidence: {prediction.get('confidence_grade', 'unknown')}",
                f"Market no-vig baseline: {_pct(market.get('market_no_vig_probability'))}",
                f"Best eligible price: {market.get('best_price', 'unknown')}",
                f"Edge: {_number(market.get('edge_percentage_points'), ' pp')}",
                f"EV per unit risk: {_number(market.get('expected_value_per_unit_risk'))}",
                f"Decision: {item.get('automated_status')}",
                "",
                "### Reviewed matchup evidence",
                "",
                f"- Starting pitching: {_assessment(evidence.get('starting_pitching'))}",
                f"- Bullpen: {_assessment(evidence.get('bullpen'))}",
                f"- Offensive matchup: {_assessment(evidence.get('offensive_matchup'))}",
                f"- Lineup information: {lineup}",
                f"- Venue context: {_assessment(evidence.get('venue_context'))}",
                f"- Weather context: {_assessment(evidence.get('weather_context'))}",
                f"- Schedule/rest context: {_assessment(evidence.get('schedule_rest_context'))}",
                f"- Material unknowns: {_material_unknowns(evidence.get('material_unknowns'))}",
                "",
            ]
        )
    lines.extend(
        [
            "Market evaluation and analyst prediction are separate. Passing automated gates does not imply publication.",
            "",
        ]
    )
    return "\n".join(lines)


def render_json_bytes(payload: Any, *, pretty: bool = True) -> bytes:
    normalized = normalize_payload(payload)
    if not pretty:
        return canonical_json_bytes(normalized)
    return json.dumps(normalized, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")


def build_publication_manifest(
    package: Mapping[str, Any] | ImmutablePublicationPackage,
    artifacts: Mapping[str, Any],
    *,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    rendered_package = render_daily_card(package)
    entries: list[dict[str, Any]] = []
    for name, value in sorted(artifacts.items()):
        if isinstance(value, bytes):
            content = value
        elif isinstance(value, str):
            content = value.encode("utf-8")
        else:
            content = canonical_json_bytes(value)
        entries.append({"name": name, "sha256": sha256(content).hexdigest(), "size_bytes": len(content)})
    unsigned = {
        "contract_version": "publication-manifest-v1",
        "generated_at": _timestamp(generated_at),
        "publication_checksum": rendered_package.get("publication_checksum"),
        "automatic_publication": False,
        "artifacts": entries,
    }
    return {**unsigned, "manifest_checksum": payload_checksum(unsigned)}


def render_results_ledger(
    publication_packages: Sequence[Mapping[str, Any] | ImmutablePublicationPackage],
    settlement_events: Sequence[Any],
    *,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    publications = [render_daily_card(package) for package in publication_packages]
    plays = [play for package in publications for play in package.get("published_plays", [])]
    settlements = [normalize_payload(event) for event in settlement_events]
    return {
        "contract_version": "results-ledger-v1",
        "generated_at": _timestamp(generated_at),
        "publication_batch_count": len(publications),
        "published_play_count": len(plays),
        "settlement_event_count": len(settlements),
        "publication_batches": publications,
        "published_plays": plays,
        "settlement_events": settlements,
    }
