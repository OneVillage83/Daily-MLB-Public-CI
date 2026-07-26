from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.artifacts import DAILY_CARD_JSON_FILENAME
from app.config import settings
from app.release_workflow import ReleaseWorkflow


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return parsed


def _json_file(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object")
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local, human-reviewed Daily MLB release-candidate workflow"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    assemble = subparsers.add_parser("assemble")
    assemble.add_argument("--run-id", required=True)
    assemble.add_argument("--at")

    template = subparsers.add_parser("prediction-template")
    template.add_argument("--run-id", required=True)
    template.add_argument("--event-id", required=True)
    template.add_argument("--output", required=True)

    seal = subparsers.add_parser("seal-prediction")
    seal.add_argument("--run-id", required=True)
    seal.add_argument("--input", required=True)
    seal.add_argument("--at")

    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("--run-id", required=True)
    evaluate.add_argument("--prediction-id", required=True)
    evaluate.add_argument("--at")

    draft = subparsers.add_parser("draft")
    draft.add_argument("--run-id", required=True)
    draft.add_argument("--at")

    approve = subparsers.add_parser("approve")
    approve.add_argument("--run-id", required=True)
    approve.add_argument("--reviewer-id", required=True)
    approve.add_argument("--decisions", required=True)
    approve.add_argument("--at")

    settle = subparsers.add_parser("settle")
    settle.add_argument("--run-id", required=True)
    settle.add_argument("--input", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    workflow = ReleaseWorkflow(settings)
    if args.command == "assemble":
        games, features = workflow.assemble_run(args.run_id, assembled_at=_timestamp(args.at))
        print(f"assembled {len(games)} canonical games and {len(features)} feature records")
    elif args.command == "prediction-template":
        template = workflow.prediction_template(args.run_id, args.event_id)
        Path(args.output).write_text(json.dumps(template, indent=2), encoding="utf-8")
        print("market-blind prediction template written")
    elif args.command == "seal-prediction":
        prediction = workflow.seal_reviewed_prediction(
            args.run_id, _json_file(args.input), sealed_at=_timestamp(args.at)
        )
        print(f"sealed prediction {prediction.prediction_id} checksum {prediction.checksum}")
    elif args.command == "evaluate":
        evaluation = workflow.evaluate_prediction(
            args.run_id, args.prediction_id, evaluated_at=_timestamp(args.at)
        )
        print(f"policy evaluation {evaluation['evaluation_id']} outcome {evaluation['decision']}")
    elif args.command == "draft":
        result = workflow.create_draft(args.run_id, generated_at=_timestamp(args.at))
        print(f"draft {result['draft_id']} requires human review; candidates={result['candidate_count']}")
    elif args.command == "approve":
        paths = workflow._paths(args.run_id)
        draft_payload = _json_file(str(paths.json_path(DAILY_CARD_JSON_FILENAME)))
        decisions_payload = _json_file(args.decisions)
        decisions = decisions_payload.get("decisions", decisions_payload)
        if not isinstance(decisions, dict):
            raise ValueError("decisions must be a JSON object")
        batch_review = decisions_payload.get("batch_review")
        if not isinstance(batch_review, dict):
            raise ValueError("decisions JSON must contain a batch_review object")
        package = workflow.approve_draft(
            args.run_id,
            draft_payload,
            reviewer_id=args.reviewer_id,
            decisions=decisions,
            batch_review=batch_review,
            approved_at=_timestamp(args.at),
        )
        print(
            f"human review complete for {package['batch_id']}; "
            f"published plays={package['published_play_count']}; no public post performed"
        )
    elif args.command == "settle":
        result = workflow.settle(args.run_id, _json_file(args.input))
        print(f"appended settlement event {result['settlement_event_id']}")


if __name__ == "__main__":
    main()
