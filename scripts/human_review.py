from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.human_review import HumanReviewDecision, HumanReviewRepository  # noqa: E402


def _repository(database_path: Path, artifact_root: Path) -> HumanReviewRepository:
    return HumanReviewRepository(
        Database(database_path),
        artifact_root=artifact_root,
        secret_values=settings.credential_values(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and record the explicit Daily MLB Human Review boundary")
    parser.add_argument("--database", type=Path, default=settings.database_path)
    parser.add_argument("--artifact-root", type=Path, default=settings.artifact_dir)
    subcommands = parser.add_subparsers(dest="command", required=True)
    show = subcommands.add_parser("show-target")
    show.add_argument("--run-id", required=True)
    for name in ("approve", "reject"):
        command = subcommands.add_parser(name)
        command.add_argument("--run-id", required=True)
        command.add_argument("--reviewer", required=True)
        command.add_argument("--notes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repository = _repository(args.database, args.artifact_root)
    if args.command == "show-target":
        payload = {
            **repository.show_target(args.run_id).identity_dict(),
            "instruction": "approve or reject this exact checksum-bound target",
        }
    else:
        review = repository.record_decision(
            run_id=args.run_id,
            reviewer_id=args.reviewer,
            decision=HumanReviewDecision(args.command),
            notes=args.notes,
        )
        payload = {
            "decision": review.record.decision.value,
            "review_checksum": review.record.checksum,
            "review_id": review.review_id,
            "reviewed_at": review.record.reviewed_at.isoformat(),
            "target": review.record.target.identity_dict(),
        }
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
