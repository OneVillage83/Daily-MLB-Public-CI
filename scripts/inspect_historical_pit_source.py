from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import NoReturn, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.predictions.historical_pit_capabilities import (  # noqa: E402
    HistoricalPITCapabilityError,
    inventory_historical_pit_reconstruction_capabilities,
)
from app.predictions.historical_pit_field_coverage import (  # noqa: E402
    HistoricalPITFieldCoverageError,
    inventory_historical_pit_field_coverage,
)
from app.predictions.historical_pit_sources import (  # noqa: E402
    HistoricalPITSourceError,
    inventory_historical_pit_source,
)
from app.redaction import redact_text  # noqa: E402

EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_USAGE = 64


class CliUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="inspect_historical_pit_source",
        description=(
            "Inventory retained MLB historical stats evidence through a strictly "
            "read-only SQLite connection; no application migrations are executed"
        ),
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--capabilities",
        action="store_true",
        help=(
            "inspect historical team/player/lineup/play coverage needed for "
            "point-in-time feature reconstruction"
        ),
    )
    mode.add_argument(
        "--field-coverage",
        action="store_true",
        help=(
            "inventory deterministic nested Retrosheet team/player field coverage "
            "without emitting retained source values"
        ),
    )
    return parser


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(_canonical_json(payload) + "\n")
    stream.flush()


def _run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    if args.field_coverage:
        field_inventory = inventory_historical_pit_field_coverage(
            args.database,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        payload = field_inventory.summary_as_dict()
        mode = "field_coverage"
    elif args.capabilities:
        capability_inventory = inventory_historical_pit_reconstruction_capabilities(
            args.database,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        payload = capability_inventory.summary_as_dict()
        mode = "reconstruction_capabilities"
    else:
        source_inventory = inventory_historical_pit_source(
            args.database,
            start_date=args.start_date,
            end_date=args.end_date,
        )
        payload = source_inventory.summary_as_dict()
        mode = "source_inventory"
    payload.update(
        {
            "mode": mode,
            "source_database": str(Path(args.database).expanduser().resolve()),
            "status": "inventoried",
        }
    )
    return EXIT_SUCCESS, payload


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        args = build_parser().parse_args(argv)
        exit_code, payload = _run(args)
        _emit(stdout, payload)
        return exit_code
    except (
        HistoricalPITSourceError,
        HistoricalPITCapabilityError,
        HistoricalPITFieldCoverageError,
    ) as exc:
        _emit(
            stderr,
            {
                "error_type": type(exc).__name__,
                "exit_code": EXIT_OPERATIONAL_FAILURE,
                "message": redact_text(str(exc)),
                "status": "failed",
            },
        )
        return EXIT_OPERATIONAL_FAILURE
    except (CliUsageError, TypeError, ValueError) as exc:
        _emit(
            stderr,
            {
                "error_type": type(exc).__name__,
                "exit_code": EXIT_USAGE,
                "message": redact_text(str(exc)),
                "status": "usage_error",
            },
        )
        return EXIT_USAGE
    except Exception:
        _emit(
            stderr,
            {
                "error_type": "UnexpectedHistoricalPITInventoryFailure",
                "exit_code": EXIT_OPERATIONAL_FAILURE,
                "message": "unexpected historical PIT source inventory failure",
                "status": "failed",
            },
        )
        return EXIT_OPERATIONAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
