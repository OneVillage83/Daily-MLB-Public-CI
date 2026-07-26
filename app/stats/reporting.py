from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


REPORT_VERSION = "DSE_STATS_VALIDATION_REPORT_V1"
RAW_CHECKSUM_INVENTORY_VERSION = "DSE_STATS_RAW_CHECKSUM_INVENTORY_V1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def canonical_report_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def report_checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_report_bytes(payload)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_raw_checksum_inventory(
    root: Path,
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Verify retained raw objects and bind the sorted inventory to one digest."""

    resolved_root = root.resolve(strict=True)
    entries: list[dict[str, Any]] = []
    warning_codes: set[str] = set()
    for record in sorted(
        records,
        key=lambda value: (
            str(value.get("provider", "")),
            str(value.get("endpoint_category", "")),
            str(value.get("artifact_relpath", "")),
            str(value.get("raw_payload_id", "")),
        ),
    ):
        expected_checksum = str(record.get("checksum_sha256", ""))
        relpath = str(record.get("artifact_relpath", ""))
        metadata_size = record.get("size_bytes")
        expected_size = (
            int(metadata_size)
            if isinstance(metadata_size, int)
            and not isinstance(metadata_size, bool)
            and metadata_size >= 0
            else None
        )
        actual_checksum: str | None = None
        actual_size: int | None = None
        status = "verified"
        candidate = resolved_root.joinpath(relpath).resolve(strict=False)
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            status = "path_escape"
            warning_codes.add("raw_artifact_path_escape")
        else:
            if candidate.is_symlink() or not candidate.is_file():
                status = "missing_or_symlink"
                warning_codes.add("raw_artifact_missing")
            else:
                actual_size = candidate.stat().st_size
                actual_checksum = _file_sha256(candidate)
                if _SHA256_PATTERN.fullmatch(expected_checksum) is None:
                    status = "invalid_expected_checksum"
                    warning_codes.add("raw_artifact_checksum_mismatch")
                elif actual_checksum != expected_checksum:
                    status = "checksum_mismatch"
                    warning_codes.add("raw_artifact_checksum_mismatch")
                elif expected_size is None:
                    status = "invalid_metadata_size"
                    warning_codes.add("raw_artifact_metadata_invalid")
                elif actual_size != expected_size:
                    status = "size_mismatch"
                    warning_codes.add("raw_artifact_size_mismatch")
        entry = {
            "raw_payload_id": str(record.get("raw_payload_id", "")),
            "provider": str(record.get("provider", "")),
            "endpoint_category": str(record.get("endpoint_category", "")),
            "artifact_relpath": relpath.replace("\\", "/"),
            "expected_checksum_sha256": expected_checksum,
            "actual_checksum_sha256": actual_checksum,
            "expected_size_bytes": expected_size,
            "actual_size_bytes": actual_size,
            "verification_status": status,
        }
        entry["entry_digest_sha256"] = report_checksum(entry)
        entries.append(entry)

    root_payload = {
        "contract_version": RAW_CHECKSUM_INVENTORY_VERSION,
        "entries": entries,
    }
    verified_count = sum(
        entry["verification_status"] == "verified" for entry in entries
    )
    inventory = {
        **root_payload,
        "metadata_record_count": len(entries),
        "verified_checksum_count": verified_count,
        "failed_verification_count": len(entries) - verified_count,
        "verified_byte_count": sum(
            int(entry["actual_size_bytes"] or 0)
            for entry in entries
            if entry["verification_status"] == "verified"
        ),
        "root_digest_sha256": report_checksum(root_payload),
    }
    return inventory, tuple(sorted(warning_codes))


def write_report(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_report_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def merge_validation_report(
    path: Path,
    *,
    section: str,
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    if section not in {
        "acquisition_run",
        "initial_validation",
        "incremental_validation",
    }:
        raise ValueError("unknown validation report section")
    existing: dict[str, Any] = {"report_version": REPORT_VERSION}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict) or loaded.get("report_version") != REPORT_VERSION:
            raise ValueError("existing stats validation report has an incompatible contract")
        existing = loaded
    if section == "initial_validation":
        prior = existing.get("initial_validation")
        if prior is not None and prior != validation:
            raise ValueError("initial July 14 validation evidence is immutable")
        existing["initial_validation"] = dict(validation)
    else:
        collection_key = (
            "acquisition_runs"
            if section == "acquisition_run"
            else "incremental_validations"
        )
        incremental = existing.setdefault(collection_key, [])
        if not isinstance(incremental, list):
            raise ValueError(f"{collection_key} must be an array")
        checksum = report_checksum(validation)
        if not any(
            isinstance(item, dict) and item.get("validation_checksum") == checksum
            for item in incremental
        ):
            item = dict(validation)
            item["validation_checksum"] = checksum
            incremental.append(item)
            incremental.sort(
                key=lambda value: (
                    str(value.get("requested_through_date", "")),
                    str(value.get("source_observed_at", "")),
                    str(value.get("validation_checksum", "")),
                )
            )
    write_report(path, existing)
    return existing
