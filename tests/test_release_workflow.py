import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import app.config as config_module
from app.analysis import assemble_canonical_game, assemble_features
from app.artifacts import (
    ANALYSIS_FEATURES_FILENAME,
    CANONICAL_GAMES_FILENAME,
    ArtifactPaths,
)
from app.config import Settings
from app.database import Database
from app.exporter import write_json
from app.publication import render_analysis_features, render_canonical_games
from app.release_workflow import ReleaseWorkflow
from app.run_state import RunStatus


RUN_ID = "run_20260716_0123456789abcdef0123456789abcdef"
EVENT_ID = "event-release-workflow"
T0 = datetime(2026, 7, 16, 20, 0, tzinfo=timezone.utc)


def _odds_summary() -> dict[str, object]:
    books = (
        ("book_a", 105, -115),
        ("book_b", 100, -110),
        ("book_c", -105, -105),
        ("book_d", 105, -115),
    )
    outcomes: dict[str, dict[str, object]] = {}
    for team, price_index in (("HOM", 1), ("AWY", 2)):
        outcomes[team] = {
            "offers": [
                {
                    "bookmaker_key": book,
                    "price": prices[price_index - 1],
                    "effective_provider_timestamp": "2026-07-16T19:59:30+00:00",
                    "provider_retrieved_at": "2026-07-16T19:59:35+00:00",
                    "calculation_eligible": True,
                    "provider_order": index,
                }
                for index, (book, *prices) in enumerate(books)
            ]
        }
    return {
        "event_id": EVENT_ID,
        "commence_time": "2026-07-16T23:00:00+00:00",
        "raw_home_team": "Home Team",
        "raw_away_team": "Away Team",
        "home_team_key": "HOM",
        "away_team_key": "AWY",
        "markets": {
            "h2h": {
                "lines": {
                    "moneyline": {
                        "complete_two_way_market": True,
                        "outcomes": outcomes,
                    }
                }
            }
        },
    }


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReleaseWorkflow:
    phase2_report = tmp_path / "PHASE2_LIVE_WEATHER_VALIDATION_REPORT.md"
    phase2_report.write_text(
        "# Fixture acceptance evidence\nPHASE2_RELEASE_GATE: ACCEPTED\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        config_module, "PHASE2_LIVE_WEATHER_REPORT_PATH", phase2_report
    )
    settings = Settings(
        database_path=tmp_path / "release.db",
        artifact_dir=tmp_path / "artifacts",
        service_auth_token="test-token",
        odds_api_key="test-odds-key",
        openweather_enabled=False,
        phase2_live_weather_accepted=True,
        phase2_weather_evidence_sha256=hashlib.sha256(
            phase2_report.read_bytes()
        ).hexdigest(),
    )
    database = Database(settings.database_path)
    database.create_run(RUN_ID, date(2026, 7, 16))
    database.transition_run(RUN_ID, RunStatus.RUNNING)
    database.upsert_game(
        {
            "id": EVENT_ID,
            "commence_time": "2026-07-16T23:00:00+00:00",
            "home_team": "Home Team",
            "away_team": "Away Team",
        },
        "HOM",
        "AWY",
        run_id=RUN_ID,
    )
    database.transition_run(RUN_ID, RunStatus.COMPLETED)
    verified = {
        "status": "VERIFIED",
        "source_ids": ["deterministic_fixture"],
        "verified_at": "2026-07-12",
        "notes": "Deterministic runtime fixture.",
        "conflicts": [],
    }
    game = assemble_canonical_game(
        run_id=RUN_ID,
        requested_date=date(2026, 7, 16),
        odds_summary=_odds_summary(),
        weather_packet={"weather_status": "indoor_fixed_roof"},
        venue={
            "physical_venue_key": "fixture-park",
            "venue": "Fixture Park",
            "team_key": "HOM",
            "active_club_association": True,
            "timezone": "America/Los_Angeles",
            "roof_type": "fixed",
            "latitude": 33.0,
            "longitude": -118.0,
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
        assembled_at=T0,
    )
    feature = assemble_features(game, generated_at=T0)
    paths = ArtifactPaths(settings.artifact_dir, date(2026, 7, 16), RUN_ID)
    write_json(paths.json_path(CANONICAL_GAMES_FILENAME), render_canonical_games([game], generated_at=T0))
    write_json(paths.json_path(ANALYSIS_FEATURES_FILENAME), render_analysis_features([feature], generated_at=T0))
    return ReleaseWorkflow(settings, database)


def _prediction_input(workflow: ReleaseWorkflow) -> dict[str, object]:
    template = workflow.prediction_template(RUN_ID, EVENT_ID)
    prediction = template["prediction"]
    assert isinstance(prediction, dict)
    source = {
        "source_id": "owner_review",
        "source_name": "Owner review evidence",
        "reference": "local-review-record",
        "observed_at": T0.isoformat(),
    }
    available = {
        "state": "available",
        "assessment": "Reviewed and recorded by the named analyst.",
        "source_ids": ["owner_review"],
    }
    prediction.update(
        {
            "home_probability": 0.70,
            "home_probability_lower": 0.65,
            "home_probability_upper": 0.75,
            "generated_at": T0.isoformat(),
            "market_independence_attested": True,
            "evidence": {
                "analyst_identity": "release-owner",
                "method_version": "DSE_REVIEWED_ANALYST_V1",
                "starting_pitching": available,
                "bullpen": available,
                "offensive_matchup": available,
                "lineup_information_state": "confirmed",
                "lineup_notes": "Reviewed lineup state.",
                "lineup_source_ids": ["owner_review"],
                "venue_context": available,
                "weather_context": available,
                "schedule_rest_context": available,
                "material_unknowns": [],
                "source_provenance": [source],
            },
        }
    )
    return template


def test_market_blind_template_and_full_human_review_path(workflow: ReleaseWorkflow) -> None:
    template = _prediction_input(workflow)
    serialized = str(template).lower()
    assert "no_vig" not in serialized
    assert "edge" not in serialized
    assert "expected_value" not in serialized
    assert "candidate" not in serialized

    prediction = workflow.seal_reviewed_prediction(RUN_ID, template, sealed_at=T0)
    evaluation = workflow.evaluate_prediction(RUN_ID, prediction.prediction_id, evaluated_at=T0)
    assert evaluation["decision"] == "CANDIDATE_REQUIRES_REVIEW"
    assert all(gate["source_checksum"] for gate in evaluation["gate_results"])

    draft = workflow.create_draft(RUN_ID, generated_at=T0)
    assert draft["review_status"] == "REVIEW_REQUIRED"
    assert draft["automatic_publication"] is False
    best_book = evaluation["best_price_books"][0]
    package = workflow.approve_draft(
        RUN_ID,
        draft,
        reviewer_id="release-owner",
        batch_review={
            "decision": "SIGN_OFF",
            "reason": "Owner completed mandatory review of the full daily card.",
            "zero_candidate_day_acknowledged": False,
        },
        decisions={
            EVENT_ID: {
                "decision": "APPROVE",
                "reason": "Owner approved after reviewing the sealed evidence.",
                "bookmaker_key": best_book,
            }
        },
        approved_at=T0.replace(second=30),
    )

    assert package["published_play_count"] == 1
    assert package["automatic_publication"] is False
    assert package["public_release_performed"] is False
    play_id = package["published_plays"][0]["play_id"]
    settlement = workflow.settle(
        RUN_ID,
        {
            "settlement_event_id": "settlement_fixture_original",
            "batch_id": package["batch_id"],
            "play_id": play_id,
            "event_kind": "original",
            "result": "win",
            "corrects_event_id": None,
            "settled_at": "2026-07-17T02:00:00+00:00",
            "recorded_at": "2026-07-17T02:01:00+00:00",
            "source_checksum": "a" * 64,
            "notes": "Deterministic fixture settlement.",
        },
    )
    assert settlement["result"] == "win"
    assert workflow.database.list_settlement_ledger(batch_id=package["batch_id"])[0][
        "play_id"
    ] == play_id
    assert workflow.database.integrity_check()["ok"] is True
