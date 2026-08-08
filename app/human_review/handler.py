from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.human_review.contracts import HUMAN_REVIEW_PHASE_INPUT_CONTRACT
from app.human_review.repository import HumanReviewRepository
from app.identifiers import parse_requested_date, validate_run_id
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class HumanReviewPhaseHandlerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HumanReviewPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        repository: HumanReviewRepository | None = None,
    ) -> None:
        self.clock = clock
        self.repository = repository or HumanReviewRepository(
            database,
            artifact_root=artifact_root,
            secret_values=secret_values,
            clock=clock,
        )

    def awaiting_input(self, run_id: str) -> bool:
        return self.repository.get_pending_for_run(validate_run_id(run_id)) is None

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        if context.phase_key is not PipelinePhaseKey.HUMAN_REVIEW:
            raise HumanReviewPhaseHandlerError("handler requires HUMAN_REVIEW")
        if (
            isinstance(context.attempt_number, bool)
            or not isinstance(context.attempt_number, int)
            or context.attempt_number < 1
        ):
            raise HumanReviewPhaseHandlerError("phase attempt must be positive")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        as_of_time = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        if as_of_time.tzinfo is None or as_of_time.utcoffset() is None:
            raise HumanReviewPhaseHandlerError("as_of_time must be timezone-aware")
        as_of_time = as_of_time.astimezone(timezone.utc)
        review = self.repository.get_pending_for_run(context.run_id)
        if review is None:
            raise HumanReviewPhaseHandlerError("explicit Human Review evidence is absent")
        target = review.record.target
        if target.requested_date != context.requested_date:
            raise HumanReviewPhaseHandlerError("Human Review requested-date mismatch")
        input_checksum = canonical_sha256(
            {
                "as_of_time": as_of_time.isoformat(),
                "contract_version": HUMAN_REVIEW_PHASE_INPUT_CONTRACT,
                "decision": review.record.decision.value,
                "final_qc": [target.final_qc_snapshot_id, target.final_qc_checksum],
                "infographic": [
                    target.infographic_snapshot_id,
                    target.infographic_checksum,
                    dict(target.infographic_artifact_checksums),
                ],
                "pdf_report": [
                    target.pdf_report_snapshot_id,
                    target.pdf_report_checksum,
                    target.pdf_artifact_checksum,
                ],
                "requested_date": context.requested_date,
                "review_checksum": review.record.checksum,
                "review_id": review.review_id,
            }
        )
        persisted = self.repository.persist_attempt(
            run_id=context.run_id,
            phase_attempt=context.attempt_number,
            phase_input_checksum=input_checksum,
            review=review,
            as_of_time=as_of_time,
        )
        rejected = persisted.review.record.decision.value == "reject"
        return PhaseExecutionResult(
            status=(PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if rejected else PipelinePhaseStatus.SUCCEEDED),
            input_checksum=input_checksum,
            output_checksum=persisted.review.record.checksum,
            artifact_relpath=persisted.manifest.relpath,
            warnings=({"decision": "reject", "message": "human reviewer rejected output"} if rejected else None),
            continue_pipeline=True,
        )
