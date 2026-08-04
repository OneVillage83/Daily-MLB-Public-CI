from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.predictions.production import PREDICTIONS_PHASE_INPUT_CONTRACT, PredictionProviderPolicyV1
from app.predictions.repository import (
    PredictionsAttemptOutcome,
    PredictionsRepository,
    PredictionsUpstreamV1,
)
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class PredictionsPhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PredictionsPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: PredictionsRepository | None = None,
        provider_policy: PredictionProviderPolicyV1 = PredictionProviderPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.provider_policy = provider_policy
        self.repository = repository or PredictionsRepository(
            database,
            artifact_root=artifact_root,
            secret_values=self.secret_values,
            clock=clock,
            provider_policy=provider_policy,
        )

    @staticmethod
    def _context(context: PhaseExecutionContext) -> datetime:
        if context.phase_key is not PipelinePhaseKey.PREDICTIONS:
            raise PredictionsPhaseHandlerError("handler requires PREDICTIONS phase")
        if (
            isinstance(context.attempt_number, bool)
            or not isinstance(context.attempt_number, int)
            or context.attempt_number < 1
        ):
            raise PredictionsPhaseHandlerError("phase attempt must be a positive integer")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        try:
            value = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PredictionsPhaseHandlerError("context as_of_time is invalid") from exc
        if value.tzinfo is None or value.utcoffset() is None:
            raise PredictionsPhaseHandlerError("context as_of_time must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _observed(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise PredictionsPhaseHandlerError("handler clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _input_checksum(
        self,
        context: PhaseExecutionContext,
        as_of: datetime,
        observed_at: datetime,
        upstream: PredictionsUpstreamV1,
        inventory_checksum: str,
    ) -> str:
        return canonical_sha256(
            {
                "as_of_time": as_of.isoformat(),
                "contract_version": PREDICTIONS_PHASE_INPUT_CONTRACT,
                "input_inventory_checksum": inventory_checksum,
                "observed_at": observed_at.isoformat(),
                "provider_policy": self.provider_policy.as_dict(),
                "requested_date": context.requested_date,
                "upstream_data_quality_checksum": upstream.data_quality.snapshot.checksum,
                "upstream_data_quality_snapshot_id": upstream.data_quality.snapshot_id,
                "upstream_model_feature_set_checksum": upstream.model_feature_set.feature_set.checksum,
                "upstream_model_feature_set_snapshot_id": upstream.model_feature_set.snapshot_id,
            }
        )

    def _warning(self, code: str, exc: Exception) -> tuple[Mapping[str, object], ...]:
        message = redact_text(str(exc), self.secret_values).strip() or "Predictions phase failed"
        return ({"code": code, "error_type": type(exc).__name__, "message": message},)

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        as_of = self._context(context)
        upstream = self.repository.resolve_upstream(context.run_id)
        observed_at = self._observed()
        values, missing = self.repository.load_input_inventory(upstream)
        expected = tuple(game.source_game_id for game in upstream.model_feature_set.feature_set.games)
        inventory_checksum = self.repository.input_inventory_checksum(values, expected)
        input_checksum = self._input_checksum(context, as_of, observed_at, upstream, inventory_checksum)
        try:
            if (
                upstream.model_feature_set.feature_set.requested_date != context.requested_date
                or upstream.model_feature_set.feature_set.as_of_time != as_of
            ):
                raise PredictionsPhaseHandlerError("context does not match sealed Model Feature Set")
            if observed_at < upstream.model_feature_set.feature_set.observed_at:
                raise PredictionsPhaseHandlerError("prediction boundary precedes Model Feature Set")
            if missing:
                raise PredictionsPhaseHandlerError("reviewed prediction input is missing for one or more games")
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=PredictionsAttemptOutcome.INPUT_FAILED,
                    upstream=upstream,
                    values=values,
                    missing=missing,
                    warnings=self._warning("predictions_input_failed", exc),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        try:
            snapshot = self.repository.assemble(upstream, values, observed_at=observed_at)
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=PredictionsAttemptOutcome.VALIDATION_FAILED,
                    upstream=upstream,
                    values=values,
                    missing=(),
                    warnings=self._warning("predictions_validation_failed", exc),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        try:
            persisted = self.repository.persist_assembly(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=input_checksum,
                predictions=snapshot,
                values=values,
            )
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=PredictionsAttemptOutcome.PERSISTENCE_FAILED,
                    upstream=upstream,
                    values=values,
                    missing=(),
                    warnings=self._warning("predictions_persistence_failed", exc),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        warnings = [dict(value) for value in persisted.predictions.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if warnings else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=input_checksum,
            output_checksum=persisted.predictions.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )
