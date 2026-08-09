from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.database import Database
from app.decision_evidence import (
    create_decision_manifest,
    publish_decision_manifest,
    verify_decision_manifest,
    publish_snapshot,
    verify_snapshot,
)
from app.final_qc.assessment import assess_final_qc
from app.final_qc.contracts import FINAL_QC_ATTEMPT_MANIFEST_CONTRACT, FinalQcCheckV1, FinalQcPolicyV1, FinalQcV1
from app.identifiers import validate_run_id
from app.infographic.repository import InfographicRepository, PersistedInfographicV1
from app.pdf_report.repository import PdfReportRepository, PersistedPdfReportV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)


class FinalQcRepositoryError(RuntimeError):
    pass


class FinalQcNotFoundError(FinalQcRepositoryError):
    pass


class FinalQcConflict(FinalQcRepositoryError):
    pass


class FinalQcIntegrityError(FinalQcRepositoryError):
    pass


class FinalQcAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    VALIDATION_FAILED = "validation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True, slots=True)
class FinalQcUpstreamV1:
    pdf: PersistedPdfReportV1
    infographic: PersistedInfographicV1


@dataclass(frozen=True, slots=True)
class PersistedFinalQcV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    snapshot: FinalQcV1
    artifact: PreModelArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class FinalQcAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: FinalQcAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    checks: tuple[FinalQcCheckV1, ...]
    manifest: PreModelArtifactV1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(v: object, n: str) -> datetime:
    try:
        return aware_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")), n)
    except ValueError as e:
        raise FinalQcIntegrityError(f"persisted {n} invalid") from e


def _policy(v: object) -> FinalQcPolicyV1:
    p = json.loads(v) if isinstance(v, str) else v
    if not isinstance(p, dict):
        raise FinalQcIntegrityError("QC policy invalid")
    q = FinalQcPolicyV1(tuple(str(x) for x in p["required_checks"]), str(p["policy_version"]))
    if p.get("checksum") != q.checksum:
        raise FinalQcIntegrityError("QC policy checksum mismatch")
    return q


class FinalQcRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: FinalQcPolicyV1 = FinalQcPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.pdf = PdfReportRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.infographic = InfographicRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> FinalQcUpstreamV1:
        info = self.infographic.get_latest_for_run_for_final_qc(validate_run_id(run_id))
        if info is None:
            raise FinalQcIntegrityError("Final QC requires sealed Infographic")
        pdf = self.pdf.get_by_snapshot_id_for_final_qc(info.document.upstream_pdf_report_snapshot_id)
        return FinalQcUpstreamV1(pdf, info)

    @staticmethod
    def _active(c: sqlite3.Connection, run_id: str, attempt: int) -> None:
        r = c.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='final_qc'", (run_id,)
        ).fetchone()
        if r is None or str(r["status"]) != "running" or int(r["attempt_count"]) != attempt:
            raise FinalQcIntegrityError("Final QC attempt is not active")

    def assess(
        self, u: FinalQcUpstreamV1, *, evaluated_at: datetime
    ) -> tuple[tuple[FinalQcCheckV1, ...], FinalQcV1 | None]:
        return assess_final_qc(
            pdf=u.pdf,
            infographic=u.infographic,
            evaluated_at=evaluated_at,
            artifact_root=self.artifact_root,
            policy=self.policy,
            secret_values=self.secret_values,
        )

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        u: FinalQcUpstreamV1,
        evaluated_at: datetime,
        input_checksum: str,
        outcome: FinalQcAttemptOutcome,
        snapshot: FinalQcV1 | None,
        warnings: tuple[Mapping[str, object], ...],
    ) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=FINAL_QC_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="final_qc",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=u.pdf.document.requested_date,
            as_of_time=u.pdf.document.as_of_time,
            observed_at=evaluated_at,
            phase_input_checksum=input_checksum,
            upstream=(
                PreModelUpstreamIdentityV1("pdf_report", u.pdf.snapshot_id, u.pdf.document.checksum),
                PreModelUpstreamIdentityV1("infographic", u.infographic.snapshot_id, u.infographic.document.checksum),
            ),
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=self._now(),
            phase_input_evidence={"policy": {**self.policy.as_dict(), "checksum": self.policy.checksum}},
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(c: sqlite3.Connection, m: PreModelAttemptManifestV1, a: PreModelArtifactV1) -> None:
        p = m.phase_input_evidence["policy"]
        u = {x.phase_key: x for x in m.upstream}
        if not isinstance(p, Mapping):
            raise FinalQcIntegrityError("QC policy missing")
        c.execute(
            """INSERT INTO final_qc_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,evaluated_at,phase_input_checksum,upstream_pdf_report_snapshot_id,upstream_pdf_report_checksum,upstream_infographic_snapshot_id,upstream_infographic_checksum,policy_json,policy_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.run_id,
                m.phase_attempt,
                m.requested_date,
                m.as_of_time.isoformat(),
                m.observed_at.isoformat(),
                m.phase_input_checksum,
                u["pdf_report"].snapshot_id,
                u["pdf_report"].checksum,
                u["infographic"].snapshot_id,
                u["infographic"].checksum,
                canonical_text(p),
                p["checksum"],
                m.outcome,
                m.snapshot_checksum,
                a.relpath,
                a.checksum,
                a.byte_count,
                canonical_text(list(m.warnings)),
                len(m.warnings),
                m.created_at.isoformat(),
                m.completed_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_checks(c: sqlite3.Connection, run_id: str, attempt: int, checks: tuple[FinalQcCheckV1, ...]) -> None:
        for q in checks:
            c.execute(
                "INSERT INTO final_qc_checks VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    attempt,
                    q.ordinal,
                    q.code,
                    int(q.passed),
                    canonical_text(q.observed),
                    q.source_checksum,
                    q.checksum,
                    canonical_text(q.as_dict()),
                ),
            )

    def persist_success(
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, u: FinalQcUpstreamV1, snapshot: FinalQcV1
    ) -> PersistedFinalQcV1:
        checks, replay = self.assess(u, evaluated_at=snapshot.evaluated_at)
        if replay is None or replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise FinalQcIntegrityError("Final QC success is not reproducible")
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            u=u,
            evaluated_at=snapshot.evaluated_at,
            input_checksum=phase_input_checksum,
            outcome=FinalQcAttemptOutcome.ASSEMBLED,
            snapshot=snapshot,
            warnings=(),
        )
        ma = None
        sa = None
        now = self._now()
        try:
            with self.database.connect() as c:
                self._active(c, run_id, phase_attempt)
            ma = publish_decision_manifest(m, self.artifact_root, "final_qc", secret_values=self.secret_values)
            sa = publish_snapshot(
                artifact_root=self.artifact_root,
                relpath=f"final_qc/snapshots/{snapshot.checksum}/final_qc_v1.json",
                payload=snapshot.as_dict(),
                content=snapshot.canonical_json_bytes(),
                secret_values=self.secret_values,
            )
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, m, ma)
                self._insert_checks(c, run_id, phase_attempt, checks)
                sid = f"final-qc:{snapshot.checksum}"
                c.execute(
                    """INSERT INTO final_qc_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,evaluated_at,contract_version,phase_input_checksum,policy_json,policy_checksum,upstream_pdf_report_snapshot_id,upstream_pdf_report_checksum,upstream_infographic_snapshot_id,upstream_infographic_checksum,qc_checksum,overall_result,check_count,canonical_json,artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        sid,
                        run_id,
                        phase_attempt,
                        snapshot.requested_date,
                        snapshot.as_of_time.isoformat(),
                        snapshot.evaluated_at.isoformat(),
                        snapshot.contract_version,
                        phase_input_checksum,
                        canonical_text({**snapshot.policy.as_dict(), "checksum": snapshot.policy.checksum}),
                        snapshot.policy.checksum,
                        u.pdf.snapshot_id,
                        u.pdf.document.checksum,
                        u.infographic.snapshot_id,
                        u.infographic.document.checksum,
                        snapshot.checksum,
                        snapshot.overall_result,
                        len(checks),
                        snapshot.canonical_json_bytes().decode(),
                        sa.relpath,
                        sa.checksum,
                        sa.byte_count,
                        now.isoformat(),
                    ),
                )
                c.execute(
                    "UPDATE final_qc_snapshots SET sealed_at=? WHERE snapshot_id=?", (self._now().isoformat(), sid)
                )
        except sqlite3.IntegrityError as e:
            self._cleanup(sa, ma)
            raise FinalQcConflict("conflicting Final QC evidence") from e
        except Exception:
            self._cleanup(sa, ma)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def persist_failure(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        u: FinalQcUpstreamV1,
        evaluated_at: datetime,
        checks: tuple[FinalQcCheckV1, ...],
        warnings: tuple[Mapping[str, object], ...],
    ) -> FinalQcAttemptEvidenceV1:
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            u=u,
            evaluated_at=evaluated_at,
            input_checksum=phase_input_checksum,
            outcome=FinalQcAttemptOutcome.VALIDATION_FAILED,
            snapshot=None,
            warnings=warnings,
        )
        a = publish_decision_manifest(m, self.artifact_root, "final_qc", secret_values=self.secret_values)
        try:
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, m, a)
                self._insert_checks(c, run_id, phase_attempt, checks)
        except Exception:
            cleanup_owned_artifact(self.artifact_root, a)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _cleanup(self, *items: PreModelArtifactV1 | None) -> None:
        for a in items:
            if a:
                cleanup_owned_artifact(self.artifact_root, a)

    def _attempt_row(self, run_id: str, attempt: int) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM final_qc_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if r is None:
            raise FinalQcNotFoundError("Final QC attempt not found")
        return r

    def _checks(self, run_id: str, attempt: int) -> tuple[FinalQcCheckV1, ...]:
        with self.database.connect() as c:
            rows = c.execute(
                "SELECT canonical_json,check_checksum FROM final_qc_checks WHERE run_id=? AND phase_attempt=? ORDER BY ordinal",
                (run_id, attempt),
            ).fetchall()
        values = []
        for r in rows:
            p = json.loads(str(r["canonical_json"]))
            q = FinalQcCheckV1(
                int(p["ordinal"]), str(p["code"]), bool(p["passed"]), p["observed"], str(p["source_checksum"])
            )
            if q.checksum != str(r["check_checksum"]):
                raise FinalQcIntegrityError("QC check checksum mismatch")
            values.append(q)
        return tuple(values)

    def _manifest_from_row(self, r: sqlite3.Row) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=FINAL_QC_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="final_qc",
            run_id=str(r["run_id"]),
            phase_attempt=int(r["phase_attempt"]),
            requested_date=str(r["requested_date"]),
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=_time(r["evaluated_at"], "evaluated_at"),
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "pdf_report", str(r["upstream_pdf_report_snapshot_id"]), str(r["upstream_pdf_report_checksum"])
                ),
                PreModelUpstreamIdentityV1(
                    "infographic", str(r["upstream_infographic_snapshot_id"]), str(r["upstream_infographic_checksum"])
                ),
            ),
            outcome=str(r["outcome"]),
            snapshot_checksum=None if r["snapshot_checksum"] is None else str(r["snapshot_checksum"]),
            warnings=tuple(dict(x) for x in json.loads(str(r["warnings_json"]))),
            created_at=_time(r["created_at"], "created_at"),
            phase_input_evidence={
                "policy": {**_policy(r["policy_json"]).as_dict(), "checksum": str(r["policy_checksum"])}
            },
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> PreModelAttemptManifestV1:
        r = self._attempt_row(run_id, attempt)
        m = self._manifest_from_row(r)
        verify_decision_manifest(
            m,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
            self.artifact_root,
            secret_values=self.secret_values,
        )
        return m

    def get_attempt_evidence(self, run_id: str, attempt: int) -> FinalQcAttemptEvidenceV1:
        r = self._attempt_row(run_id, attempt)
        m = self.get_attempt_manifest(run_id, attempt)
        return FinalQcAttemptEvidenceV1(
            m.run_id,
            m.phase_attempt,
            FinalQcAttemptOutcome(m.outcome),
            m.snapshot_checksum,
            m.phase_input_checksum,
            self._checks(m.run_id, m.phase_attempt),
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
        )

    def _row(self, where: str, v: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(f"SELECT * FROM final_qc_snapshots WHERE {where}", v).fetchone()
        if r is None or r["sealed_at"] is None:
            raise FinalQcNotFoundError("sealed Final QC snapshot not found")
        return r

    def _verify(self, r: sqlite3.Row) -> PersistedFinalQcV1:
        checks = self._checks(str(r["run_id"]), int(r["phase_attempt"]))
        s = FinalQcV1(
            str(r["run_id"]),
            str(r["requested_date"]),
            _time(r["as_of_time"], "as_of_time"),
            _time(r["evaluated_at"], "evaluated_at"),
            _policy(r["policy_json"]),
            str(r["upstream_pdf_report_snapshot_id"]),
            str(r["upstream_pdf_report_checksum"]),
            str(r["upstream_infographic_snapshot_id"]),
            str(r["upstream_infographic_checksum"]),
            checks,
            secret_values=self.secret_values,
        )
        if s.checksum != str(r["qc_checksum"]) or s.canonical_json_bytes().decode() != str(r["canonical_json"]):
            raise FinalQcIntegrityError("QC canonical reconstruction mismatch")
        u = FinalQcUpstreamV1(
            self.pdf.get_by_snapshot_id(s.upstream_pdf_report_snapshot_id),
            self.infographic.get_by_snapshot_id(s.upstream_infographic_snapshot_id),
        )
        _, replay = self.assess(u, evaluated_at=s.evaluated_at)
        if replay is None or replay.canonical_json_bytes() != s.canonical_json_bytes():
            raise FinalQcIntegrityError("historical QC replay mismatch")
        a = PreModelArtifactV1(str(r["artifact_relpath"]), str(r["artifact_checksum"]), int(r["artifact_byte_count"]))
        verify_snapshot(
            artifact_root=self.artifact_root,
            artifact=a,
            expected_relpath=f"final_qc/snapshots/{s.checksum}/final_qc_v1.json",
            payload=s.as_dict(),
            content=s.canonical_json_bytes(),
            secret_values=self.secret_values,
        )
        self.get_attempt_manifest(str(r["run_id"]), int(r["phase_attempt"]))
        return PersistedFinalQcV1(
            str(r["snapshot_id"]),
            str(r["run_id"]),
            int(r["phase_attempt"]),
            s,
            a,
            str(r["phase_input_checksum"]),
            _time(r["created_at"], "created_at"),
            _time(r["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedFinalQcV1:
        return self._verify(self._row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedFinalQcV1:
        return self._verify(self._row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedFinalQcV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM final_qc_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._verify(r)
