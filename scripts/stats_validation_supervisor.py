from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NoReturn, Sequence, TextIO

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.identifiers import parse_requested_date  # noqa: E402
from app.redaction import redact_text  # noqa: E402


SUPERVISOR_VERSION = "DSE_STATS_VALIDATION_SUPERVISOR_V1"
EXIT_SUCCESS = 0
EXIT_OPERATIONAL_FAILURE = 1
EXIT_PARTIAL_OR_INCOMPLETE = 2
EXIT_USAGE = 64
_RUN_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class SupervisorUsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise SupervisorUsageError(message)


@dataclass(frozen=True, slots=True)
class StagePlan:
    name: str
    command: tuple[str, ...]
    stdout_path: Path
    stderr_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": list(self.command),
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    command: tuple[str, ...]
    exit_code: int
    started_at: str
    completed_at: str
    stdout_path: Path
    stderr_path: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
        }


@dataclass(frozen=True, slots=True)
class SupervisorPlan:
    requested_through_date: date
    acquisition_command: str
    mode: str
    database: Path
    raw_root: Path
    run_directory: Path
    supervisor_report: Path
    stages: tuple[StagePlan, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": SUPERVISOR_VERSION,
            "status": "plan",
            "requested_through_date": self.requested_through_date.isoformat(),
            "acquisition_command": self.acquisition_command,
            "mode": self.mode,
            "database": str(self.database),
            "raw_root": str(self.raw_root),
            "run_directory": str(self.run_directory),
            "supervisor_report": str(self.supervisor_report),
            "stages": [stage.as_dict() for stage in self.stages],
            "network_requests_executed": 0,
            "exit_code": EXIT_SUCCESS,
        }


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp_label(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _validate_run_label(value: str) -> str:
    if not _RUN_LABEL_RE.fullmatch(value):
        raise SupervisorUsageError(
            "--run-label must use 1-80 letters, numbers, periods, underscores, or hyphens"
        )
    return value


def _stage(
    run_directory: Path,
    name: str,
    command: Sequence[str],
) -> StagePlan:
    return StagePlan(
        name=name,
        command=tuple(command),
        stdout_path=run_directory / f"{name}.stdout.log",
        stderr_path=run_directory / f"{name}.stderr.log",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="stats_validation_supervisor",
        description=(
            "Run a controlled current-season stats acquisition, formal validation, "
            "SQLite checks, and retained-evidence secret scan"
        ),
    )
    parser.add_argument("--through", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--acquisition-command",
        choices=("backfill-current", "daily"),
        default="backfill-current",
    )
    parser.add_argument("--mode", choices=("live", "offline"), default="live")
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--source-stats-run-id")
    parser.add_argument("--resume-run-id")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--run-label")
    parser.add_argument("--plan-only", action="store_true")
    return parser


def _validated_arguments(args: argparse.Namespace) -> tuple[date, str, str]:
    requested_date = parse_requested_date(args.through)
    acquisition_command = str(args.acquisition_command)
    mode = str(args.mode)
    if args.source_stats_run_id:
        if acquisition_command != "backfill-current":
            raise SupervisorUsageError(
                "--source-stats-run-id is supported only with backfill-current"
            )
        if args.fixture_root is not None:
            raise SupervisorUsageError(
                "--source-stats-run-id cannot be combined with --fixture-root"
            )
        mode = "offline"
    elif mode == "offline" and args.fixture_root is None:
        raise SupervisorUsageError(
            "offline acquisition requires --fixture-root or --source-stats-run-id"
        )
    elif mode == "live" and args.fixture_root is not None:
        raise SupervisorUsageError("--fixture-root is valid only with --mode offline")
    return requested_date, acquisition_command, mode


def build_plan(
    args: argparse.Namespace,
    *,
    python_executable: Path | None = None,
    now: datetime | None = None,
) -> SupervisorPlan:
    requested_date, acquisition_command, mode = _validated_arguments(args)
    executable = (python_executable or Path(sys.executable)).resolve()
    label = (
        _validate_run_label(args.run_label)
        if args.run_label
        else f"{requested_date.isoformat()}-{_timestamp_label(now or _utc_now())}"
    )
    database = args.database.resolve()
    raw_root = args.raw_root.resolve()
    output_root = args.output_root.resolve()
    run_directory = output_root / label
    acquisition_report = run_directory / "acquisition-report.json"
    validation_report = run_directory / "validation-report.json"
    supervisor_report = run_directory / "supervisor-report.json"

    acquisition_args: list[str]
    if acquisition_command == "daily":
        acquisition_args = ["daily", "--date", requested_date.isoformat()]
    else:
        acquisition_args = [
            "backfill-current",
            "--season",
            str(requested_date.year),
            "--through",
            requested_date.isoformat(),
        ]
    acquisition_args.extend(
        [
            "--database",
            str(database),
            "--raw-root",
            str(raw_root),
            "--report",
            str(acquisition_report),
            "--mode",
            mode,
        ]
    )
    if args.fixture_root is not None:
        acquisition_args.extend(["--fixture-root", str(args.fixture_root.resolve())])
    if args.source_stats_run_id:
        acquisition_args.extend(
            ["--source-stats-run-id", str(args.source_stats_run_id)]
        )
    if args.resume_run_id:
        acquisition_args.extend(["--resume-run-id", str(args.resume_run_id)])

    stages = [
        _stage(
            run_directory,
            "acquisition",
            (
                str(executable),
                str(ROOT / "scripts" / "stats_acquisition.py"),
                *acquisition_args,
            ),
        ),
        _stage(
            run_directory,
            "validation",
            (
                str(executable),
                str(ROOT / "scripts" / "stats_acquisition.py"),
                "validate",
                "--season",
                str(requested_date.year),
                "--through",
                requested_date.isoformat(),
                "--database",
                str(database),
                "--raw-root",
                str(raw_root),
                "--report",
                str(validation_report),
                "--mode",
                "offline",
            ),
        ),
        _stage(
            run_directory,
            "sqlite-check",
            (
                str(executable),
                str(ROOT / "scripts" / "initialize_database.py"),
                "--database",
                str(database),
                "--check",
            ),
        ),
    ]
    scan_command = [
        str(executable),
        str(ROOT / "scripts" / "scan_stats_evidence.py"),
        "--root",
        str(raw_root),
        "--root",
        str(run_directory),
    ]
    if args.env_file is not None:
        scan_command.extend(["--env-file", str(args.env_file.resolve())])
    stages.append(_stage(run_directory, "evidence-scan", scan_command))
    return SupervisorPlan(
        requested_through_date=requested_date,
        acquisition_command=acquisition_command,
        mode=mode,
        database=database,
        raw_root=raw_root,
        run_directory=run_directory,
        supervisor_report=supervisor_report,
        stages=tuple(stages),
    )


def _validate_execution_paths(plan: SupervisorPlan) -> None:
    if plan.database.is_symlink() or not plan.database.is_file():
        raise SupervisorUsageError(
            "--database must identify an existing regular SQLite file"
        )
    if plan.raw_root.is_symlink():
        raise SupervisorUsageError("--raw-root must not be a symlink")
    plan.raw_root.mkdir(parents=True, exist_ok=True)
    if not plan.raw_root.is_dir():
        raise SupervisorUsageError("--raw-root must identify a directory")
    if plan.run_directory.exists():
        raise SupervisorUsageError("supervisor run directory already exists")
    plan.run_directory.mkdir(parents=True, exist_ok=False)


def _run_stage(stage: StagePlan) -> StageResult:
    started = _utc_now()
    completed_process = subprocess.run(
        stage.command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    completed = _utc_now()
    stage.stdout_path.write_text(completed_process.stdout, encoding="utf-8")
    stage.stderr_path.write_text(completed_process.stderr, encoding="utf-8")
    return StageResult(
        name=stage.name,
        command=stage.command,
        exit_code=completed_process.returncode,
        started_at=started.isoformat(),
        completed_at=completed.isoformat(),
        stdout_path=stage.stdout_path,
        stderr_path=stage.stderr_path,
    )


def _overall_exit_code(results: Sequence[StageResult]) -> int:
    by_name = {item.name: item.exit_code for item in results}
    acquisition = by_name.get("acquisition")
    if acquisition == EXIT_USAGE:
        return EXIT_USAGE
    for stage_name in ("sqlite-check", "evidence-scan"):
        if by_name.get(stage_name) != EXIT_SUCCESS:
            return EXIT_OPERATIONAL_FAILURE
    validation = by_name.get("validation")
    if acquisition not in {EXIT_SUCCESS, EXIT_PARTIAL_OR_INCOMPLETE}:
        return EXIT_OPERATIONAL_FAILURE
    if validation not in {EXIT_SUCCESS, EXIT_PARTIAL_OR_INCOMPLETE}:
        return EXIT_OPERATIONAL_FAILURE
    if EXIT_PARTIAL_OR_INCOMPLETE in {acquisition, validation}:
        return EXIT_PARTIAL_OR_INCOMPLETE
    return EXIT_SUCCESS


def _status_for_exit(exit_code: int) -> str:
    if exit_code == EXIT_SUCCESS:
        return "completed"
    if exit_code == EXIT_PARTIAL_OR_INCOMPLETE:
        return "partial"
    if exit_code == EXIT_USAGE:
        return "usage_error"
    return "failed"


def execute_plan(plan: SupervisorPlan) -> tuple[int, dict[str, object]]:
    _validate_execution_paths(plan)
    results: list[StageResult] = []
    for stage in plan.stages:
        if stage.name == "validation" and results and results[0].exit_code == EXIT_USAGE:
            continue
        results.append(_run_stage(stage))
    exit_code = _overall_exit_code(results)
    payload: dict[str, object] = {
        "contract_version": SUPERVISOR_VERSION,
        "status": _status_for_exit(exit_code),
        "requested_through_date": plan.requested_through_date.isoformat(),
        "acquisition_command": plan.acquisition_command,
        "mode": plan.mode,
        "database": str(plan.database),
        "raw_root": str(plan.raw_root),
        "run_directory": str(plan.run_directory),
        "supervisor_report": str(plan.supervisor_report),
        "stages": [item.as_dict() for item in results],
        "exit_code": exit_code,
    }
    plan.supervisor_report.write_text(
        _canonical_json(payload) + "\n",
        encoding="utf-8",
    )
    return exit_code, payload


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    error_output = stderr if stderr is not None else sys.stderr
    try:
        arguments = build_parser().parse_args(argv)
        plan = build_plan(arguments)
        if arguments.plan_only:
            output.write(_canonical_json(plan.as_dict()) + "\n")
            output.flush()
            return EXIT_SUCCESS
        exit_code, payload = execute_plan(plan)
        output.write(_canonical_json(payload) + "\n")
        output.flush()
        return exit_code
    except (SupervisorUsageError, ValueError, OSError) as exc:
        usage_payload: dict[str, object] = {
            "contract_version": SUPERVISOR_VERSION,
            "status": "usage_error",
            "exit_code": EXIT_USAGE,
            "error_type": type(exc).__name__,
            "message": redact_text(str(exc)),
        }
        error_output.write(_canonical_json(usage_payload) + "\n")
        error_output.flush()
        return EXIT_USAGE
    except Exception as exc:
        failure_payload: dict[str, object] = {
            "contract_version": SUPERVISOR_VERSION,
            "status": "failed",
            "exit_code": EXIT_OPERATIONAL_FAILURE,
            "error_type": type(exc).__name__,
            "message": redact_text(str(exc)),
        }
        error_output.write(_canonical_json(failure_payload) + "\n")
        error_output.flush()
        return EXIT_OPERATIONAL_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
