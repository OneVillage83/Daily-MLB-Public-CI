from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.infographic.artifact import infographic_artifact_bytes
from app.infographic.contracts import INFOGRAPHIC_PHASE_INPUT_CONTRACT
from app.infographic.policy import InfographicPolicyV1
from app.infographic.repository import InfographicAttemptOutcome, InfographicRepository
from app.pdf_report.repository import PersistedPdfReportV1
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class InfographicPhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InfographicPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: InfographicRepository | None = None,
        policy: InfographicPolicyV1 = InfographicPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.repository = repository or InfographicRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def _warning(self, code: str, e: Exception) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "code": code,
                "error_type": type(e).__name__,
                "message": redact_text(str(e), self.secret_values).strip() or "Infographic failed",
            },
        )

    def _fail(
        self,
        c: PhaseExecutionContext,
        checksum: str,
        generated: datetime,
        outcome: InfographicAttemptOutcome,
        pdf: PersistedPdfReportV1,
        code: str,
        e: Exception,
    ) -> None:
        try:
            self.repository.persist_failed_attempt(
                run_id=c.run_id,
                phase_attempt=c.attempt_number,
                phase_input_checksum=checksum,
                generated_at=generated,
                outcome=outcome,
                pdf=pdf,
                warnings=self._warning(code, e),
            )
        except Exception as x:
            e.add_note(f"failed-attempt evidence error: {type(x).__name__}")

    def __call__(self, c: PhaseExecutionContext) -> PhaseExecutionResult:
        if c.phase_key is not PipelinePhaseKey.INFOGRAPHIC:
            raise InfographicPhaseHandlerError("handler requires INFOGRAPHIC")
        if isinstance(c.attempt_number, bool) or not isinstance(c.attempt_number, int) or c.attempt_number < 1:
            raise InfographicPhaseHandlerError("phase attempt must be positive")
        validate_run_id(c.run_id)
        parse_requested_date(c.requested_date)
        asof = datetime.fromisoformat(c.as_of_time.replace("Z", "+00:00"))
        if asof.tzinfo is None or asof.utcoffset() is None:
            raise InfographicPhaseHandlerError("context as_of_time must be aware")
        asof = asof.astimezone(timezone.utc)
        pdf = self.repository.resolve_upstream(c.run_id)
        generated = self.clock()
        if generated.tzinfo is None or generated.utcoffset() is None:
            raise InfographicPhaseHandlerError("handler clock must be aware")
        generated = generated.astimezone(timezone.utc)
        checksum = canonical_sha256(
            {
                "as_of_time": asof.isoformat(),
                "contract_version": INFOGRAPHIC_PHASE_INPUT_CONTRACT,
                "generated_at": generated.isoformat(),
                "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
                "requested_date": c.requested_date,
                "upstream_pdf_report_checksum": pdf.document.checksum,
                "upstream_pdf_report_snapshot_id": pdf.snapshot_id,
            }
        )
        try:
            if (
                pdf.document.requested_date != c.requested_date
                or pdf.document.as_of_time != asof
                or generated < pdf.document.generated_at
            ):
                raise InfographicPhaseHandlerError("context or generation boundary mismatch")
        except Exception as e:
            self._fail(
                c, checksum, generated, InfographicAttemptOutcome.INPUT_FAILED, pdf, "infographic_input_failed", e
            )
            raise
        try:
            snapshot = self.repository.assemble(pdf, generated_at=generated)
        except Exception as e:
            self._fail(
                c, checksum, generated, InfographicAttemptOutcome.ASSEMBLY_FAILED, pdf, "infographic_assembly_failed", e
            )
            raise
        try:
            infographic_artifact_bytes(snapshot)
        except Exception as e:
            self._fail(
                c,
                checksum,
                generated,
                InfographicAttemptOutcome.RENDERING_FAILED,
                pdf,
                "infographic_rendering_failed",
                e,
            )
            raise
        try:
            persisted = self.repository.persist_assembly(
                run_id=c.run_id, phase_attempt=c.attempt_number, phase_input_checksum=checksum, snapshot=snapshot
            )
        except Exception as e:
            self._fail(
                c,
                checksum,
                generated,
                InfographicAttemptOutcome.PERSISTENCE_FAILED,
                pdf,
                "infographic_persistence_failed",
                e,
            )
            raise
        warnings = [dict(v) for v in persisted.document.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if warnings else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=checksum,
            output_checksum=persisted.document.checksum,
            artifact_relpath=persisted.artifacts.feed.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )
