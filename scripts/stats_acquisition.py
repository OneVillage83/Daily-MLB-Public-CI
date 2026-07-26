from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
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
    AcquisitionResumeError,
    PYBASEBALL_REQUIRED_VERSION,
    STATS_ACQUISITION_VERSION,
    StatsAcquisitionService,
    build_source_run_replay_transport,
    build_stats_transport,
    canonical_result_json,
    default_stats_user_agent,
    stats_source_contract_version,
    stats_provider_inventory,
)
from app.stats.contracts import StatsTransport  # noqa: E402
from app.stats.raw_store import RawArtifactStore  # noqa: E402
from app.stats.transport import FixtureStatsTransport  # noqa: E402


EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_PARTIAL_OR_INCOMPLETE = 2
EXIT_USAGE = 64


class CliUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliUsageError(message)


def _add_shared_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--raw-root",
        type=Path,
        required=True,
    )
    parser.add_argument(
        "--report",
        type=Path,
        required=True,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode",
        choices=tuple(item.value for item in AcquisitionMode),
        default=AcquisitionMode.LIVE.value,
    )
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--resume-run-id")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="stats_acquisition",
        description="Controlled Daily MLB statistics acquisition and validation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    bootstrap = subparsers.add_parser(AcquisitionCommand.BOOTSTRAP_RETROSHEET.value)
    _add_shared_flags(bootstrap)
    bootstrap.add_argument("--through-season", type=int, required=True)

    backfill = subparsers.add_parser(AcquisitionCommand.BACKFILL_CURRENT.value)
    _add_shared_flags(backfill)
    backfill.add_argument("--season", type=int, required=True)
    backfill.add_argument("--through", required=True)
    backfill.add_argument("--source-stats-run-id")

    daily = subparsers.add_parser(AcquisitionCommand.DAILY.value)
    _add_shared_flags(daily)
    daily.add_argument("--date", required=True)

    reconcile_register = subparsers.add_parser(
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN.value
    )
    _add_shared_flags(reconcile_register)
    reconcile_register.add_argument("--through-season", type=int, required=True)
    reconcile_register.add_argument("--source-stats-run-id", required=True)

    validate = subparsers.add_parser(AcquisitionCommand.VALIDATE.value)
    _add_shared_flags(validate)
    validate.add_argument("--season", type=int, required=True)
    validate.add_argument("--through", required=True)
    return parser


def _emit(stream: TextIO, payload: dict[str, object]) -> None:
    stream.write(canonical_result_json(payload) + "\n")
    stream.flush()


def _season(value: int, flag: str) -> int:
    if value < 1876 or value > 9999:
        raise CliUsageError(f"{flag} must be a valid MLB season year")
    return value


def _requested_date(args: argparse.Namespace, command: AcquisitionCommand) -> date:
    if command in {
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
    }:
        return date(_season(args.through_season, "--through-season"), 12, 31)
    if command is AcquisitionCommand.DAILY:
        return parse_requested_date(args.date)
    requested = parse_requested_date(args.through)
    season = _season(args.season, "--season")
    if requested.year != season:
        raise CliUsageError("--through must fall within --season")
    return requested


def _dry_run_plan(
    command: AcquisitionCommand,
    requested_date: date,
    *,
    source_stats_run_id: str | None = None,
) -> tuple[int, dict[str, object]]:
    return EXIT_SUCCESS, {
        "acquisition_version": STATS_ACQUISITION_VERSION,
        "acquisition_provenance": {
            "canonical_transport": "project_controlled_exact_raw_bytes",
            "canonical_adapter_version": STATS_ACQUISITION_VERSION,
            "source_contract_version": stats_source_contract_version(
                command, requested_date.year
            ),
            "pybaseball_version": PYBASEBALL_REQUIRED_VERSION,
            "pybaseball_role": "noncanonical_parity_reference",
            "providers": stats_provider_inventory(
                requested_date.year,
                historical_through_season=(
                    requested_date.year
                    if command
                    in {
                        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
                        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
                    }
                    else None
                ),
            ),
        },
        "command": command.value,
        "status": "dry_run",
        "requested_through_date": requested_date.isoformat(),
        "run_id": None,
        "stats_run_id": None,
        "source_stats_run_id": source_stats_run_id,
        "counts": {"network_requests": 0, "persistent_writes": 0},
        "completeness": None,
        "warnings": [],
        "report_path": None,
        "dry_run": True,
        "exit_code": EXIT_SUCCESS,
    }


def _read_only_database_backup(source_path: Path, destination_path: Path) -> None:
    if source_path.is_symlink():
        raise AcquisitionConfigurationError(
            "validation dry-run requires an existing regular --database file"
        )
    source = source_path.resolve()
    if not source.is_file():
        raise AcquisitionConfigurationError(
            "validation dry-run requires an existing regular --database file"
        )
    source_uri = f"{source.as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
        with closing(sqlite3.connect(destination_path)) as destination_connection:
            source_connection.backup(destination_connection)


def _read_only_raw_store(root: Path) -> RawArtifactStore:
    if root.is_symlink():
        raise AcquisitionConfigurationError(
            "validation dry-run requires an existing regular --raw-root directory"
        )
    resolved = root.resolve()
    if not resolved.is_dir():
        raise AcquisitionConfigurationError(
            "validation dry-run requires an existing regular --raw-root directory"
        )
    store = RawArtifactStore.__new__(RawArtifactStore)
    store.root = resolved
    return store


def _with_exit_contract(
    command: AcquisitionCommand,
    result_payload: dict[str, object],
    default_exit_code: int,
) -> tuple[int, dict[str, object]]:
    exit_code = default_exit_code
    outcome = result_payload.get("outcome")
    if outcome == "partial":
        exit_code = EXIT_PARTIAL_OR_INCOMPLETE
        raw_warnings = result_payload.get("warnings")
        warnings = (
            [str(item) for item in raw_warnings]
            if isinstance(raw_warnings, list)
            else []
        )
        warning = "regular_season_completeness_not_satisfied"
        if warning not in warnings:
            warnings.append(warning)
        result_payload["warnings"] = sorted(warnings)
        result_payload["status"] = "completed_with_warnings"
    elif outcome == "failed":
        exit_code = EXIT_OPERATIONAL_FAILURE
    result_payload["exit_code"] = exit_code
    return exit_code, result_payload


def _validate_dry_run(
    args: argparse.Namespace,
    requested_date: date,
) -> tuple[int, dict[str, object]]:
    with tempfile.TemporaryDirectory(prefix="dse-stats-validation-dry-run-") as name:
        temporary_root = Path(name)
        database_path = temporary_root / "stats.db"
        _read_only_database_backup(args.database, database_path)
        raw_store = _read_only_raw_store(args.raw_root)
        database = Database(database_path)
        service = StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=FixtureStatsTransport(raw_store, {}),
            report_path=temporary_root / "report.json",
        )
        result = service.validate(
            AcquisitionRequest(
                requested_through_date=requested_date,
                mode=AcquisitionMode(args.mode),
                resume_run_id=args.resume_run_id,
                dry_run=True,
            )
        )
        payload = result.as_dict()
        payload["report_path"] = None
        return _with_exit_contract(
            AcquisitionCommand.VALIDATE, payload, result.exit_code
        )


def _run(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    command = AcquisitionCommand(args.command)
    mode = AcquisitionMode(args.mode)
    requested_date = _requested_date(args, command)
    source_stats_run_id = getattr(args, "source_stats_run_id", None)
    AcquisitionRequest(
        requested_through_date=requested_date,
        mode=mode,
        resume_run_id=args.resume_run_id,
        source_stats_run_id=source_stats_run_id,
        dry_run=args.dry_run,
    )
    if mode is AcquisitionMode.LIVE and args.fixture_root is not None:
        raise AcquisitionConfigurationError(
            "--fixture-root is valid only with --mode offline"
        )
    is_source_replay = (
        command is AcquisitionCommand.BACKFILL_CURRENT
        and source_stats_run_id is not None
    )
    if is_source_replay and mode is not AcquisitionMode.OFFLINE:
        raise AcquisitionConfigurationError(
            "backfill source-run replay requires --mode offline"
        )
    if is_source_replay and args.fixture_root is not None:
        raise AcquisitionConfigurationError(
            "source-run replay cannot also use --fixture-root"
        )
    if args.dry_run:
        if command is AcquisitionCommand.VALIDATE:
            return _validate_dry_run(args, requested_date)
        return _dry_run_plan(
            command,
            requested_date,
            source_stats_run_id=source_stats_run_id,
        )
    if (
        command is not AcquisitionCommand.VALIDATE
        and mode is AcquisitionMode.OFFLINE
        and not is_source_replay
    ):
        if args.fixture_root is None:
            raise AcquisitionConfigurationError(
                "offline provider commands require --fixture-root"
            )

    database = Database(args.database)
    raw_store = RawArtifactStore(args.raw_root)
    transport: StatsTransport
    if command is AcquisitionCommand.VALIDATE:
        transport = FixtureStatsTransport(raw_store, {})
    elif is_source_replay:
        if source_stats_run_id is None:
            raise AssertionError("source replay identity disappeared")
        transport = build_source_run_replay_transport(
            database=database,
            raw_store=raw_store,
            source_stats_run_id=source_stats_run_id,
            requested_through_date=requested_date,
        )
    else:
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
    request = AcquisitionRequest(
        requested_through_date=requested_date,
        mode=mode,
        resume_run_id=args.resume_run_id,
        source_stats_run_id=source_stats_run_id,
        dry_run=False,
    )
    result = service.execute(command, request)
    return _with_exit_contract(command, result.as_dict(), result.exit_code)


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
    except (AcquisitionExecutionError, AcquisitionResumeError) as exc:
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
