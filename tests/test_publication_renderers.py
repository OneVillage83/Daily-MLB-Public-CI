from __future__ import annotations

from datetime import datetime, timezone
import json

from app.publication import (
    build_publication_manifest,
    render_analysis_features,
    render_candidate_policy_results,
    render_canonical_games,
    render_daily_card_markdown,
    render_json_bytes,
    render_prediction_evaluations,
    render_results_ledger,
    render_sealed_predictions,
)


NOW = datetime(2026, 7, 16, 15, 0, tzinfo=timezone.utc)


def test_machine_readable_record_renderers_preserve_numeric_values() -> None:
    record = {"event_id": "event-1", "probability": 0.58, "price": -110}
    documents = [
        render_canonical_games([record], generated_at=NOW),
        render_analysis_features([record], generated_at=NOW),
        render_sealed_predictions([record], generated_at=NOW),
        render_prediction_evaluations([record], generated_at=NOW),
        render_candidate_policy_results([record], generated_at=NOW),
    ]

    for document in documents:
        assert document["record_count"] == 1
        assert document["records"][0]["probability"] == 0.58
        assert isinstance(document["records"][0]["price"], int)
        assert json.loads(render_json_bytes(document))["records"][0]["probability"] == 0.58


def test_markdown_marks_unapproved_candidate_as_review_required() -> None:
    card = {
        "requested_date": "2026-07-16",
        "items": [
            {
                "event_id": "event-1",
                "selected_team_key": "LAD",
                "scheduled_first_pitch_utc": "2026-07-16T18:00:00+00:00",
                "automated_status": "CANDIDATE_REQUIRES_REVIEW",
                "prediction": {
                    "probability": 0.58,
                    "lower_bound": 0.52,
                    "upper_bound": 0.63,
                    "confidence_grade": "B",
                },
                "market_evaluation": {
                    "market_no_vig_probability": 0.53,
                    "best_price": -110,
                    "edge_percentage_points": 5.0,
                    "expected_value_per_unit_risk": 0.107,
                },
                "analyst_evidence": {
                    "starting_pitching": {
                        "state": "available",
                        "assessment": "Home starter has the stronger reviewed matchup.",
                    },
                    "bullpen": {"state": "unknown", "assessment": None},
                    "offensive_matchup": {
                        "state": "available",
                        "assessment": "Platoon context reviewed.",
                    },
                    "lineup_information_state": "projected",
                    "lineup_notes": "Confirmed lineups not yet available.",
                    "venue_context": {
                        "state": "available",
                        "assessment": "Outdoor venue context reviewed.",
                    },
                    "weather_context": {
                        "state": "available",
                        "assessment": "Fixture weather is within the approved offset.",
                    },
                    "schedule_rest_context": {
                        "state": "unknown",
                        "assessment": None,
                    },
                    "material_unknowns": ["Confirmed starting lineups"],
                },
            }
        ],
    }

    markdown = render_daily_card_markdown(card)

    assert "REVIEW REQUIRED" in markdown
    assert "Prediction: 58.0%" in markdown
    assert "Market no-vig baseline: 53.0%" in markdown
    assert "Starting pitching: available: Home starter" in markdown
    assert "Bullpen: unknown" in markdown
    assert "Lineup information: projected: Confirmed lineups" in markdown
    assert "Material unknowns: Confirmed starting lineups" in markdown
    assert "Passing automated gates does not imply publication" in markdown


def test_zero_candidate_markdown_is_a_valid_pass_card() -> None:
    markdown = render_daily_card_markdown({"requested_date": "2026-07-16", "items": []})

    assert "PASS - no reviewed plays qualify" in markdown


def test_manifest_records_exact_content_checksums_and_no_auto_publication() -> None:
    package = {"publication_checksum": "publication-checksum", "published_plays": []}
    artifacts = {"daily_card.md": "PASS\n", "daily_card.json": {"plays": []}}

    manifest = build_publication_manifest(package, artifacts, generated_at=NOW)

    assert manifest["automatic_publication"] is False
    assert [entry["name"] for entry in manifest["artifacts"]] == ["daily_card.json", "daily_card.md"]
    assert all(len(entry["sha256"]) == 64 for entry in manifest["artifacts"])
    assert len(manifest["manifest_checksum"]) == 64


def test_results_ledger_preserves_publication_and_appends_settlement_events() -> None:
    publication = {
        "publication_checksum": "immutable-source",
        "published_plays": [{"event_id": "event-1", "prediction": {"probability": 0.58}}],
    }
    settlement = {"event_id": "event-1", "result": "win", "settled_at": NOW.isoformat()}

    ledger = render_results_ledger([publication], [settlement], generated_at=NOW)

    assert ledger["published_play_count"] == 1
    assert ledger["settlement_event_count"] == 1
    assert ledger["published_plays"][0]["prediction"]["probability"] == 0.58
    assert ledger["settlement_events"] == [settlement]
