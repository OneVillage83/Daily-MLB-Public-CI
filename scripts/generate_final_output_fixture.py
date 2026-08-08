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
        context={"odds_weather": {"weather": {"status": "available"}}},
        upstream_ranking_entry_checksum="d" * 64,
        upstream_gate_game_checksum="e" * 64,
        upstream_value_game_checksum="f" * 64,
        upstream_prediction_game_checksum="1" * 64,
        upstream_matchup_packet_game_checksum="2" * 64,
        upstream_data_quality_game_checksum="3" * 64,
    )


def _report() -> ProductionPdfReportV1:
    upstream = (
        "rankings",
        "recommendation_gate",
        "value_engine",
        "predictions",
        "matchup_packet",
        "data_quality",
    )
    return ProductionPdfReportV1(
        run_id="run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        requested_date="2026-08-07",
        as_of_time=NOW,
        generated_at=NOW + timedelta(minutes=1),
        policy=PdfReportPolicyV1(),
        upstream_snapshot_ids={key: f"{key}:fixture" for key in upstream},
        upstream_checksums={key: SHA for key in upstream},
        games=(_game(1, "recommend", 1), _game(2, "pass"), _game(3, "avoid")),
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
    args = parser.parse_args()
    root = args.output_root.resolve()
    report = _report()
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
