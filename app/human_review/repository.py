from __future__ import annotations
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from app.database import Database
from app.decision_evidence import create_decision_manifest, publish_decision_manifest, verify_decision_manifest
from app.final_qc.repository import FinalQcRepository, PersistedFinalQcV1
from app.human_review.contracts import (
    HUMAN_REVIEW_ATTEMPT_MANIFEST_CONTRACT,
    HumanReviewDecision,
    HumanReviewRecordV1,
    HumanReviewTargetV1,
)
from app.identifiers import validate_run_id
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)


class HumanReviewRepositoryError(RuntimeError):
    pass


class HumanReviewNotFoundError(HumanReviewRepositoryError):
    pass


class HumanReviewConflict(HumanReviewRepositoryError):
    pass


class HumanReviewIntegrityError(HumanReviewRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedHumanReviewV1:
    review_id: str
    review_ordinal: int
    record: HumanReviewRecordV1
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HumanReviewAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    review: PersistedHumanReviewV1
    manifest: PreModelArtifactV1
    phase_input_checksum: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(v: object, n: str) -> datetime:
    try:
        return aware_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")), n)
    except ValueError as e:
        raise HumanReviewIntegrityError(f"persisted {n} invalid") from e


class HumanReviewRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.qc = FinalQcRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def show_target(self, run_id: str) -> HumanReviewTargetV1:
        qc = self.qc.get_latest_for_run(validate_run_id(run_id))
        if qc is None:
            raise HumanReviewNotFoundError("Human Review requires PASS Final QC")
        return self._target_from_qc(qc)

    def _target_from_qc(self, qc: PersistedFinalQcV1) -> HumanReviewTargetV1:
        pdf = self.qc.pdf.get_by_snapshot_id(qc.snapshot.upstream_pdf_report_snapshot_id)
        info = self.qc.infographic.get_by_snapshot_id(qc.snapshot.upstream_infographic_snapshot_id)
        return HumanReviewTargetV1(
            qc.run_id,
            pdf.document.requested_date,
            qc.snapshot_id,
            qc.snapshot.checksum,
            pdf.snapshot_id,
            pdf.document.checksum,
            pdf.artifacts.pdf.checksum,
            info.snapshot_id,
            info.document.checksum,
            {"feed": info.artifacts.feed.checksum, "story": info.artifacts.story.checksum},
        )

    def record_decision(
        self,
        *,
        run_id: str,
        reviewer_id: str,
        decision: HumanReviewDecision,
        notes: str | None = None,
        reviewed_at: datetime | None = None,
    ) -> PersistedHumanReviewV1:
        target = self.show_target(run_id)
        record = HumanReviewRecordV1(
            target,
            reviewer_id,
            decision,
            self._now() if reviewed_at is None else reviewed_at,
            notes,
            secret_values=self.secret_values,
        )
        rid = f"human-review:{record.checksum}"
        with self.database.connect(write=True) as c:
            existing = c.execute("SELECT * FROM human_review_records WHERE review_id=?", (rid,)).fetchone()
            if existing is not None:
                return self.get_by_review_id(rid)
            ordinal = int(
                c.execute(
                    "SELECT coalesce(max(review_ordinal),0)+1 FROM human_review_records WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            try:
                c.execute(
                    """INSERT INTO human_review_records(review_id,run_id,review_ordinal,requested_date,final_qc_snapshot_id,final_qc_checksum,pdf_report_snapshot_id,pdf_report_checksum,pdf_artifact_checksum,infographic_snapshot_id,infographic_checksum,infographic_artifact_checksums_json,reviewer_id,decision,reviewed_at,notes,review_checksum,canonical_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rid,
                        run_id,
                        ordinal,
                        target.requested_date,
                        target.final_qc_snapshot_id,
                        target.final_qc_checksum,
                        target.pdf_report_snapshot_id,
                        target.pdf_report_checksum,
                        target.pdf_artifact_checksum,
                        target.infographic_snapshot_id,
                        target.infographic_checksum,
                        canonical_text(dict(target.infographic_artifact_checksums)),
                        record.reviewer_id,
                        record.decision.value,
                        record.reviewed_at.isoformat(),
                        record.notes,
                        record.checksum,
                        record.canonical_json_bytes().decode(),
                        self._now().isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise HumanReviewConflict("conflicting Human Review evidence") from e
        return self.get_by_review_id(rid)

    def _from_row(self, r: sqlite3.Row) -> PersistedHumanReviewV1:
        target = HumanReviewTargetV1(
            str(r["run_id"]),
            str(r["requested_date"]),
            str(r["final_qc_snapshot_id"]),
            str(r["final_qc_checksum"]),
            str(r["pdf_report_snapshot_id"]),
            str(r["pdf_report_checksum"]),
            str(r["pdf_artifact_checksum"]),
            str(r["infographic_snapshot_id"]),
            str(r["infographic_checksum"]),
            {str(k): str(v) for k, v in json.loads(str(r["infographic_artifact_checksums_json"])).items()},
        )
        record = HumanReviewRecordV1(
            target,
            str(r["reviewer_id"]),
            HumanReviewDecision(str(r["decision"])),
            _time(r["reviewed_at"], "reviewed_at"),
            None if r["notes"] is None else str(r["notes"]),
            secret_values=self.secret_values,
        )
        if record.checksum != str(r["review_checksum"]) or record.canonical_json_bytes().decode() != str(
            r["canonical_json"]
        ):
            raise HumanReviewIntegrityError("Human Review canonical reconstruction mismatch")
        exact = self._target_from_qc(self.qc.get_by_snapshot_id(record.target.final_qc_snapshot_id))
        if exact != record.target:
            raise HumanReviewIntegrityError("Human Review target no longer reconciles")
        return PersistedHumanReviewV1(
            str(r["review_id"]), int(r["review_ordinal"]), record, _time(r["created_at"], "created_at")
        )

    def get_by_review_id(self, review_id: str) -> PersistedHumanReviewV1:
        with self.database.connect() as c:
            r = c.execute("SELECT * FROM human_review_records WHERE review_id=?", (review_id,)).fetchone()
        if r is None:
            raise HumanReviewNotFoundError("Human Review record not found")
        return self._from_row(r)

    def get_pending_for_run(self, run_id: str) -> PersistedHumanReviewV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                """SELECT h.* FROM human_review_records h LEFT JOIN human_review_attempt_evidence a ON a.review_id=h.review_id WHERE h.run_id=? AND a.review_id IS NULL ORDER BY h.review_ordinal LIMIT 1""",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._from_row(r)

    @staticmethod
    def _active(c: sqlite3.Connection, run_id: str, attempt: int) -> None:
        r = c.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='human_review'",
            (run_id,),
        ).fetchone()
        if r is None or str(r["status"]) != "running" or int(r["attempt_count"]) != attempt:
            raise HumanReviewIntegrityError("Human Review attempt is not active")

    def persist_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        review: PersistedHumanReviewV1,
        as_of_time: datetime,
    ) -> HumanReviewAttemptEvidenceV1:
        t = review.record.target
        m = create_decision_manifest(
            contract_version=HUMAN_REVIEW_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="human_review",
            run_id=run_id,
            phase_attempt=phase_attempt,
            requested_date=t.requested_date,
            as_of_time=as_of_time,
            observed_at=review.record.reviewed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=(
                PreModelUpstreamIdentityV1("final_qc", t.final_qc_snapshot_id, t.final_qc_checksum),
                PreModelUpstreamIdentityV1("pdf_report", t.pdf_report_snapshot_id, t.pdf_report_checksum),
                PreModelUpstreamIdentityV1("infographic", t.infographic_snapshot_id, t.infographic_checksum),
            ),
            outcome="assembled",
            snapshot_checksum=review.record.checksum,
            warnings=(),
            created_at=self._now(),
            phase_input_evidence={
                "decision": review.record.decision.value,
                "review_checksum": review.record.checksum,
                "review_id": review.review_id,
            },
            secret_values=self.secret_values,
        )
        a = publish_decision_manifest(m, self.artifact_root, "human_review", secret_values=self.secret_values)
        try:
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                existing = c.execute(
                    "SELECT * FROM human_review_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                    (run_id, phase_attempt),
                ).fetchone()
                if existing is not None:
                    expected = (
                        review.review_id,
                        review.record.checksum,
                        phase_input_checksum,
                        a.relpath,
                        a.checksum,
                        a.byte_count,
                    )
                    actual = (
                        str(existing["review_id"]),
                        str(existing["review_checksum"]),
                        str(existing["phase_input_checksum"]),
                        str(existing["evidence_manifest_relpath"]),
                        str(existing["evidence_manifest_checksum"]),
                        int(existing["evidence_manifest_byte_count"]),
                    )
                    if actual != expected:
                        raise HumanReviewConflict("conflicting Human Review attempt replay")
                else:
                    c.execute(
                        """INSERT INTO human_review_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,completed_at,phase_input_checksum,final_qc_snapshot_id,final_qc_checksum,review_id,review_checksum,decision,outcome,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            run_id,
                            phase_attempt,
                            t.requested_date,
                            as_of_time.isoformat(),
                            review.record.reviewed_at.isoformat(),
                            phase_input_checksum,
                            t.final_qc_snapshot_id,
                            t.final_qc_checksum,
                            review.review_id,
                            review.record.checksum,
                            review.record.decision.value,
                            "assembled",
                            a.relpath,
                            a.checksum,
                            a.byte_count,
                            m.created_at.isoformat(),
                        ),
                    )
        except Exception:
            cleanup_owned_artifact(self.artifact_root, a)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def get_attempt_evidence(self, run_id: str, phase_attempt: int) -> HumanReviewAttemptEvidenceV1:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM human_review_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), phase_attempt),
            ).fetchone()
        if r is None:
            raise HumanReviewNotFoundError("Human Review attempt not found")
        review = self.get_by_review_id(str(r["review_id"]))
        t = review.record.target
        m = create_decision_manifest(
            contract_version=HUMAN_REVIEW_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="human_review",
            run_id=run_id,
            phase_attempt=phase_attempt,
            requested_date=t.requested_date,
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=review.record.reviewed_at,
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1("final_qc", t.final_qc_snapshot_id, t.final_qc_checksum),
                PreModelUpstreamIdentityV1("pdf_report", t.pdf_report_snapshot_id, t.pdf_report_checksum),
                PreModelUpstreamIdentityV1("infographic", t.infographic_snapshot_id, t.infographic_checksum),
            ),
            outcome="assembled",
            snapshot_checksum=review.record.checksum,
            warnings=(),
            created_at=_time(r["created_at"], "created_at"),
            phase_input_evidence={
                "decision": review.record.decision.value,
                "review_checksum": review.record.checksum,
                "review_id": review.review_id,
            },
            secret_values=self.secret_values,
        )
        a = PreModelArtifactV1(
            str(r["evidence_manifest_relpath"]),
            str(r["evidence_manifest_checksum"]),
            int(r["evidence_manifest_byte_count"]),
        )
        verify_decision_manifest(m, a, self.artifact_root, secret_values=self.secret_values)
        return HumanReviewAttemptEvidenceV1(run_id, phase_attempt, review, a, str(r["phase_input_checksum"]))
