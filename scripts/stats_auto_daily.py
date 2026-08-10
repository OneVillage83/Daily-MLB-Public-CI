from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import NoReturn, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.database import Database  # noqa: E402
from app.identifiers import parse_requested_date  # noqa: E402
from app.redaction import redact_text  # noqa: E402
from app.stats.acquisition import (  # noqa: E402
    AcquisitionCommand,
    AcquisitionConfigurationError,
    AcquisitionExecutionError,
    AcquisitionMode,
    AcquisitionRequest,
    AcquisitionResult,
    AcquisitionResumeError,
    StatsAcquisitionService,
    build_stats_transport,
    canonical_result_json,
    default_stats_user_agent,
)
from app.stats.auto_daily import (  # noqa: E402
    AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION,
    AutoDailyStatsCoordinator,
    AutoDailyStatsGapError,
    plan_auto_daily_stats_gap,
)
from app.stats.raw_store import RawArtifactStore  # noqa: E402
from app.stats.repository import StatsRepository  # noqa: E402


EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_PARTIAL_OR_INCOMPLETE = 2
EXIT_USAGE = 64


class CliUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="stats_auto_daily",
        description=(
            "Run Daily MLB statistics acquisition with automatic current-season "
            "gap detection and repair before the requested day"
        ),
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in AcquisitionMode),
        default=AcquisitionMode.LIVE.value,
    )
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(canonical_result_json(payload) + "\n")
    stream.flush()


def _validate_args(args: argparse.Namespace) -> tuple[date, AcquisitionMode]:
    target_date = parse_requested_date(args.date)
    mode = AcquisitionMode(args.mode)
    if mode is AcquisitionMode.LIVE and args.fixture_root is not None:
        raise AcquisitionConfigurationError(
            "--fixture-root is valid only with --mode offline"
        )
    if mode is AcquisitionMode.OFFLINE and args.fixture_root is None and not args.dry_run:
        raise AcquisitionConfigurationError(
            "offline automatic daily acquisition requires --fixture-root"
        )
    return target_date, mode


def _run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    target_date, mode = _validate_args(args)
    database = Database(args.database)
    repository = StatsRepository(database)
    initial_plan = plan_auto_daily_stats_gap(repository, target_date)
    if args.dry_run:
        return EXIT_SUCCESS, {
            "contract_version": AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION,
            "status": "dry_run",
            "plan": initial_plan.as_dict(),
            "plan_checksum": initial_plan.checksum,
            "exit_code": EXIT_SUCCESS,
        }

    raw_store = RawArtifactStore(args.raw_root)

    def run_command(
        command: AcquisitionCommand,
        requested_date: date,
    ) -> AcquisitionResult:
        # Use a fresh transport/service per retained acquisition run so request,
        # retry, capture, and reconciliation counts remain scoped to that run.
        transport = build_stats_transport(
            mode=mode,
            raw_store=raw_store,
            fixture_root=args.fixture_root,
            user_agent=default_stats_user_agent(),
        )
        service = StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=transport,
            report_path=args.report,
        )
        return service.execute(
            command,
            AcquisitionRequest(
                requested_through_date=requested_date,
                mode=mode,
            ),
        )

    coordinator = AutoDailyStatsCoordinator(
        repository=repository,
        run_daily=lambda requested_date: run_command(
            AcquisitionCommand.DAILY,
            requested_date,
        ),
        run_baseline_backfill=lambda requested_date: run_command(
            AcquisitionCommand.BACKFILL_CURRENT,
            requested_date,
        ),
    )
    execution = coordinator.execute(target_date)
    payload = execution.as_dict()
    exit_code = execution.daily_result.exit_code
    payload["status"] = execution.daily_result.status
    payload["exit_code"] = exit_code
    return exit_code, payload


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
    except (CliUsageError, AcquisitionConfigurationError, ValueError) as exc:
        payload = {
            "status": "usage_error",
            "exit_code": EXIT_USAGE,
            "error_type": type(exc).__name__,
            "message": redact_text(str(exc)),
        }
        _emit(stderr, payload)
        return EXIT_USAGE
    except (
        AutoDailyStatsGapError,
        AcquisitionExecutionError,
        AcquisitionResumeError,
    ) as exc:
        payload = {
            "status": "failed",
            "exit_code": EXIT_OPERATIONAL_FAILURE,
            "error_type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "run_id": getattr(exc, "run_id", None),
            "stats_run_id": getattr(exc, "stats_run_id", None),
        }
        _emit(stderr, payload)
        return EXIT_OPERATIONAL_FAILURE
    except Exception as exc:
        payload = {
            "status": "failed",
            "exit_code": EXIT_OPERATIONAL_FAILURE,
            "error_type": type(exc).__name__,
            "message": redact_text(str(exc)),
        }
        _emit(stderr, payload)
        return EXIT_OPERATIONAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
