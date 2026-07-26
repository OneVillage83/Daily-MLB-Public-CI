from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionOutcome,
    AcquisitionResult,
    StatsAcquisitionService,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.reporting import (
    build_raw_checksum_inventory,
    merge_validation_report,
    write_report,
)
from app.stats.transport import FixtureStatsTransport


@pytest.mark.parametrize(
    ("outcome", "status", "expected_exit_code"),
    (
        (AcquisitionOutcome.SUCCESS, "completed", 0),
        (AcquisitionOutcome.FAILED, "failed", 1),
        (AcquisitionOutcome.PARTIAL, "completed_with_warnings", 2),
    ),
)
def test_shared_result_serialization_uses_authoritative_exit_code(
    outcome: AcquisitionOutcome,
    status: str,
    expected_exit_code: int,
) -> None:
    result = AcquisitionResult(
        command=AcquisitionCommand.DAILY,
        status=status,
        requested_through_date=date(2026, 7, 16),
        run_id="run_20260716_00000000000000000000000000000000",
        stats_run_id="stats_00000000000000000000000000000000",
        counts={},
        completeness=None,
        warnings=(),
        report_path=Path("validation.json"),
        dry_run=False,
        outcome=outcome,
    )

    payload = result.as_dict()

    assert payload["outcome_contract_version"] == (
        "DSE_STATS_ACQUISITION_OUTCOME_V1"
    )
    assert payload["outcome"] == outcome.value
    assert payload["exit_code"] == result.exit_code == expected_exit_code


def test_report_bytes_are_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_report(first, {"b": 2, "a": [1, 0]})
    write_report(second, {"a": [1, 0], "b": 2})
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().endswith(b"\n")


def test_raw_checksum_inventory_is_deterministic_and_detects_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    first = b"first fixture\n"
    second = b"second fixture\n"
    (root / "a.csv").write_bytes(first)
    (root / "b.csv").write_bytes(second)
    records = [
        {
            "raw_payload_id": "raw-b",
            "provider": "statcast",
            "endpoint_category": "daily",
            "artifact_relpath": "b.csv",
            "checksum_sha256": hashlib.sha256(second).hexdigest(),
            "size_bytes": len(second),
        },
        {
            "raw_payload_id": "raw-a",
            "provider": "baseball_reference",
            "endpoint_category": "schedule",
            "artifact_relpath": "a.csv",
            "checksum_sha256": hashlib.sha256(first).hexdigest(),
            "size_bytes": len(first),
        },
    ]

    inventory, warnings = build_raw_checksum_inventory(root, records)
    reordered, reordered_warnings = build_raw_checksum_inventory(
        root, tuple(reversed(records))
    )

    assert warnings == reordered_warnings == ()
    assert inventory == reordered
    assert inventory["metadata_record_count"] == 2
    assert inventory["verified_checksum_count"] == 2
    assert inventory["failed_verification_count"] == 0
    assert len(inventory["root_digest_sha256"]) == 64
    assert [entry["raw_payload_id"] for entry in inventory["entries"]] == [
        "raw-a",
        "raw-b",
    ]

    (root / "a.csv").write_bytes(b"changed\n")
    drifted, drifted_warnings = build_raw_checksum_inventory(root, records)

    assert drifted_warnings == ("raw_artifact_checksum_mismatch",)
    assert drifted["verified_checksum_count"] == 1
    assert drifted["failed_verification_count"] == 1
    assert drifted["root_digest_sha256"] != inventory["root_digest_sha256"]


def test_incremental_validation_never_overwrites_initial_evidence(tmp_path: Path) -> None:
    path = tmp_path / "validation.json"
    initial = {"requested_through_date": "2026-07-14", "result": "passed"}
    merge_validation_report(path, section="initial_validation", validation=initial)
    merge_validation_report(
        path,
        section="incremental_validation",
        validation={"requested_through_date": "2026-07-16", "result": "partial"},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["initial_validation"] == initial
    assert payload["incremental_validations"][0]["requested_through_date"] == "2026-07-16"

    with pytest.raises(ValueError, match="immutable"):
        merge_validation_report(
            path,
            section="initial_validation",
            validation={"requested_through_date": "2026-07-14", "result": "changed"},
        )


def test_july_16_validate_cannot_preempt_initial_july_14_evidence(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "validation.json"
    raw_store = RawArtifactStore(tmp_path / "raw")
    service = StatsAcquisitionService(
        database=Database(tmp_path / "stats.db"),
        raw_store=raw_store,
        transport=FixtureStatsTransport(raw_store, {}),
        report_path=report_path,
    )

    service._write_report(
        AcquisitionResult(
            command=AcquisitionCommand.VALIDATE,
            status="completed_with_warnings",
            requested_through_date=date(2026, 7, 16),
            run_id=None,
            stats_run_id=None,
            counts={},
            completeness=None,
            warnings=("regular_season_completeness_not_satisfied",),
            report_path=report_path,
            dry_run=False,
        )
    )
    after_july_16 = json.loads(report_path.read_text(encoding="utf-8"))
    assert "initial_validation" not in after_july_16
    assert after_july_16["incremental_validations"][0][
        "requested_through_date"
    ] == "2026-07-16"

    service._write_report(
        AcquisitionResult(
            command=AcquisitionCommand.VALIDATE,
            status="completed",
            requested_through_date=date(2026, 7, 14),
            run_id=None,
            stats_run_id=None,
            counts={},
            completeness=None,
            warnings=(),
            report_path=report_path,
            dry_run=False,
        )
    )
    final = json.loads(report_path.read_text(encoding="utf-8"))
    assert final["initial_validation"]["requested_through_date"] == "2026-07-14"
    assert final["incremental_validations"][0]["requested_through_date"] == (
        "2026-07-16"
    )
