from __future__ import annotations

import hashlib
import io
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionExecutionError,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionRequest,
    AcquisitionResult,
    canonical_result_json,
)
from app.stats.completeness import CompletenessResult
from app.stats.contracts import StatsRequest, StatsResponse, StatsTransportError
from app.stats.raw_store import RawArtifactStore
from scripts import stats_acquisition as cli


def _paths(tmp_path: Path) -> list[str]:
    return [
        "--database",
        str(tmp_path / "stats.db"),
        "--raw-root",
        str(tmp_path / "raw"),
        "--report",
        str(tmp_path / "report.json"),
    ]


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = cli.main(argv, stdout=stdout, stderr=stderr)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("command_args", "expected_command", "expected_date"),
    (
        (
            ["bootstrap-retrosheet", "--through-season", "2025"],
            AcquisitionCommand.BOOTSTRAP_RETROSHEET,
            date(2025, 12, 31),
        ),
        (
            [
                "backfill-current",
                "--season",
                "2026",
                "--through",
                "2026-07-14",
            ],
            AcquisitionCommand.BACKFILL_CURRENT,
            date(2026, 7, 14),
        ),
        (
            ["daily", "--date", "2026-07-14"],
            AcquisitionCommand.DAILY,
            date(2026, 7, 14),
        ),
        (
            [
                "reconcile-player-register-pin",
                "--through-season",
                "2025",
                "--source-stats-run-id",
                "stats_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            ],
            AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
            date(2025, 12, 31),
        ),
        (
            [
                "validate",
                "--season",
                "2026",
                "--through",
                "2026-07-14",
            ],
            AcquisitionCommand.VALIDATE,
            date(2026, 7, 14),
        ),
    ),
)
def test_documented_command_forms_default_to_live(
    tmp_path: Path,
    command_args: list[str],
    expected_command: AcquisitionCommand,
    expected_date: date,
) -> None:
    args = cli.build_parser().parse_args([*command_args, *_paths(tmp_path)])

    assert AcquisitionCommand(args.command) is expected_command
    assert AcquisitionMode(args.mode) is AcquisitionMode.LIVE
    assert cli._requested_date(args, expected_command) == expected_date


def test_collection_dry_run_is_deterministic_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ["daily", "--date", "2026-07-14", *_paths(tmp_path), "--dry-run"]

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("dry-run attempted persistence or transport construction")

    monkeypatch.setattr(cli, "Database", unexpected)
    monkeypatch.setattr(cli, "RawArtifactStore", unexpected)
    monkeypatch.setattr(cli, "build_stats_transport", unexpected)

    first = _invoke(argv)
    second = _invoke(argv)

    assert first == second
    assert first[0] == cli.EXIT_SUCCESS
    assert first[2] == ""
    assert first[1].count("\n") == 1
    payload = json.loads(first[1])
    assert payload["status"] == "dry_run"
    assert payload["requested_through_date"] == "2026-07-14"
    assert payload["counts"] == {"network_requests": 0, "persistent_writes": 0}
    provenance = dict(payload["acquisition_provenance"])
    providers = provenance.pop("providers")
    assert provenance == {
        "canonical_transport": "project_controlled_exact_raw_bytes",
        "canonical_adapter_version": "DSE_MLB_STATS_ACQUISITION_V1",
        "source_contract_version": (
            "baseball-reference-standard-2026+"
            "baseball-savant-statcast-search-csv-2026"
        ),
        "pybaseball_version": "2.2.7",
        "pybaseball_role": "noncanonical_parity_reference",
    }
    assert set(providers) == {
        "retrosheet",
        "baseball_reference",
        "statcast",
        "pybaseball",
    }
    assert providers["pybaseball"]["adapter_version"] == (
        "DSE_PYBASEBALL_REGISTER_ADAPTER_V1"
    )
    assert providers["pybaseball"]["pybaseball_version"] == "2.2.7"
    assert providers["baseball_reference"]["resource_identity"].startswith(
        "https://www.baseball-reference.com/"
    )
    assert "/leagues/daily.fcgi" in str(
        providers["baseball_reference"]["resource_identity"]
    )
    assert "/leagues/daily.cgi" not in str(
        providers["baseball_reference"]["resource_identity"]
    )
    assert first[1] == canonical_result_json(payload) + "\n"
    assert not (tmp_path / "stats.db").exists()
    assert not (tmp_path / "raw").exists()
    assert not (tmp_path / "report.json").exists()


def test_player_register_reconciliation_dry_run_is_offline_safe_and_versioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_stats_run_id = "stats_" + "a" * 32
    argv = [
        "reconcile-player-register-pin",
        "--through-season",
        "2025",
        "--source-stats-run-id",
        source_stats_run_id,
        *_paths(tmp_path),
        "--dry-run",
    ]

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("bridge dry-run attempted persistence or network access")

    monkeypatch.setattr(cli, "Database", unexpected)
    monkeypatch.setattr(cli, "RawArtifactStore", unexpected)
    monkeypatch.setattr(cli, "build_stats_transport", unexpected)

    code, stdout, stderr = _invoke(argv)

    assert code == cli.EXIT_SUCCESS
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["command"] == "reconcile-player-register-pin"
    assert payload["source_stats_run_id"] == source_stats_run_id
    assert payload["acquisition_provenance"]["source_contract_version"] == (
        "DSE_PLAYER_REGISTER_PIN_RECONCILIATION_V1"
    )
    assert payload["acquisition_provenance"]["providers"]["retrosheet"][
        "season_contract"
    ] == "regular-season-through-2025"
    assert payload["counts"] == {"network_requests": 0, "persistent_writes": 0}
    assert not (tmp_path / "stats.db").exists()


def test_backfill_source_replay_dry_run_is_offline_and_network_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_stats_run_id = "stats_" + "b" * 32
    argv = [
        "backfill-current",
        "--season",
        "2026",
        "--through",
        "2026-07-14",
        "--mode",
        "offline",
        "--source-stats-run-id",
        source_stats_run_id,
        *_paths(tmp_path),
        "--dry-run",
    ]

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("replay dry-run attempted persistence or transport")

    monkeypatch.setattr(cli, "Database", unexpected)
    monkeypatch.setattr(cli, "RawArtifactStore", unexpected)
    monkeypatch.setattr(cli, "build_stats_transport", unexpected)
    monkeypatch.setattr(cli, "build_source_run_replay_transport", unexpected)

    code, stdout, stderr = _invoke(argv)

    assert code == cli.EXIT_SUCCESS
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["command"] == "backfill-current"
    assert payload["source_stats_run_id"] == source_stats_run_id
    assert payload["counts"] == {"network_requests": 0, "persistent_writes": 0}
    assert not (tmp_path / "stats.db").exists()


def test_backfill_source_replay_requires_offline_mode(tmp_path: Path) -> None:
    code, stdout, stderr = _invoke(
        [
            "backfill-current",
            "--season",
            "2026",
            "--through",
            "2026-07-14",
            "--source-stats-run-id",
            "stats_" + "c" * 32,
            *_paths(tmp_path),
        ]
    )

    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert "requires --mode offline" in json.loads(stderr)["message"]


def test_validation_dry_run_reads_database_without_mutating_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "stats.db"
    raw_root = tmp_path / "raw"
    report_path = tmp_path / "report.json"
    Database(database_path)
    RawArtifactStore(raw_root)
    before_database = hashlib.sha256(database_path.read_bytes()).hexdigest()
    before_raw = tuple(sorted(path.relative_to(raw_root) for path in raw_root.rglob("*")))

    def unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("validation dry-run attempted provider transport")

    monkeypatch.setattr(cli, "build_stats_transport", unexpected)
    code, stdout, stderr = _invoke(
        [
            "validate",
            "--season",
            "2026",
            "--through",
            "2026-07-14",
            *_paths(tmp_path),
            "--dry-run",
        ]
    )

    assert code == cli.EXIT_OPERATIONAL_FAILURE
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["dry_run"] is True
    assert payload["report_path"] is None
    assert "stats_ingestion_runs" in payload["counts"]
    assert "validation_scope_has_no_ingestion_runs" in payload["warnings"]
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_database
    assert tuple(sorted(path.relative_to(raw_root) for path in raw_root.rglob("*"))) == before_raw
    assert not report_path.exists()


class _ServiceStub:
    def __init__(
        self,
        result: AcquisitionResult | BaseException,
        calls: list[tuple[AcquisitionCommand, AcquisitionRequest]],
    ) -> None:
        self.result = result
        self.calls = calls

    def execute(
        self, command: AcquisitionCommand, request: AcquisitionRequest
    ) -> AcquisitionResult:
        self.calls.append((command, request))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _stub_execution(
    monkeypatch: pytest.MonkeyPatch,
    result: AcquisitionResult | BaseException,
) -> tuple[list[tuple[AcquisitionCommand, AcquisitionRequest]], dict[str, Any]]:
    calls: list[tuple[AcquisitionCommand, AcquisitionRequest]] = []
    transport_args: dict[str, Any] = {}
    monkeypatch.setattr(cli, "Database", lambda path: object())
    monkeypatch.setattr(cli, "RawArtifactStore", lambda path: object())

    def build_transport(**kwargs: object) -> object:
        transport_args.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "build_stats_transport", build_transport)
    monkeypatch.setattr(
        cli,
        "StatsAcquisitionService",
        lambda **kwargs: _ServiceStub(result, calls),
    )
    return calls, transport_args


def _result(
    *, complete: bool, status: str = "completed", warnings: tuple[str, ...] = ()
) -> AcquisitionResult:
    requested = date(2026, 7, 14)
    through = requested if complete else date(2026, 7, 13)
    return AcquisitionResult(
        command=AcquisitionCommand.DAILY,
        status=status,
        requested_through_date=requested,
        run_id="2026-07-14_00000000000000000000000000000000",
        stats_run_id="stats_00000000000000000000000000000000",
        counts={"network_requests": 1},
        completeness=CompletenessResult(
            requested_through_date=requested,
            contiguous_regular_season_complete_through_date=through,
            latest_ingested_completed_game_date=through,
            partial_date=None if complete else requested,
            partial_date_reason=None if complete else "scheduled_regular_season_game_not_final",
        ),
        warnings=warnings,
        report_path=Path("report.json"),
        dry_run=False,
        outcome=(
            AcquisitionOutcome.SUCCESS
            if complete
            else AcquisitionOutcome.PARTIAL
        ),
    )


def test_success_exit_zero_and_resume_is_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls, _ = _stub_execution(monkeypatch, _result(complete=True))
    resume_id = "stats_" + "a" * 32

    code, stdout, stderr = _invoke(
        [
            "daily",
            "--date",
            "2026-07-14",
            *_paths(tmp_path),
            "--resume-run-id",
            resume_id,
        ]
    )

    assert code == cli.EXIT_SUCCESS
    assert stderr == ""
    assert json.loads(stdout)["exit_code"] == cli.EXIT_SUCCESS
    assert calls[0][0] is AcquisitionCommand.DAILY
    assert calls[0][1].resume_run_id == resume_id


def test_complete_result_with_informational_warning_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_execution(
        monkeypatch,
        _result(
            complete=True,
            status="completed_with_warnings",
            warnings=("explained_provider_difference",),
        ),
    )

    code, stdout, stderr = _invoke(
        ["daily", "--date", "2026-07-14", *_paths(tmp_path)]
    )

    assert code == cli.EXIT_SUCCESS
    assert stderr == ""
    assert json.loads(stdout)["exit_code"] == cli.EXIT_SUCCESS


def test_incomplete_result_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_execution(monkeypatch, _result(complete=False))

    code, stdout, stderr = _invoke(
        ["daily", "--date", "2026-07-14", *_paths(tmp_path)]
    )

    assert code == cli.EXIT_PARTIAL_OR_INCOMPLETE
    assert stderr == ""
    payload = json.loads(stdout)
    assert payload["exit_code"] == cli.EXIT_PARTIAL_OR_INCOMPLETE
    assert payload["status"] == "completed_with_warnings"
    assert "regular_season_completeness_not_satisfied" in payload["warnings"]


def test_operational_failure_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_execution(
        monkeypatch,
        AcquisitionExecutionError(
            "provider unavailable",
            run_id="run_20260714_00000000000000000000000000000000",
            stats_run_id="stats_00000000000000000000000000000000",
        ),
    )

    code, stdout, stderr = _invoke(
        ["daily", "--date", "2026-07-14", *_paths(tmp_path)]
    )

    assert code == cli.EXIT_OPERATIONAL_FAILURE
    assert stdout == ""
    payload = json.loads(stderr)
    assert payload["exit_code"] == cli.EXIT_OPERATIONAL_FAILURE
    assert payload["status"] == "failed"
    assert payload["run_id"] == "run_20260714_00000000000000000000000000000000"
    assert payload["stats_run_id"] == "stats_00000000000000000000000000000000"


class _TerminalFailureTransport:
    def fetch(self, request: StatsRequest) -> StatsResponse:
        raise StatsTransportError(
            "terminal provider error for ?token=test-only-sensitive-value",
            attempts=3,
            captures=(),
        )


def test_cli_and_persisted_report_share_failed_outcome_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "build_stats_transport",
        lambda **kwargs: _TerminalFailureTransport(),
    )

    code, stdout, stderr = _invoke(
        ["bootstrap-retrosheet", "--through-season", "2025", *_paths(tmp_path)]
    )

    assert code == cli.EXIT_OPERATIONAL_FAILURE
    assert stdout == ""
    emitted = json.loads(stderr)
    assert emitted["status"] == "failed"
    assert emitted["exit_code"] == 1
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    persisted = report["acquisition_runs"][0]
    assert persisted["outcome_contract_version"] == (
        "DSE_STATS_ACQUISITION_OUTCOME_V1"
    )
    assert persisted["outcome"] == "failed"
    assert persisted["status"] == "failed"
    assert persisted["exit_code"] == emitted["exit_code"] == 1
    assert persisted["partial_date"] is None
    assert persisted["partial_date_reason"] is None
    assert persisted["counts"] == {
        "provider_attempts": 3,
        "raw_captures_observed": 0,
    }
    assert "test-only-sensitive-value" not in stderr
    assert "test-only-sensitive-value" not in (tmp_path / "report.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("argv", "add_paths"),
    (
        (["daily", "--date", "2026-07-14"], False),
        (["daily", "--date", "2026-7-14"], True),
        (
            [
                "backfill-current",
                "--season",
                "2025",
                "--through",
                "2026-07-14",
            ],
            True,
        ),
    ),
)
def test_usage_errors_exit_64(
    tmp_path: Path, argv: list[str], add_paths: bool
) -> None:
    if add_paths:
        argv = [*argv, *_paths(tmp_path)]
    code, stdout, stderr = _invoke(argv)

    assert code == cli.EXIT_USAGE
    assert stdout == ""
    assert json.loads(stderr)["exit_code"] == cli.EXIT_USAGE


def test_offline_fixture_mode_is_explicit_and_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    calls, transport_args = _stub_execution(monkeypatch, _result(complete=True))

    code, _, stderr = _invoke(
        [
            "daily",
            "--date",
            "2026-07-14",
            *_paths(tmp_path),
            "--mode",
            "offline",
            "--fixture-root",
            str(fixture_root),
        ]
    )

    assert code == cli.EXIT_SUCCESS
    assert stderr == ""
    assert transport_args["mode"] is AcquisitionMode.OFFLINE
    assert transport_args["fixture_root"] == fixture_root
    assert calls[0][1].mode is AcquisitionMode.OFFLINE
