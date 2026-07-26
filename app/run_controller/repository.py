from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.artifacts import validate_artifact_relpath
from app.database import Database, utc_now
from app.identifiers import generate_run_id, parse_requested_date, validate_run_id
from app.migrations import CURRENT_SCHEMA_VERSION
from app.redaction import redact_text, redact_value
from app.run_controller.contracts import (
    CANONICAL_PIPELINE_PHASES,
    PIPELINE_RUN_TYPE,
    PIPELINE_SPORT,
    REUSABLE_PHASE_STATUSES,
    SUCCESSFUL_PHASE_STATUSES,
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelinePhaseV1,
    PipelineRunStatus,
    PipelineRunSummaryV1,
    PipelineRunV1,
    PipelineTransitionType,
    PipelineTransitionV1,
    validate_pipeline_phase_transition,
    validate_pipeline_run_transition,
)


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GIT_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_BEARER_MATERIAL = re.compile(r"\bbearer\s+[^\s,;\"']+", re.IGNORECASE)
_CREDENTIAL_MATERIAL = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{70,255}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{32,}\b"),
)


class PipelineRepositoryError(RuntimeError):
    pass


class PipelineRunNotFoundError(PipelineRepositoryError):
    pass


class PipelinePhaseNotFoundError(PipelineRepositoryError):
    pass


class DuplicatePipelineRunError(PipelineRepositoryError):
    def __init__(self, existing_run_id: str, existing_status: PipelineRunStatus) -> None:
        self.existing_run_id = existing_run_id
        self.existing_status = existing_status
        super().__init__(
            "manual pipeline run already exists for the requested slate date; "
            "resume it or explicitly force a new run"
        )


class PipelineRepositoryInvariantError(PipelineRepositoryError):
    pass


def resolve_code_revision(repository_root: Path | None = None) -> str:
    """Return the exact Git revision when available, otherwise an explicit fallback."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    revision = completed.stdout.strip().lower()
    return revision if completed.returncode == 0 and _GIT_REVISION.fullmatch(revision) else "unavailable"


def _requested_date(value: date | str) -> date:
    if isinstance(value, datetime):
        raise TypeError("requested_date must be a date or exact YYYY-MM-DD string")
    if isinstance(value, date):
        return value
    return parse_requested_date(value)


def _as_of_time(value: datetime | str) -> str:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("as_of_time must be an ISO-8601 timestamp") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise ValueError("as_of_time must include an explicit timezone offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value.strip()


def _timezone_name(value: str) -> str:
    normalized = _nonempty(value, "timezone")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone name") from exc
    return normalized


def _checksum(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if _SHA256.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be 64 lowercase hexadecimal characters")
    return normalized


def _code_revision(value: str | None, repository_root: Path | None) -> str:
    revision = resolve_code_revision(repository_root) if value is None else value.strip().lower()
    if revision != "unavailable" and _GIT_REVISION.fullmatch(revision) is None:
        raise ValueError("code_revision must be 40 lowercase hexadecimal characters or unavailable")
    return revision


def _safe_json(
    value: Any,
    *,
    secret_values: Iterable[str],
    name: str,
    reject_bearer: bool = False,
) -> tuple[Any, str]:
    try:
        raw_encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    if reject_bearer and _BEARER_MATERIAL.search(raw_encoded):
        raise ValueError(f"{name} contains bearer credential material")
    safe = redact_value(
        value,
        secret_values,
        preserve_field_names=("id", "key", "provider_id", "region_key"),
    )
    try:
        encoded = json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON serializable") from exc
    if any(pattern.search(encoded) for pattern in _CREDENTIAL_MATERIAL):
        raise ValueError(f"{name} contains credential material")
    return json.loads(encoded), encoded


def _decode_json(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


def _run_from_row(row: sqlite3.Row) -> PipelineRunV1:
    failure_phase = row["failure_phase"]
    return PipelineRunV1(
        run_id=str(row["run_id"]),
        sport=str(row["sport"]),
        run_type=str(row["run_type"]),
        requested_date=str(row["requested_date"]),
        as_of_time=str(row["as_of_time"]),
        timezone=str(row["timezone"]),
        pipeline_version=str(row["pipeline_version"]),
        configuration_version=str(row["configuration_version"]),
        configuration_fingerprint=str(row["configuration_fingerprint"]),
        configuration_metadata=_decode_json(row["configuration_metadata_json"]) or {},
        code_revision=str(row["code_revision"]),
        database_schema_version=int(row["database_schema_version"]),
        force_refresh=bool(row["force_refresh"]),
        status=PipelineRunStatus(row["status"]),
        failure_phase=PipelinePhaseKey(failure_phase) if failure_phase else None,
        error_message=row["error_message"],
        final_summary=_decode_json(row["final_summary_json"]),
        created_at=str(row["created_at"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        updated_at=str(row["updated_at"]),
    )


def _phase_from_row(row: sqlite3.Row) -> PipelinePhaseV1:
    return PipelinePhaseV1(
        run_id=str(row["run_id"]),
        phase_key=PipelinePhaseKey(row["phase_key"]),
        ordinal=int(row["ordinal"]),
        status=PipelinePhaseStatus(row["status"]),
        attempt_count=int(row["attempt_count"]),
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        updated_at=str(row["updated_at"]),
        input_checksum=row["input_checksum"],
        output_checksum=row["output_checksum"],
        artifact_relpath=row["artifact_relpath"],
        warnings=_decode_json(row["warnings_json"]),
        error=_decode_json(row["error_json"]),
        reused_from_run_id=row["reused_from_run_id"],
    )


class PipelineRunRepository:
    def __init__(
        self,
        database: Database,
        *,
        secret_values: Iterable[str] = (),
        repository_root: Path | None = None,
    ) -> None:
        self.database = database
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.repository_root = repository_root

    def create_pipeline_run(
        self,
        *,
        requested_date: date | str,
        as_of_time: datetime | str,
        timezone_name: str,
        pipeline_version: str,
        configuration_version: str,
        configuration_metadata: Mapping[str, Any],
        force_refresh: bool = False,
        code_revision: str | None = None,
        run_id: str | None = None,
        created_at: str | None = None,
    ) -> PipelineRunSummaryV1:
        parsed_date = _requested_date(requested_date)
        safe_run_id = validate_run_id(
            run_id or generate_run_id(parsed_date),
            expected_date=parsed_date,
        )
        if not isinstance(configuration_metadata, Mapping):
            raise TypeError("configuration_metadata must be a mapping")
        safe_configuration, configuration_json = _safe_json(
            dict(configuration_metadata),
            secret_values=self.secret_values,
            name="configuration_metadata",
            reject_bearer=True,
        )
        if not isinstance(safe_configuration, Mapping):
            raise PipelineRepositoryInvariantError(
                "safe configuration metadata is not a mapping"
            )
        safe_configuration_version = _nonempty(
            configuration_version,
            "configuration_version",
        )
        fingerprint_payload = json.dumps(
            {
                "configuration_version": safe_configuration_version,
                "metadata": safe_configuration,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        configuration_fingerprint = hashlib.sha256(
            fingerprint_payload.encode("utf-8")
        ).hexdigest()
        timestamp = created_at or utc_now()
        values = (
            safe_run_id,
            PIPELINE_SPORT,
            PIPELINE_RUN_TYPE,
            parsed_date.isoformat(),
            _as_of_time(as_of_time),
            _timezone_name(timezone_name),
            _nonempty(pipeline_version, "pipeline_version"),
            safe_configuration_version,
            configuration_fingerprint,
            configuration_json,
            _code_revision(code_revision, self.repository_root),
            CURRENT_SCHEMA_VERSION,
            int(bool(force_refresh)),
            timestamp,
            timestamp,
        )

        with self.database.connect(write=True) as connection:
            existing = connection.execute(
                """
                SELECT run_id, status FROM pipeline_runs
                WHERE sport=? AND run_type=? AND requested_date=?
                ORDER BY created_at DESC, run_id DESC LIMIT 1
                """,
                (PIPELINE_SPORT, PIPELINE_RUN_TYPE, parsed_date.isoformat()),
            ).fetchone()
            if existing is not None and not force_refresh:
                raise DuplicatePipelineRunError(
                    str(existing["run_id"]),
                    PipelineRunStatus(existing["status"]),
                )
            try:
                connection.execute(
                    """
                    INSERT INTO pipeline_runs(
                        run_id,sport,run_type,requested_date,as_of_time,timezone,
                        pipeline_version,configuration_version,
                        configuration_fingerprint,configuration_metadata_json,
                        code_revision,database_schema_version,force_refresh,status,
                        failure_phase,error_message,final_summary_json,created_at,
                        started_at,completed_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,NULL,?,
                              NULL,NULL,?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicatePipelineRunError(
                    safe_run_id,
                    PipelineRunStatus.PENDING,
                ) from exc
            connection.executemany(
                """
                INSERT INTO pipeline_run_phases(
                    run_id,phase_key,ordinal,status,attempt_count,started_at,
                    completed_at,updated_at,input_checksum,output_checksum,
                    artifact_relpath,warnings_json,error_json,reused_from_run_id
                ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, NULL,
                          NULL, NULL, NULL, NULL)
                """,
                [
                    (safe_run_id, phase.key.value, phase.ordinal, timestamp)
                    for phase in CANONICAL_PIPELINE_PHASES
                ],
            )
            transitions: list[
                tuple[str, str | None, str | None, str, str, str, str, str]
            ] = [
                (
                    safe_run_id,
                    None,
                    None,
                    PipelineRunStatus.PENDING.value,
                    PipelineTransitionType.CREATED.value,
                    "pipeline run created",
                    json.dumps(
                        {"force_refresh": bool(force_refresh)},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                )
            ]
            transitions.extend(
                (
                    safe_run_id,
                    phase.key.value,
                    None,
                    PipelinePhaseStatus.PENDING.value,
                    PipelineTransitionType.CREATED.value,
                    "pipeline phase initialized",
                    '{"attempt_count":0}',
                    timestamp,
                )
                for phase in CANONICAL_PIPELINE_PHASES
            )
            connection.executemany(
                """
                INSERT INTO pipeline_run_transitions(
                    run_id,phase_key,from_status,to_status,transition_type,
                    reason,audit_metadata_json,transitioned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                transitions,
            )
            run_row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?",
                (safe_run_id,),
            ).fetchone()
            phase_rows = connection.execute(
                """
                SELECT * FROM pipeline_run_phases
                WHERE run_id=? ORDER BY ordinal
                """,
                (safe_run_id,),
            ).fetchall()
        if run_row is None or len(phase_rows) != len(CANONICAL_PIPELINE_PHASES):
            raise PipelineRepositoryInvariantError("pipeline run initialization was incomplete")
        return PipelineRunSummaryV1(
            run=_run_from_row(run_row),
            phases=tuple(_phase_from_row(row) for row in phase_rows),
        )

    def get_pipeline_run(self, run_id: str) -> PipelineRunV1 | None:
        safe_run_id = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?",
                (safe_run_id,),
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def find_existing_manual_run(
        self,
        requested_date: date | str,
    ) -> PipelineRunV1 | None:
        parsed_date = _requested_date(requested_date)
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pipeline_runs
                WHERE sport=? AND run_type=? AND requested_date=?
                ORDER BY created_at DESC, run_id DESC LIMIT 1
                """,
                (PIPELINE_SPORT, PIPELINE_RUN_TYPE, parsed_date.isoformat()),
            ).fetchone()
        return None if row is None else _run_from_row(row)

    def get_pipeline_run_phases(self, run_id: str) -> tuple[PipelinePhaseV1, ...]:
        safe_run_id = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_run_phases
                WHERE run_id=? ORDER BY ordinal
                """,
                (safe_run_id,),
            ).fetchall()
        return tuple(_phase_from_row(row) for row in rows)

    def get_pipeline_run_transitions(
        self,
        run_id: str,
    ) -> tuple[PipelineTransitionV1, ...]:
        safe_run_id = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pipeline_run_transitions
                WHERE run_id=? ORDER BY transitioned_at, id
                """,
                (safe_run_id,),
            ).fetchall()
        return tuple(
            PipelineTransitionV1(
                transition_id=int(row["id"]),
                run_id=str(row["run_id"]),
                phase_key=(
                    PipelinePhaseKey(row["phase_key"])
                    if row["phase_key"] is not None
                    else None
                ),
                from_status=row["from_status"],
                to_status=str(row["to_status"]),
                transition_type=PipelineTransitionType(row["transition_type"]),
                reason=row["reason"],
                audit_metadata=_decode_json(row["audit_metadata_json"]) or {},
                transitioned_at=str(row["transitioned_at"]),
            )
            for row in rows
        )

    def transition_pipeline_run(
        self,
        run_id: str,
        target: PipelineRunStatus,
        *,
        failure_phase: PipelinePhaseKey | None = None,
        error_message: str | None = None,
        final_summary: Mapping[str, Any] | None = None,
        reason: str | None = None,
        transitioned_at: str | None = None,
    ) -> PipelineRunV1:
        return self._transition_pipeline_run(
            run_id,
            target,
            transition_type=PipelineTransitionType.STATE_TRANSITION,
            failure_phase=failure_phase,
            error_message=error_message,
            final_summary=final_summary,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def resume_pipeline_run(
        self,
        run_id: str,
        *,
        reason: str,
        transitioned_at: str | None = None,
    ) -> PipelineRunV1:
        return self._transition_pipeline_run(
            run_id,
            PipelineRunStatus.RUNNING,
            transition_type=PipelineTransitionType.RESUME,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def _transition_pipeline_run(
        self,
        run_id: str,
        target: PipelineRunStatus,
        *,
        transition_type: PipelineTransitionType,
        failure_phase: PipelinePhaseKey | None = None,
        error_message: str | None = None,
        final_summary: Mapping[str, Any] | None = None,
        reason: str | None = None,
        transitioned_at: str | None = None,
    ) -> PipelineRunV1:
        safe_run_id = validate_run_id(run_id)
        target_status = target if isinstance(target, PipelineRunStatus) else PipelineRunStatus(target)
        safe_failure_phase = (
            failure_phase
            if failure_phase is None or isinstance(failure_phase, PipelinePhaseKey)
            else PipelinePhaseKey(failure_phase)
        )
        timestamp = transitioned_at or utc_now()
        safe_reason = redact_text(reason, self.secret_values) if reason else None
        safe_error = (
            redact_text(error_message, self.secret_values)
            if error_message is not None
            else None
        )
        if target_status is PipelineRunStatus.FAILED:
            if safe_failure_phase is None or not safe_error:
                raise ValueError("failed pipeline runs require failure_phase and error_message")
        elif safe_failure_phase is not None or safe_error is not None:
            raise ValueError("failure metadata is valid only for failed pipeline runs")
        summary_json = None
        if final_summary is not None:
            _, summary_json = _safe_json(
                dict(final_summary),
                secret_values=self.secret_values,
                name="final_summary",
            )

        with self.database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?",
                (safe_run_id,),
            ).fetchone()
            if row is None:
                raise PipelineRunNotFoundError(f"pipeline run not found: {safe_run_id}")
            current = PipelineRunStatus(row["status"])
            validate_pipeline_run_transition(
                current,
                target_status,
                transition_type=transition_type,
            )
            phase_rows = connection.execute(
                "SELECT phase_key,status FROM pipeline_run_phases WHERE run_id=?",
                (safe_run_id,),
            ).fetchall()
            if target_status in {
                PipelineRunStatus.SUCCEEDED,
                PipelineRunStatus.SUCCEEDED_WITH_WARNINGS,
                PipelineRunStatus.DEGRADED,
            }:
                statuses = {PipelinePhaseStatus(item["status"]) for item in phase_rows}
                if len(phase_rows) != len(CANONICAL_PIPELINE_PHASES) or not statuses.issubset(
                    SUCCESSFUL_PHASE_STATUSES
                ):
                    raise PipelineRepositoryInvariantError(
                        "successful pipeline run requires every phase to be terminal and non-failed"
                    )
            if target_status is PipelineRunStatus.FAILED:
                if safe_failure_phase is None:
                    raise PipelineRepositoryInvariantError(
                        "failed pipeline run is missing its failure phase"
                    )
                failed = next(
                    (
                        item
                        for item in phase_rows
                        if item["phase_key"] == safe_failure_phase.value
                    ),
                    None,
                )
                if failed is None or PipelinePhaseStatus(failed["status"]) is not PipelinePhaseStatus.FAILED:
                    raise PipelineRepositoryInvariantError(
                        "pipeline run failure_phase must identify a failed phase"
                    )

            started_at = row["started_at"]
            completed_at = row["completed_at"]
            if target_status is PipelineRunStatus.RUNNING:
                started_at = started_at or timestamp
                completed_at = None
            elif target_status is not PipelineRunStatus.PENDING:
                completed_at = timestamp
            connection.execute(
                """
                UPDATE pipeline_runs
                SET status=?,failure_phase=?,error_message=?,final_summary_json=?,
                    started_at=?,completed_at=?,updated_at=?
                WHERE run_id=?
                """,
                (
                    target_status.value,
                    safe_failure_phase.value if safe_failure_phase else None,
                    safe_error,
                    summary_json,
                    started_at,
                    completed_at,
                    timestamp,
                    safe_run_id,
                ),
            )
            self._insert_transition(
                connection,
                run_id=safe_run_id,
                phase_key=None,
                from_status=current.value,
                to_status=target_status.value,
                transition_type=transition_type,
                reason=safe_reason,
                audit_metadata={
                    "failure_phase": (
                        safe_failure_phase.value if safe_failure_phase else None
                    ),
                    "error_message": safe_error,
                    "final_summary": _decode_json(summary_json),
                },
                transitioned_at=timestamp,
            )
            updated = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?",
                (safe_run_id,),
            ).fetchone()
        if updated is None:
            raise PipelineRepositoryInvariantError("transitioned pipeline run disappeared")
        return _run_from_row(updated)

    def transition_pipeline_phase(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        target: PipelinePhaseStatus,
        *,
        input_checksum: str | None = None,
        output_checksum: str | None = None,
        artifact_relpath: str | None = None,
        warnings: Any | None = None,
        error: Any | None = None,
        reason: str | None = None,
        transitioned_at: str | None = None,
    ) -> PipelinePhaseV1:
        return self._transition_pipeline_phase(
            run_id,
            phase_key,
            target,
            transition_type=PipelineTransitionType.STATE_TRANSITION,
            input_checksum=input_checksum,
            output_checksum=output_checksum,
            artifact_relpath=artifact_relpath,
            warnings=warnings,
            error=error,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def retry_pipeline_phase(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        *,
        reason: str,
        transitioned_at: str | None = None,
    ) -> PipelinePhaseV1:
        return self._transition_pipeline_phase(
            run_id,
            phase_key,
            PipelinePhaseStatus.RUNNING,
            transition_type=PipelineTransitionType.RETRY,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def force_refresh_pipeline_phase(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        *,
        reason: str,
        input_checksum: str | None = None,
        transitioned_at: str | None = None,
    ) -> PipelinePhaseV1:
        return self._transition_pipeline_phase(
            run_id,
            phase_key,
            PipelinePhaseStatus.RUNNING,
            transition_type=PipelineTransitionType.FORCED_REFRESH,
            input_checksum=input_checksum,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def mark_pipeline_phase_reused(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        *,
        reused_from_run_id: str,
        reason: str,
        transitioned_at: str | None = None,
    ) -> PipelinePhaseV1:
        safe_run_id = validate_run_id(run_id)
        safe_source_run_id = validate_run_id(reused_from_run_id)
        safe_phase_key = (
            phase_key if isinstance(phase_key, PipelinePhaseKey) else PipelinePhaseKey(phase_key)
        )
        if safe_source_run_id == safe_run_id:
            raise ValueError("reused_from_run_id must identify a different run")
        with self.database.connect() as connection:
            source = connection.execute(
                """
                SELECT * FROM pipeline_run_phases
                WHERE run_id=? AND phase_key=?
                """,
                (safe_source_run_id, safe_phase_key.value),
            ).fetchone()
        if source is None:
            raise PipelinePhaseNotFoundError("reused source pipeline phase was not found")
        if PipelinePhaseStatus(source["status"]) not in REUSABLE_PHASE_STATUSES:
            raise PipelineRepositoryInvariantError(
                "reused source pipeline phase is not successfully terminal"
            )
        return self._transition_pipeline_phase(
            safe_run_id,
            safe_phase_key,
            PipelinePhaseStatus.REUSED,
            transition_type=PipelineTransitionType.REUSE,
            input_checksum=source["input_checksum"],
            output_checksum=source["output_checksum"],
            artifact_relpath=source["artifact_relpath"],
            warnings=_decode_json(source["warnings_json"]),
            reused_from_run_id=safe_source_run_id,
            reason=reason,
            transitioned_at=transitioned_at,
        )

    def _transition_pipeline_phase(
        self,
        run_id: str,
        phase_key: PipelinePhaseKey,
        target: PipelinePhaseStatus,
        *,
        transition_type: PipelineTransitionType,
        input_checksum: str | None = None,
        output_checksum: str | None = None,
        artifact_relpath: str | None = None,
        warnings: Any | None = None,
        error: Any | None = None,
        reused_from_run_id: str | None = None,
        reason: str | None = None,
        transitioned_at: str | None = None,
    ) -> PipelinePhaseV1:
        safe_run_id = validate_run_id(run_id)
        safe_phase_key = (
            phase_key if isinstance(phase_key, PipelinePhaseKey) else PipelinePhaseKey(phase_key)
        )
        target_status = (
            target if isinstance(target, PipelinePhaseStatus) else PipelinePhaseStatus(target)
        )
        safe_input = _checksum(input_checksum, "input_checksum")
        safe_output = _checksum(output_checksum, "output_checksum")
        safe_artifact = (
            validate_artifact_relpath(artifact_relpath)
            if artifact_relpath is not None
            else None
        )
        safe_reason = redact_text(reason, self.secret_values) if reason else None
        warnings_json = None
        if warnings is not None:
            _, warnings_json = _safe_json(
                warnings,
                secret_values=self.secret_values,
                name="warnings",
            )
        error_json = None
        if error is not None:
            _, error_json = _safe_json(
                error,
                secret_values=self.secret_values,
                name="error",
            )
        if target_status is PipelinePhaseStatus.FAILED and error_json is None:
            raise ValueError("failed pipeline phases require error metadata")
        if target_status is not PipelinePhaseStatus.FAILED and error_json is not None:
            raise ValueError("error metadata is valid only for failed pipeline phases")
        timestamp = transitioned_at or utc_now()

        with self.database.connect(write=True) as connection:
            row = connection.execute(
                """
                SELECT * FROM pipeline_run_phases
                WHERE run_id=? AND phase_key=?
                """,
                (safe_run_id, safe_phase_key.value),
            ).fetchone()
            if row is None:
                raise PipelinePhaseNotFoundError(
                    f"pipeline phase not found: {safe_run_id}/{safe_phase_key.value}"
                )
            current = PipelinePhaseStatus(row["status"])
            validate_pipeline_phase_transition(
                current,
                target_status,
                transition_type=transition_type,
            )
            attempt_count = int(row["attempt_count"])
            started_at = row["started_at"]
            completed_at = row["completed_at"]
            stored_input = row["input_checksum"]
            stored_output = row["output_checksum"]
            stored_artifact = row["artifact_relpath"]
            stored_warnings = row["warnings_json"]
            if target_status is PipelinePhaseStatus.RUNNING:
                attempt_count += 1
                started_at = timestamp
                completed_at = None
                stored_output = None
                stored_artifact = None
                stored_warnings = None
            else:
                completed_at = timestamp
            connection.execute(
                """
                UPDATE pipeline_run_phases
                SET status=?,attempt_count=?,started_at=?,completed_at=?,
                    updated_at=?,input_checksum=?,output_checksum=?,
                    artifact_relpath=?,warnings_json=?,error_json=?,
                    reused_from_run_id=?
                WHERE run_id=? AND phase_key=?
                """,
                (
                    target_status.value,
                    attempt_count,
                    started_at,
                    completed_at,
                    timestamp,
                    safe_input if safe_input is not None else stored_input,
                    safe_output if safe_output is not None else stored_output,
                    safe_artifact if safe_artifact is not None else stored_artifact,
                    warnings_json if warnings_json is not None else stored_warnings,
                    error_json,
                    reused_from_run_id,
                    safe_run_id,
                    safe_phase_key.value,
                ),
            )
            self._insert_transition(
                connection,
                run_id=safe_run_id,
                phase_key=safe_phase_key,
                from_status=current.value,
                to_status=target_status.value,
                transition_type=transition_type,
                reason=safe_reason,
                audit_metadata={
                    "attempt_count": attempt_count,
                    "input_checksum": (
                        safe_input if safe_input is not None else stored_input
                    ),
                    "output_checksum": (
                        safe_output if safe_output is not None else stored_output
                    ),
                    "artifact_relpath": (
                        safe_artifact if safe_artifact is not None else stored_artifact
                    ),
                    "warnings": _decode_json(
                        warnings_json if warnings_json is not None else stored_warnings
                    ),
                    "error": _decode_json(error_json),
                    "reused_from_run_id": reused_from_run_id,
                },
                transitioned_at=timestamp,
            )
            updated = connection.execute(
                """
                SELECT * FROM pipeline_run_phases
                WHERE run_id=? AND phase_key=?
                """,
                (safe_run_id, safe_phase_key.value),
            ).fetchone()
        if updated is None:
            raise PipelineRepositoryInvariantError("transitioned pipeline phase disappeared")
        return _phase_from_row(updated)

    @staticmethod
    def _insert_transition(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        phase_key: PipelinePhaseKey | None,
        from_status: str | None,
        to_status: str,
        transition_type: PipelineTransitionType,
        reason: str | None,
        audit_metadata: Mapping[str, Any],
        transitioned_at: str,
    ) -> None:
        metadata_json = json.dumps(
            audit_metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            """
            INSERT INTO pipeline_run_transitions(
                run_id,phase_key,from_status,to_status,transition_type,
                reason,audit_metadata_json,transitioned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                phase_key.value if phase_key else None,
                from_status,
                to_status,
                transition_type.value,
                reason,
                metadata_json,
                transitioned_at,
            ),
        )
