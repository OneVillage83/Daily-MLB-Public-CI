from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

ROOT: Final = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPRINT_FOCUSED_TESTS: Final = (
    "tests/test_pre_model_migration_v12.py",
    "tests/test_pre_model_pipeline.py",
    "tests/test_data_quality_contracts.py",
    "tests/test_data_quality_engine.py",
    "tests/test_data_quality_rules.py",
    "tests/test_matchup_packet.py",
    "tests/test_matchup_packet_artifact.py",
    "tests/test_model_feature_set.py",
    "tests/test_model_feature_set_artifact.py",
    "tests/test_model_feature_set_identity.py",
    "tests/test_model_feature_set_v3_integration.py",
    "tests/test_odds_weather_handler.py",
    "tests/test_run_controller_odds_weather.py",
    "tests/test_odds_weather_attempt_manifest.py",
    "tests/test_odds_weather_selector.py",
    "tests/test_odds_weather_repository.py",
    "tests/test_odds_weather_repository_integrity.py",
    "tests/test_odds_weather_credential_boundary.py",
    "tests/test_odds_weather_migration_v11.py",
    "tests/test_baseball_intelligence_migration_v10.py",
    "tests/test_game_state_migration_v9.py",
    "tests/test_migrations.py",
    "tests/test_odds_weather_assembly.py",
    "tests/test_run_controller_baseball_intelligence.py",
    "tests/test_run_controller_service.py",
    "tests/test_run_controller_persistence.py",
    "tests/test_run_controller_cli.py",
)
SPRINT_STATS_TESTS: Final = (
    "tests/stats/test_feature_materialization.py",
    "tests/stats/test_stats_features.py",
    "tests/stats/test_migration_v5.py",
    "tests/stats/test_fielding_grain_v6.py",
)


@dataclass(frozen=True, slots=True)
class Gate:
    label: str
    command: tuple[str, ...]


def _python_module(python: str, module: str, *arguments: str) -> tuple[str, ...]:
    return (python, "-m", module, *arguments)


def build_gates(
    profile: str,
    *,
    python: str,
    stats_python: str,
    tests: Sequence[str],
    python_targets: Sequence[str],
) -> tuple[Gate, ...]:
    if profile == "task":
        if not tests or not python_targets:
            raise ValueError("task requires at least one --test and --python-target")
        return (
            Gate("focused tests", _python_module(python, "pytest", "-q", *tests)),
            Gate("Ruff affected surface", _python_module(python, "ruff", "check", *python_targets)),
            Gate(
                "mypy affected surface",
                _python_module(python, "mypy", "--no-incremental", *python_targets),
            ),
        )
    if profile == "sprint":
        focused = tuple(tests) or SPRINT_FOCUSED_TESTS
        return (
            Gate("sprint focused tests", _python_module(python, "pytest", "-q", *focused)),
            Gate("development full suite", _python_module(python, "pytest", "-q")),
            Gate(
                "focused stats regression",
                _python_module(stats_python, "pytest", "-q", *SPRINT_STATS_TESTS),
            ),
            Gate("Ruff full repository", _python_module(python, "ruff", "check", ".")),
            Gate("mypy full repository", _python_module(python, "mypy", "--no-incremental")),
            Gate(
                "repository secret scan",
                (python, "scripts/scan_repository_secrets.py"),
            ),
        )
    if profile == "release":
        return (
            Gate("development full suite", _python_module(python, "pytest", "-q")),
            Gate("stats full suite", _python_module(stats_python, "pytest", "-q")),
            Gate("stats-only suite", _python_module(stats_python, "pytest", "-q", "tests/stats")),
            Gate("Ruff full repository", _python_module(python, "ruff", "check", ".")),
            Gate("mypy full repository", _python_module(python, "mypy", "--no-incremental")),
            Gate("development dependency consistency", _python_module(python, "pip", "check")),
            Gate("stats dependency consistency", _python_module(stats_python, "pip", "check")),
            Gate(
                "development locked audit",
                _python_module(
                    python,
                    "pip_audit",
                    "--require-hashes",
                    "-r",
                    "requirements.txt",
                    "--progress-spinner",
                    "off",
                ),
            ),
            Gate(
                "stats locked audit",
                _python_module(
                    stats_python,
                    "pip_audit",
                    "--require-hashes",
                    "-r",
                    "requirements-stats.txt",
                    "--progress-spinner",
                    "off",
                ),
            ),
            Gate(
                "development hash-locked dry run",
                _python_module(
                    python,
                    "pip",
                    "install",
                    "--dry-run",
                    "--ignore-installed",
                    "--require-hashes",
                    "-r",
                    "requirements-dev.txt",
                ),
            ),
            Gate(
                "stats hash-locked dry run",
                _python_module(
                    stats_python,
                    "pip",
                    "install",
                    "--dry-run",
                    "--ignore-installed",
                    "--require-hashes",
                    "-r",
                    "requirements-stats.txt",
                ),
            ),
            Gate(
                "offline pybaseball compatibility",
                (stats_python, "scripts/check_pybaseball_compatibility.py"),
            ),
            Gate("repository secret scan", (python, "scripts/scan_repository_secrets.py")),
            Gate("Docker release verification", ("bash", "scripts/verify_docker_release.sh")),
        )
    raise ValueError(f"unsupported validation profile: {profile}")


def _display(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def _sqlite_gate() -> None:
    from app.database import Database
    from app.migrations import CURRENT_SCHEMA_VERSION

    with tempfile.TemporaryDirectory(prefix="daily-mlb-checkpoint-") as directory:
        path = Path(directory) / "validation.sqlite3"
        Database(path)
        connection = sqlite3.connect(path)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
    if version != CURRENT_SCHEMA_VERSION or integrity != "ok" or foreign_keys:
        raise RuntimeError("fresh SQLite validation failed")
    print(f"PASS sqlite user_version={version} integrity_check=ok foreign_key_check=0")


def _generated_evidence_gate(root: Path) -> None:
    from app.config import settings

    resolved = root.resolve(strict=True)
    secrets = tuple(item.encode("utf-8") for item in settings.credential_values() if item)
    checked = 0
    for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
        payload = path.read_bytes()
        checked += 1
        if any(secret in payload for secret in secrets):
            raise RuntimeError("generated evidence contains a configured secret value")
    print(f"PASS generated-evidence configured-secret scan files={checked}")


def _skips(profile: str) -> tuple[str, ...]:
    if profile == "task":
        return (
            "full development and stats suites",
            "dependency audits and hash-locked install rehearsals",
            "Docker and public workflows",
            "migration and release failure-injection matrices",
        )
    if profile == "sprint":
        return (
            "complete local stats-environment suite (shared stats code and dependencies unchanged)",
            "dependency audits and hash-locked install rehearsals (dependencies unchanged)",
            "local Docker build (Docker/runtime files unchanged; exact-head public CI supplies proof)",
            "complete release migration failure-injection matrix (focused v12 fresh/upgrade and rollback gates run in sprint)",
        )
    return ()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one explicit Daily MLB validation profile")
    parser.add_argument("profile", choices=("task", "sprint", "release"))
    parser.add_argument("--test", action="append", default=[], help="focused pytest path; repeatable")
    parser.add_argument(
        "--python-target",
        action="append",
        default=[],
        help="affected Python file/package for task Ruff and mypy; repeatable",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--stats-python", default=sys.executable)
    parser.add_argument("--generated-evidence-root", type=Path)
    parser.add_argument("--explain", "--dry-run", action="store_true", dest="explain")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        gates = build_gates(
            args.profile,
            python=args.python,
            stats_python=args.stats_python,
            tests=args.test,
            python_targets=args.python_target,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"validation profile: {args.profile}")
    for gate in gates:
        print(f"REQUIRED {gate.label}: {_display(gate.command)}")
    if args.profile in {"sprint", "release"}:
        print("REQUIRED SQLite: fresh schema, integrity_check, foreign_key_check")
    if args.generated_evidence_root is not None:
        print(f"REQUIRED generated-evidence scan: {args.generated_evidence_root}")
    for reason in _skips(args.profile):
        print(f"SKIPPED: {reason}")
    if args.explain:
        return 0

    os.chdir(ROOT)
    for gate in gates:
        print(f"RUN {gate.label}: {_display(gate.command)}", flush=True)
        completed = subprocess.run(gate.command, cwd=ROOT, check=False)
        if completed.returncode:
            print(f"FAILED {gate.label}: exit={completed.returncode}", file=sys.stderr)
            return completed.returncode
    if args.profile in {"sprint", "release"}:
        try:
            _sqlite_gate()
        except Exception as exc:
            print(f"FAILED SQLite: {type(exc).__name__}", file=sys.stderr)
            return 1
    if args.generated_evidence_root is not None:
        try:
            _generated_evidence_gate(args.generated_evidence_root)
        except Exception as exc:
            print(f"FAILED generated-evidence scan: {type(exc).__name__}", file=sys.stderr)
            return 1
    print(f"PASS validation profile: {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
