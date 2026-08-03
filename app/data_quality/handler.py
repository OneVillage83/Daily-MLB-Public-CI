from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.data_quality.contracts import DataQualityDisposition, DataQualityPolicyV1
from app.data_quality.engine import DataQualityAssessmentError
from app.data_quality.repository import (
    DataQualityAttemptOutcome,
    DataQualityRepository,
    data_quality_warning_payload,
    DataQualityUpstreamV1,
)
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult

DATA_QUALITY_PHASE_INPUT_CONTRACT = "DSE_DATA_QUALITY_PHASE_INPUT_V1"


class DataQualityPhaseHandlerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DataQualityPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        repository: DataQualityRepository | None = None,
        policy: DataQualityPolicyV1 = DataQualityPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.policy = policy
        self.repository = repository or DataQualityRepository(
            database,
            artifact_root=artifact_root,
            secret_values=self.secret_values,
            clock=clock,
            policy=policy,
        )

    @staticmethod
    def _validate_context(context: PhaseExecutionContext) -> datetime:
        if context.phase_key is not PipelinePhaseKey.DATA_QUALITY:
            raise DataQualityPhaseHandlerError("handler requires DATA_QUALITY phase")
        if isinstance(context.attempt_number, bool) or not isinstance(context.attempt_number, int) or context.attempt_number < 1:
            raise DataQualityPhaseHandlerError("phase attempt must be positive")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        try:
            as_of = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DataQualityPhaseHandlerError("context as_of_time is invalid") from exc
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise DataQualityPhaseHandlerError("context as_of_time must be timezone-aware")
        return as_of.astimezone(timezone.utc)

    def _observed_at(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise DataQualityPhaseHandlerError("handler clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _input_checksum(
        self,
        context: PhaseExecutionContext,
        observed_at: datetime,
        upstream: DataQualityUpstreamV1,
    ) -> str:
        return canonical_sha256(
            {
                "as_of_time": upstream.slate.slate.as_of_time.isoformat(),
                "contract_version": DATA_QUALITY_PHASE_INPUT_CONTRACT,
                "observed_at": observed_at.isoformat(),
                "policy": self.policy.as_dict(),
                "requested_date": upstream.slate.slate.requested_date,
                "upstream_baseball_intelligence_checksum": upstream.baseball_intelligence.assembly.checksum,
                "upstream_baseball_intelligence_snapshot_id": upstream.baseball_intelligence.snapshot_id,
                "upstream_daily_slate_checksum": upstream.slate.slate.checksum,
                "upstream_daily_slate_snapshot_id": upstream.slate.snapshot_id,
                "upstream_game_state_checksum": upstream.state.state.checksum,
                "upstream_game_state_snapshot_id": upstream.state.snapshot_id,
                "upstream_odds_weather_checksum": upstream.odds_weather.snapshot.checksum,
                "upstream_odds_weather_snapshot_id": upstream.odds_weather.snapshot_id,
            }
        )

    def _failure_warning(self, code: str, exc: Exception) -> tuple[Mapping[str, object], ...]:
        message = redact_text(str(exc), self.secret_values).strip() or "Data Quality failed"
        return ({"code": code, "error_type": type(exc).__name__, "message": message},)

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        context_as_of = self._validate_context(context)
        upstream = self.repository.resolve_upstream(context.run_id)
        if (
            upstream.slate.slate.requested_date != context.requested_date
            or upstream.slate.slate.as_of_time != context_as_of
        ):
            raise DataQualityPhaseHandlerError("context does not match sealed upstream chain")
        observed_at = self._observed_at()
        latest = max(
            upstream.slate.slate.observed_at,
            upstream.state.state.observed_at,
            upstream.baseball_intelligence.assembly.observed_at,
            upstream.odds_weather.snapshot.observed_at,
        )
        if observed_at < latest:
            raise DataQualityPhaseHandlerError(
                "assessment boundary cannot precede upstream evidence"
            )
        input_checksum = self._input_checksum(context, observed_at, upstream)
        try:
            result, _ = self.repository.assess_for_run(
                context.run_id, observed_at=observed_at
            )
        except Exception as exc:
            if isinstance(exc, DataQualityAssessmentError):
                try:
                    self.repository.persist_failed_attempt(
                        run_id=context.run_id,
                        phase_attempt=context.attempt_number,
                        phase_input_checksum=input_checksum,
                        observed_at=observed_at,
                        outcome=DataQualityAttemptOutcome.ASSESSMENT_FAILED,
                        warnings=self._failure_warning("data_quality_assessment_failed", exc),
                        upstream=upstream,
                    )
                except Exception as retained_exc:
                    exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        try:
            persisted = self.repository.persist_assessment(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=input_checksum,
                result=result,
            )
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=DataQualityAttemptOutcome.PERSISTENCE_FAILED,
                    warnings=self._failure_warning("data_quality_persistence_failed", exc),
                    upstream=upstream,
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        warnings = data_quality_warning_payload(persisted.snapshot)
        if any(
            game.disposition in {DataQualityDisposition.DEGRADED, DataQualityDisposition.INSUFFICIENT}
            for game in persisted.snapshot.games
        ):
            status = PipelinePhaseStatus.DEGRADED
        elif warnings:
            status = PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
        else:
            status = PipelinePhaseStatus.SUCCEEDED
        return PhaseExecutionResult(
            status=status,
            input_checksum=input_checksum,
            output_checksum=persisted.snapshot.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=list(warnings) or None,
            continue_pipeline=True,
        )
