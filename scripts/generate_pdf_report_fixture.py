from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT_STR = str(REPO_ROOT)
if REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, REPO_ROOT_STR)

from app.pdf_report.artifact import write_pdf_report_artifacts  # noqa: E402
from app.pdf_report.assembly import assemble_pdf_report_document  # noqa: E402
from app.rankings.engine import rank_recommendations  # noqa: E402
from app.recommendation_gate.engine import evaluate_recommendation_gate  # noqa: E402
from tests.test_recommendation_gate import _eligible_value_engine  # noqa: E402
from tests.test_value_engine import _packet_predictions  # noqa: E402


def build_fixture(output_root: Path) -> Path:
    packet, predictions = _packet_predictions()
    recommendation_gate = evaluate_recommendation_gate(_eligible_value_engine())
    rankings = rank_recommendations(recommendation_gate)
    document = assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=recommendation_gate,
        predictions=predictions,
        matchup_packet=packet,
    )
    artifacts = write_pdf_report_artifacts(document, output_root)
    return output_root / artifacts.pdf_relpath


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the deterministic PDF Report V1 fixture artifact."
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path(".validation/pdf-report-v1"),
    )
    args = parser.parse_args()
    pdf_path = build_fixture(args.output_root)
    print(pdf_path)


if __name__ == "__main__":
    main()
