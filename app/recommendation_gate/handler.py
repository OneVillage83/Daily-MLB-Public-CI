from __future__ import annotations
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text
from app.recommendation_gate.production import RECOMMENDATION_GATE_PHASE_INPUT_CONTRACT, RecommendationPolicyV1
from app.recommendation_gate.repository import RecommendationGateAttemptOutcome, RecommendationGateRepository
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class RecommendationGatePhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RecommendationGatePhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: RecommendationGateRepository | None = None,
        policy: RecommendationPolicyV1 = RecommendationPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.repository = repository or RecommendationGateRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def _warning(self, code: str, e: Exception) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "code": code,
                "error_type": type(e).__name__,
                "message": redact_text(str(e), self.secret_values).strip() or "Recommendation Gate failed",
            },
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        c = context
        if c.phase_key is not PipelinePhaseKey.RECOMMENDATION_GATE:
            raise RecommendationGatePhaseHandlerError("handler requires RECOMMENDATION_GATE")
        if isinstance(c.attempt_number, bool) or not isinstance(c.attempt_number, int) or c.attempt_number < 1:
            raise RecommendationGatePhaseHandlerError("phase attempt must be positive")
        validate_run_id(c.run_id)
        parse_requested_date(c.requested_date)
        asof = datetime.fromisoformat(c.as_of_time.replace("Z", "+00:00"))
        if asof.tzinfo is None or asof.utcoffset() is None:
            raise RecommendationGatePhaseHandlerError("context as_of_time must be aware")
        asof = asof.astimezone(timezone.utc)
        u = self.repository.resolve_upstream(c.run_id)
        evaluated = self.clock()
        if evaluated.tzinfo is None or evaluated.utcoffset() is None:
            raise RecommendationGatePhaseHandlerError("handler clock must be timezone-aware")
        evaluated = evaluated.astimezone(timezone.utc)
        input_checksum = canonical_sha256(
            {
                "as_of_time": asof.isoformat(),
                "contract_version": RECOMMENDATION_GATE_PHASE_INPUT_CONTRACT,
                "evaluated_at": evaluated.isoformat(),
                "policy": self.policy.as_dict(),
                "requested_date": c.requested_date,
                "upstream_data_quality_checksum": u.quality.snapshot.checksum,
                "upstream_data_quality_snapshot_id": u.quality.snapshot_id,
                "upstream_predictions_checksum": u.predictions.predictions.checksum,
                "upstream_predictions_snapshot_id": u.predictions.snapshot_id,
                "upstream_value_checksum": u.value.value_engine.checksum,
                "upstream_value_snapshot_id": u.value.snapshot_id,
            }
        )
        try:
            if (
                u.value.value_engine.requested_date != c.requested_date
                or u.value.value_engine.as_of_time != asof
                or evaluated < u.value.value_engine.evaluated_at
            ):
                raise RecommendationGatePhaseHandlerError("context or evaluation boundary mismatch")
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    evaluated_at=evaluated,
                    outcome=RecommendationGateAttemptOutcome.INPUT_FAILED,
                    upstream=u,
                    warnings=self._warning("recommendation_gate_input_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        try:
            s = self.repository.evaluate(u, evaluated_at=evaluated)
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    evaluated_at=evaluated,
                    outcome=RecommendationGateAttemptOutcome.EVALUATION_FAILED,
                    upstream=u,
                    warnings=self._warning("recommendation_gate_evaluation_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        try:
            p = self.repository.persist_assembly(
                run_id=c.run_id, phase_attempt=c.attempt_number, phase_input_checksum=input_checksum, snapshot=s
            )
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    evaluated_at=evaluated,
                    outcome=RecommendationGateAttemptOutcome.PERSISTENCE_FAILED,
                    upstream=u,
                    warnings=self._warning("recommendation_gate_persistence_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        w = [dict(v) for v in p.gate.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if w else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=input_checksum,
            output_checksum=p.gate.checksum,
            artifact_relpath=p.artifact.relpath,
            warnings=w or None,
            continue_pipeline=True,
        )
