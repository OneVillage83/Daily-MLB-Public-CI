from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from app.identifiers import parse_requested_date  # noqa: E402
from app.redaction import redact_text  # noqa: E402
from app.stats.feature_materialization import (  # noqa: E402
    FeatureMaterializationError,
    FeatureMaterializationRequest,
    FeatureMaterializationService,
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _write_report(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stats_feature_materialization",
        description=(
            "Materialize Daily MLB V3 player features from retained database "
            "evidence without provider or Statcast revision writes"
        ),
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--feature-as-of", required=True)
    parser.add_argument("--source-stats-run-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = FeatureMaterializationRequest(
            feature_as_of=parse_requested_date(args.feature_as_of),
            source_stats_run_id=args.source_stats_run_id,
            dry_run=args.dry_run,
        )
        service = FeatureMaterializationService(
            database=Database(args.database),
            report_path=args.report,
        )
        result = service.materialize(request)
        payload = result.as_dict()
        if not args.dry_run:
            _write_report(args.report, payload)
        print(_canonical_json(payload), flush=True)
        return 0
    except Exception as exc:
        message = redact_text(str(exc))
        failure_payload: dict[str, object] = {
            "status": "failed",
            "error_type": type(exc).__name__,
            "message": message,
            "network_requests": 0,
            "statcast_revision_writes": 0,
        }
        if not isinstance(exc, (FeatureMaterializationError, ValueError, TypeError)):
            failure_payload["message"] = "unexpected feature materialization failure"
        print(_canonical_json(failure_payload), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
