from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.infographic.artifact import publish_infographic_artifacts  # noqa: E402
from app.infographic.assembly import assemble_infographic_document  # noqa: E402
from app.pdf_report.artifact import publish_production_pdf_report_artifacts  # noqa: E402
from app.pdf_report.policy import PdfReportPolicyV1  # noqa: E402
from app.pdf_report.production import (  # noqa: E402
    PdfReportGameV1,
    PdfReportOutcomeV1,
    ProductionPdfReportV1,
)
from app.pre_model_evidence import PreModelArtifactV1  # noqa: E402


NOW = datetime(2026, 8, 7, 16, 0, tzinfo=timezone.utc)
SHA = "a" * 64


def _game(ordinal: int, decision: str, rank: int | None = None) -> PdfReportGameV1:
    def outcome(side: str) -> PdfReportOutcomeV1:
        return PdfReportOutcomeV1(
            side=side,
            outcome_team_id=f"fixture-team-{side}-{ordinal}",
            prediction_probability=0.55 if side == "home" else 0.45,
            probability_lower=0.48 if side == "home" else 0.38,
            probability_upper=0.62 if side == "home" else 0.52,
            availability="available",
            bookmaker_count=4,
            consensus_no_vig_probability=0.51 if side == "home" else 0.49,
            best_price=-105.0 if side == "home" else 115.0,
            best_price_bookmakers=("fixture_book",),
            edge=0.04 if side == "home" else -0.04,
            expected_value_per_unit=0.08 if side == "home" else -0.08,
            lower_bound_clearance=-0.03 if side == "home" else -0.11,
            freshness_state="fresh",
            value_checksum=SHA,
            prediction_checksum="b" * 64,
            market_context_checksum="c" * 64,
        )

    away_player = {
        "canonical_player_id": f"fixture-away-player-{ordinal}",
        "full_name": f"Alex Awaystarter {ordinal}",
        "source_player_id": f"{ordinal}01",
    }
    home_player = {
        "canonical_player_id": f"fixture-home-player-{ordinal}",
        "full_name": f"Henry Homestarter {ordinal}",
        "source_player_id": f"{ordinal}02",
    }
    context = {
        "baseball_intelligence": {
            "away": {
                "bullpen_source_player_ids": (f"{ordinal}03", f"{ordinal}04"),
                "coverage": {
                    "bullpen_feature_count": 2,
                    "bullpen_player_count": 2,
                    "lineup_feature_count": 2,
                    "lineup_player_count": 2,
                    "starter_feature_available": True,
                },
                "lineup_source_player_ids": (f"{ordinal}05", f"{ordinal}06"),
                "players": (
                    away_player,
                    {"full_name": f"Away Reliever One {ordinal}", "source_player_id": f"{ordinal}03"},
                    {"full_name": f"Away Reliever Two {ordinal}", "source_player_id": f"{ordinal}04"},
                    {"full_name": f"Away Batter One {ordinal}", "source_player_id": f"{ordinal}05"},
                    {"full_name": f"Away Batter Two {ordinal}", "source_player_id": f"{ordinal}06"},
                ),
            },
            "home": {
                "bullpen_source_player_ids": (f"{ordinal}07", f"{ordinal}08"),
                "coverage": {
                    "bullpen_feature_count": 2,
                    "bullpen_player_count": 2,
                    "lineup_feature_count": 2,
                    "lineup_player_count": 2,
                    "starter_feature_available": True,
                },
                "lineup_source_player_ids": (f"{ordinal}09", f"{ordinal}10"),
                "players": (
                    home_player,
                    {"full_name": f"Home Reliever One {ordinal}", "source_player_id": f"{ordinal}07"},
                    {"full_name": f"Home Reliever Two {ordinal}", "source_player_id": f"{ordinal}08"},
                    {"full_name": f"Home Batter One {ordinal}", "source_player_id": f"{ordinal}09"},
                    {"full_name": f"Home Batter Two {ordinal}", "source_player_id": f"{ordinal}10"},
                ),
            },
            "venue_id": f"fixture-park-{ordinal}",
        },
        "data_quality": {
            "disposition": "degraded" if decision == "avoid" else "clear",
            "issues": (
                {
                    "code": "weather_evidence",
                    "message": "Weather evidence requires operator attention.",
                    "severity": "warning",
                },
            )
            if decision == "avoid"
            else (),
        },
        "game_state": {
            "away": {
                "lineup": {
                    "availability": "partial",
                    "entries": (
                        {"batting_order_slot": 1, "player": {"full_name": f"Away Batter One {ordinal}"}},
                        {"batting_order_slot": 2, "player": {"full_name": f"Away Batter Two {ordinal}"}},
                    ),
                },
                "starter": {"certainty": "probable", "player": away_player},
            },
            "game_status": "scheduled",
            "home": {
                "lineup": {
                    "availability": "partial",
                    "entries": (
                        {"batting_order_slot": 1, "player": {"full_name": f"Home Batter One {ordinal}"}},
                        {"batting_order_slot": 2, "player": {"full_name": f"Home Batter Two {ordinal}"}},
                    ),
                },
                "starter": {"certainty": "probable", "player": home_player},
            },
        },
        "odds_weather": {
            "odds": {
                "availability": "available",
                "freshness_counts": {"fresh": 8, "stale": 0},
                "normalized_market_count": 1,
                "raw_snapshot_count": 8,
                "retrieved_at": NOW.isoformat(),
            },
            "weather": {
                "baseball_wind_impact": {"classification": "crosswind"},
                "nws": {
                    "forecast": {
                        "precipitation_probability_pct": 15,
                        "temperature_f": 74,
                        "wind_direction_cardinal": "SW",
                        "wind_speed_mph": 8,
                    }
                },
                "primary_source": "nws",
                "relevance": "outdoor",
                "status": "available",
                "venue_context": {
                    "operational_roof_status": "open",
                    "roof_type": "open_air",
                    "venue_name": f"Fixture Ballpark {ordinal}",
                },
            },
        },
        "schedule": {
            "away_probable_starter": away_player,
            "doubleheader_status": "single_game",
            "game_number": 1,
            "game_status": "scheduled",
            "home_probable_starter": home_player,
            "scheduled_start_time": (NOW + timedelta(hours=ordinal)).isoformat(),
            "source_venue_name": f"Fixture Ballpark {ordinal}",
        },
    }
    outcomes = (outcome("home"), outcome("away"))
    return PdfReportGameV1(
        ordinal=ordinal,
        source_game_id=f"fixture-game-{ordinal}",
        away_team_id=f"fixture-away-{ordinal}",
        home_team_id=f"fixture-home-{ordinal}",
        scheduled_start_time=NOW + timedelta(hours=ordinal),
        decision=decision,
        selected_side="home" if decision == "recommend" else None,
        selected_team_id=f"fixture-team-home-{ordinal}" if decision == "recommend" else None,
        recommendation_rank=rank,
        quality_disposition="degraded" if decision == "avoid" else "clear",
        quality_issue_codes=("weather_evidence",) if decision == "avoid" else (),
        provider_kind="reviewed_analyst",
        provider_contract="DSE_REVIEWED_ANALYST_V1",
        provider_version="reviewed-fixture-v1",
        calibration_state="uncalibrated",
        market_independence_attested=True,
        outcomes=outcomes,
        context=context,
        upstream_ranking_entry_checksum="d" * 64,
        upstream_gate_game_checksum="e" * 64,
        upstream_value_game_checksum="f" * 64,
        upstream_prediction_game_checksum="1" * 64,
        upstream_matchup_packet_game_checksum="2" * 64,
        upstream_data_quality_game_checksum="3" * 64,
    )


def _report(game_count: int = 3) -> ProductionPdfReportV1:
    upstream = (
        "rankings",
        "recommendation_gate",
        "value_engine",
        "predictions",
        "matchup_packet",
        "data_quality",
    )
    games: list[PdfReportGameV1] = []
    recommendation_rank = 0
    for ordinal in range(1, game_count + 1):
        if ordinal == 1 or (game_count > 3 and ordinal % 5 == 1):
            decision = "recommend"
            recommendation_rank += 1
            rank: int | None = recommendation_rank
        else:
            decision = "avoid" if ordinal % 3 == 0 else "pass"
            rank = None
        games.append(_game(ordinal, decision, rank))
    return ProductionPdfReportV1(
        run_id="run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        requested_date="2026-08-07",
        as_of_time=NOW,
        generated_at=NOW + timedelta(minutes=1),
        policy=PdfReportPolicyV1(),
        upstream_snapshot_ids={key: f"{key}:fixture" for key in upstream},
        upstream_checksums={key: SHA for key in upstream},
        games=tuple(games),
    )


def _artifact(artifact: PreModelArtifactV1) -> dict[str, object]:
    return {
        "byte_count": artifact.byte_count,
        "relpath": artifact.relpath,
        "sha256": artifact.checksum,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic provider-free final-output fixtures")
    parser.add_argument("--output-root", type=Path, default=Path(".validation/final-output-fixture"))
    parser.add_argument("--game-count", type=int, default=3)
    args = parser.parse_args()
    if args.game_count < 0:
        parser.error("--game-count must be nonnegative")
    root = args.output_root.resolve()
    report = _report(args.game_count)
    pdf = publish_production_pdf_report_artifacts(report, root)
    infographic = assemble_infographic_document(
        report,
        upstream_pdf_report_snapshot_id="pdf-report:fixture",
        generated_at=NOW + timedelta(minutes=2),
    )
    visuals = publish_infographic_artifacts(infographic, root)
    print(
        json.dumps(
            {
                "application_provider_network_requests": 0,
                "artifact_root": str(root),
                "infographic_checksum": infographic.checksum,
                "infographic_files": {
                    "document": _artifact(visuals.document),
                    "feed": _artifact(visuals.feed),
                    "manifest": _artifact(visuals.manifest),
                    "story": _artifact(visuals.story),
                },
                "pdf_files": {
                    "document": _artifact(pdf.document),
                    "manifest": _artifact(pdf.manifest),
                    "page_count": pdf.page_count,
                    "pdf": _artifact(pdf.pdf),
                },
                "semantic_report_checksum": report.checksum,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
