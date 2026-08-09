from __future__ import annotations
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.final_qc.contracts import FINAL_QC_PHASE_INPUT_CONTRACT, FinalQcPolicyV1
from app.final_qc.repository import FinalQcRepository
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class FinalQcPhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FinalQcPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: FinalQcRepository | None = None,
        policy: FinalQcPolicyV1 = FinalQcPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.repository = repository or FinalQcRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def __call__(self, c: PhaseExecutionContext) -> PhaseExecutionResult:
        if c.phase_key is not PipelinePhaseKey.FINAL_QC:
            raise FinalQcPhaseHandlerError("handler requires FINAL_QC")
        if isinstance(c.attempt_number, bool) or not isinstance(c.attempt_number, int) or c.attempt_number < 1:
            raise FinalQcPhaseHandlerError("phase attempt must be positive")
        validate_run_id(c.run_id)
        parse_requested_date(c.requested_date)
        asof = datetime.fromisoformat(c.as_of_time.replace("Z", "+00:00"))
        evaluated = self.clock()
        if asof.tzinfo is None or asof.utcoffset() is None or evaluated.tzinfo is None or evaluated.utcoffset() is None:
            raise FinalQcPhaseHandlerError("QC timestamps must be aware")
        asof = asof.astimezone(timezone.utc)
        evaluated = evaluated.astimezone(timezone.utc)
        u = self.repository.resolve_upstream(c.run_id)
        checksum = canonical_sha256(
            {
                "as_of_time": asof.isoformat(),
                "contract_version": FINAL_QC_PHASE_INPUT_CONTRACT,
                "evaluated_at": evaluated.isoformat(),
                "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
                "requested_date": c.requested_date,
                "upstream_infographic": [u.infographic.snapshot_id, u.infographic.document.checksum],
                "upstream_pdf_report": [u.pdf.snapshot_id, u.pdf.document.checksum],
            }
        )
        if (
            u.pdf.document.requested_date != c.requested_date
            or u.pdf.document.as_of_time != asof
            or evaluated < u.infographic.document.generated_at
        ):
            raise FinalQcPhaseHandlerError("Final QC context boundary mismatch")
        checks, snapshot = self.repository.assess(u, evaluated_at=evaluated)
        if snapshot is None:
            failed = tuple(q.code for q in checks if not q.passed)
            error = FinalQcPhaseHandlerError("Final QC failed: " + ",".join(failed))
            warnings = (
                {
                    "code": "final_qc_validation_failed",
                    "error_type": type(error).__name__,
                    "message": redact_text(str(error), self.secret_values),
                },
            )
            try:
                self.repository.persist_failure(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=checksum,
                    u=u,
                    evaluated_at=evaluated,
                    checks=checks,
                    warnings=warnings,
                )
            except Exception as x:
                error.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise error
        persisted = self.repository.persist_success(
            run_id=c.run_id, phase_attempt=c.attempt_number, phase_input_checksum=checksum, u=u, snapshot=snapshot
        )
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED,
            input_checksum=checksum,
            output_checksum=persisted.snapshot.checksum,
            artifact_relpath=persisted.artifact.relpath,
            continue_pipeline=True,
        )
