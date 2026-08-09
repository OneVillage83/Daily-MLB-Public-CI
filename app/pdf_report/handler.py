from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.pdf_report.artifact import production_pdf_artifact_bytes
from app.pdf_report.policy import PdfReportPolicyV1
from app.pdf_report.production import PDF_REPORT_PHASE_INPUT_CONTRACT
from app.pdf_report.repository import PdfReportAttemptOutcome, PdfReportRepository, PdfReportUpstreamV1
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class PdfReportPhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PdfReportPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: PdfReportRepository | None = None,
        policy: PdfReportPolicyV1 = PdfReportPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.policy = policy
        self.repository = repository or PdfReportRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def _warnings(self, code: str, exc: Exception) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "code": code,
                "error_type": type(exc).__name__,
                "message": redact_text(str(exc), self.secret_values).strip() or "PDF Report failed",
            },
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        if context.phase_key is not PipelinePhaseKey.PDF_REPORT:
            raise PdfReportPhaseHandlerError("handler requires PDF_REPORT")
        if (
            isinstance(context.attempt_number, bool)
            or not isinstance(context.attempt_number, int)
            or context.attempt_number < 1
        ):
            raise PdfReportPhaseHandlerError("phase attempt must be positive")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        as_of = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise PdfReportPhaseHandlerError("context as_of_time must be aware")
        as_of = as_of.astimezone(timezone.utc)
        upstream = self.repository.resolve_upstream(context.run_id)
        generated_at = self.clock()
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise PdfReportPhaseHandlerError("handler clock must be timezone-aware")
        generated_at = generated_at.astimezone(timezone.utc)
        input_checksum = canonical_sha256(
            {
                "as_of_time": as_of.isoformat(),
                "contract_version": PDF_REPORT_PHASE_INPUT_CONTRACT,
                "generated_at": generated_at.isoformat(),
                "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
                "requested_date": context.requested_date,
                "upstream": {
                    "rankings": [upstream.rankings.snapshot_id, upstream.rankings.rankings.checksum],
                    "recommendation_gate": [upstream.gate.snapshot_id, upstream.gate.gate.checksum],
                    "value_engine": [upstream.value.snapshot_id, upstream.value.value_engine.checksum],
                    "predictions": [upstream.predictions.snapshot_id, upstream.predictions.predictions.checksum],
                    "matchup_packet": [upstream.matchup_packet.snapshot_id, upstream.matchup_packet.packet.checksum],
                    "data_quality": [upstream.data_quality.snapshot_id, upstream.data_quality.snapshot.checksum],
                },
            }
        )
        try:
            if (
                upstream.rankings.rankings.requested_date != context.requested_date
                or upstream.rankings.rankings.as_of_time != as_of
                or generated_at < upstream.rankings.rankings.ranked_at
            ):
                raise PdfReportPhaseHandlerError("context or report generation boundary mismatch")
        except Exception as exc:
            self._persist_failure(
                context,
                input_checksum,
                generated_at,
                PdfReportAttemptOutcome.INPUT_FAILED,
                upstream,
                "pdf_report_input_failed",
                exc,
            )
            raise
        try:
            snapshot = self.repository.assemble(upstream, generated_at=generated_at)
        except Exception as exc:
            self._persist_failure(
                context,
                input_checksum,
                generated_at,
                PdfReportAttemptOutcome.ASSEMBLY_FAILED,
                upstream,
                "pdf_report_assembly_failed",
                exc,
            )
            raise
        try:
            production_pdf_artifact_bytes(snapshot)
        except Exception as exc:
            self._persist_failure(
                context,
                input_checksum,
                generated_at,
                PdfReportAttemptOutcome.RENDERING_FAILED,
                upstream,
                "pdf_report_rendering_failed",
                exc,
            )
            raise
        try:
            persisted = self.repository.persist_assembly(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=input_checksum,
                snapshot=snapshot,
            )
        except Exception as exc:
            self._persist_failure(
                context,
                input_checksum,
                generated_at,
                PdfReportAttemptOutcome.PERSISTENCE_FAILED,
                upstream,
                "pdf_report_persistence_failed",
                exc,
            )
            raise
        warnings = [dict(item) for item in persisted.document.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if warnings else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=input_checksum,
            output_checksum=persisted.document.checksum,
            artifact_relpath=persisted.artifacts.pdf.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )

    def _persist_failure(
        self,
        context: PhaseExecutionContext,
        checksum: str,
        generated_at: datetime,
        outcome: PdfReportAttemptOutcome,
        upstream: PdfReportUpstreamV1,
        code: str,
        exc: Exception,
    ) -> None:
        try:
            self.repository.persist_failed_attempt(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=checksum,
                generated_at=generated_at,
                outcome=outcome,
                upstream=upstream,
                warnings=self._warnings(code, exc),
            )
        except Exception as evidence_error:
            exc.add_note(f"failed-attempt evidence error: {type(evidence_error).__name__}")
