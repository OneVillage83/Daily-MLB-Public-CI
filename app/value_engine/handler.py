from __future__ import annotations
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.value_engine.production import VALUE_ENGINE_PHASE_INPUT_CONTRACT, ValuePolicyV1
from app.value_engine.repository import ValueEngineAttemptOutcome, ValueEngineRepository


class ValueEnginePhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _context(c: PhaseExecutionContext, key: PipelinePhaseKey) -> datetime:
    if c.phase_key is not key:
        raise ValueEnginePhaseHandlerError(f"handler requires {key.name} phase")
    if isinstance(c.attempt_number, bool) or not isinstance(c.attempt_number, int) or c.attempt_number < 1:
        raise ValueEnginePhaseHandlerError("phase attempt must be positive")
    validate_run_id(c.run_id)
    parse_requested_date(c.requested_date)
    v = datetime.fromisoformat(c.as_of_time.replace("Z", "+00:00"))
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueEnginePhaseHandlerError("context as_of_time must be aware")
    return v.astimezone(timezone.utc)


class ValueEnginePhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: ValueEngineRepository | None = None,
        policy: ValuePolicyV1 = ValuePolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.repository = repository or ValueEngineRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def _warning(self, code: str, e: Exception) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "code": code,
                "error_type": type(e).__name__,
                "message": redact_text(str(e), self.secret_values).strip() or "Value Engine failed",
            },
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        c = context
        asof = _context(c, PipelinePhaseKey.VALUE_ENGINE)
        u = self.repository.resolve_upstream(c.run_id)
        evaluated = self.clock()
        if evaluated.tzinfo is None or evaluated.utcoffset() is None:
            raise ValueEnginePhaseHandlerError("handler clock must be timezone-aware")
        evaluated = evaluated.astimezone(timezone.utc)
        contexts, checksums = self.repository._market_inputs(u)
        inventory = canonical_sha256(
            [
                {"market_context_checksum": checksums[g.source_game_id], "source_game_id": g.source_game_id}
                for g in u.predictions.predictions.games
            ]
        )
        input_checksum = canonical_sha256(
            {
                "as_of_time": asof.isoformat(),
                "contract_version": VALUE_ENGINE_PHASE_INPUT_CONTRACT,
                "evaluated_at": evaluated.isoformat(),
                "market_inventory_checksum": inventory,
                "policy": self.policy.as_dict(),
                "requested_date": c.requested_date,
                "upstream_data_quality_checksum": u.data_quality.snapshot.checksum,
                "upstream_data_quality_snapshot_id": u.data_quality.snapshot_id,
                "upstream_model_feature_set_checksum": u.model_feature_set.feature_set.checksum,
                "upstream_model_feature_set_snapshot_id": u.model_feature_set.snapshot_id,
                "upstream_predictions_checksum": u.predictions.predictions.checksum,
                "upstream_predictions_snapshot_id": u.predictions.snapshot_id,
            }
        )
        try:
            if (
                u.predictions.predictions.requested_date != c.requested_date
                or u.predictions.predictions.as_of_time != asof
                or evaluated < u.predictions.predictions.observed_at
            ):
                raise ValueEnginePhaseHandlerError("context or evaluation boundary does not match Predictions")
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    evaluated_at=evaluated,
                    outcome=ValueEngineAttemptOutcome.INPUT_FAILED,
                    upstream=u,
                    market_inventory_checksum=inventory,
                    warnings=self._warning("value_engine_input_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        try:
            s = self.repository.calculate(u, evaluated_at=evaluated)
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    evaluated_at=evaluated,
                    outcome=ValueEngineAttemptOutcome.CALCULATION_FAILED,
                    upstream=u,
                    market_inventory_checksum=inventory,
                    warnings=self._warning("value_engine_calculation_failed", e),
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
                    outcome=ValueEngineAttemptOutcome.PERSISTENCE_FAILED,
                    upstream=u,
                    market_inventory_checksum=inventory,
                    warnings=self._warning("value_engine_persistence_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        w = [dict(v) for v in p.value_engine.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if w else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=input_checksum,
            output_checksum=p.value_engine.checksum,
            artifact_relpath=p.artifact.relpath,
            warnings=w or None,
            continue_pipeline=True,
        )
