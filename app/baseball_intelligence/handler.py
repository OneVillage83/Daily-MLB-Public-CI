"""Production Manual Run Controller handler for Baseball Intelligence Assembly."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.baseball_intelligence.assembly import (
    BaseballIntelligenceAssemblyError,
    BaseballIntelligenceAssemblyResultV1,
)
from app.baseball_intelligence.attempt_manifest import (
    BaseballIntelligenceAttemptManifestV1,
    BaseballIntelligenceAttemptOutcome,
)
from app.baseball_intelligence.repository import (
    BaseballIntelligenceAttemptEvidenceV1,
    BaseballIntelligenceNotFoundError,
    BaseballIntelligenceRepository,
    PersistedBaseballIntelligenceV1,
)
from app.baseball_intelligence.selector import (
    BaseballFeatureCandidateInventoryV1,
    BaseballIntelligenceSelectorError,
)
from app.daily_slate.contracts import canonical_sha256
from app.daily_slate.repository import PersistedDailySlateV1
from app.database import Database
from app.game_state.repository import PersistedGameStateV1
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


BASEBALL_INTELLIGENCE_PHASE_INPUT_CONTRACT = (
    "DSE_BASEBALL_INTELLIGENCE_PHASE_INPUT_V1"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_aware_utc(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    return _aware_utc(parsed, field_name)


def baseball_intelligence_phase_input_checksum(
    *,
    requested_date: str,
    as_of_time: datetime,
    upstream_daily_slate_checksum: str,
    upstream_game_state_checksum: str,
    candidate_inventory_checksum: str,
    selection_observed_at: datetime,
) -> str:
    """Hash the complete retained-evidence and PIT input domain for Phase 3."""

    return canonical_sha256(
        {
            "as_of_time": _aware_utc(as_of_time, "as_of_time").isoformat(),
            "candidate_inventory_checksum": candidate_inventory_checksum,
            "contract_version": BASEBALL_INTELLIGENCE_PHASE_INPUT_CONTRACT,
            "requested_date": parse_requested_date(requested_date).isoformat(),
            "selection_observed_at": _aware_utc(
                selection_observed_at,
                "selection_observed_at",
            ).isoformat(),
            "upstream_daily_slate_checksum": upstream_daily_slate_checksum,
            "upstream_game_state_checksum": upstream_game_state_checksum,
        }
    )


class BaseballIntelligencePhaseHandler:
    """Execute Phase 3 entirely from sealed, retained internal evidence."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        repository: BaseballIntelligenceRepository | None = None,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.repository = repository or BaseballIntelligenceRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
        )

    def _validate_context(
        self,
        context: PhaseExecutionContext,
    ) -> tuple[
        str,
        PersistedDailySlateV1,
        PersistedGameStateV1,
        datetime,
    ]:
        if context.phase_key is not PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY:
            raise ValueError(
                "BaseballIntelligencePhaseHandler may only execute "
                "BASEBALL_INTELLIGENCE_ASSEMBLY"
            )
        if (
            isinstance(context.attempt_number, bool)
            or not isinstance(context.attempt_number, int)
            or context.attempt_number < 1
        ):
            raise ValueError("BASEBALL_INTELLIGENCE_ASSEMBLY attempt must be positive")
        safe_run_id = validate_run_id(context.run_id)
        requested_date = parse_requested_date(context.requested_date).isoformat()
        context_as_of = _parse_aware_utc(context.as_of_time, "context as_of_time")
        slate, state = self.repository._verify_upstream(safe_run_id)
        if (
            slate.run_id != safe_run_id
            or state.run_id != safe_run_id
            or slate.slate.requested_date != requested_date
            or state.state.requested_date != requested_date
        ):
            raise ValueError("Phase 3 context does not match sealed upstream run/date")
        if (
            slate.slate.as_of_time != context_as_of
            or state.state.as_of_time != context_as_of
        ):
            raise ValueError("Phase 3 context as_of_time does not match sealed upstream")
        return safe_run_id, slate, state, context_as_of

    def _safe_failure_warning(
        self,
        *,
        code: str,
        error: Exception,
    ) -> tuple[Mapping[str, object], ...]:
        message = redact_text(str(error), self.secret_values).strip()
        if not message:
            message = "Baseball Intelligence phase failed"
        return (
            {
                "code": code,
                "error_type": type(error).__name__,
                "message": message,
            },
        )

    def _retain_failed_attempt_without_masking(
        self,
        *,
        original: Exception,
        run_id: str,
        phase_attempt: int,
        outcome: BaseballIntelligenceAttemptOutcome,
        inventory: BaseballFeatureCandidateInventoryV1,
        selection_observed_at: datetime,
        warning_code: str,
    ) -> None:
        try:
            existing = self.repository.get_attempt_evidence(run_id, phase_attempt)
        except BaseballIntelligenceNotFoundError:
            existing = None
        if (
            existing is not None
            and existing.outcome is BaseballIntelligenceAttemptOutcome.ASSEMBLED
        ):
            return
        try:
            self.repository.persist_failed_attempt(
                run_id=run_id,
                phase_attempt=phase_attempt,
                outcome=outcome,
                inventory=inventory,
                warnings=self._safe_failure_warning(
                    code=warning_code,
                    error=original,
                ),
                selection_observed_at=selection_observed_at,
            )
        except Exception as evidence_error:
            original.add_note(
                "failed-attempt evidence could not be retained "
                f"({type(evidence_error).__name__})"
            )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        run_id, slate, state, context_as_of = self._validate_context(context)
        selection_observed_at = _aware_utc(
            self.clock(),
            "selection_observed_at",
        )
        latest_upstream_observation = max(
            slate.slate.observed_at,
            state.state.observed_at,
        )
        if selection_observed_at < latest_upstream_observation:
            raise ValueError(
                "selection_observed_at cannot precede retained upstream evidence"
            )

        # A selector failure here intentionally creates no misleading empty inventory
        # or attempt manifest. The controller retains the redacted phase failure.
        inventory = self.repository.load_candidate_inventory(run_id=run_id)
        input_checksum = baseball_intelligence_phase_input_checksum(
            requested_date=context.requested_date,
            as_of_time=context_as_of,
            upstream_daily_slate_checksum=slate.slate.checksum,
            upstream_game_state_checksum=state.state.checksum,
            candidate_inventory_checksum=inventory.checksum,
            selection_observed_at=selection_observed_at,
        )
        try:
            result, assembled_inventory = self.repository.assemble_for_run(
                run_id=run_id,
                observed_at=selection_observed_at,
            )
            if assembled_inventory != inventory:
                raise BaseballIntelligenceAssemblyError(
                    "Phase 3 candidate inventory changed during fixed-boundary assembly"
                )
        except BaseballIntelligenceSelectorError:
            raise
        except BaseballIntelligenceAssemblyError as exc:
            self._retain_failed_attempt_without_masking(
                original=exc,
                run_id=run_id,
                phase_attempt=context.attempt_number,
                outcome=BaseballIntelligenceAttemptOutcome.SELECTION_FAILED,
                inventory=inventory,
                selection_observed_at=selection_observed_at,
                warning_code="baseball_intelligence_selection_failed",
            )
            raise

        try:
            persisted = self.repository.persist_assembly(
                run_id=run_id,
                phase_attempt=context.attempt_number,
                result=result,
                inventory=inventory,
            )
            reconstructed = self.repository.get_for_run_attempt(
                run_id,
                context.attempt_number,
            )
            evidence = self.repository.get_attempt_evidence(
                run_id,
                context.attempt_number,
            )
            manifest = self.repository.get_attempt_manifest(
                run_id,
                context.attempt_number,
            )
            self._verify_success(
                result=result,
                inventory=inventory,
                persisted=persisted,
                reconstructed=reconstructed,
                evidence=evidence,
                manifest=manifest,
            )
        except Exception as exc:
            self._retain_failed_attempt_without_masking(
                original=exc,
                run_id=run_id,
                phase_attempt=context.attempt_number,
                outcome=BaseballIntelligenceAttemptOutcome.ASSEMBLY_FAILED,
                inventory=inventory,
                selection_observed_at=selection_observed_at,
                warning_code="baseball_intelligence_assembly_failed",
            )
            raise

        warnings = result.warning_payload()
        return PhaseExecutionResult(
            status=(
                PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
                if warnings
                else PipelinePhaseStatus.SUCCEEDED
            ),
            input_checksum=input_checksum,
            output_checksum=result.assembly.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )

    def _verify_success(
        self,
        *,
        result: BaseballIntelligenceAssemblyResultV1,
        inventory: BaseballFeatureCandidateInventoryV1,
        persisted: PersistedBaseballIntelligenceV1,
        reconstructed: PersistedBaseballIntelligenceV1,
        evidence: BaseballIntelligenceAttemptEvidenceV1,
        manifest: BaseballIntelligenceAttemptManifestV1,
    ) -> None:
        canonical = result.assembly.canonical_json_bytes()
        if (
            persisted.snapshot_id != f"bia:{result.assembly.checksum}"
            or reconstructed.snapshot_id != persisted.snapshot_id
            or persisted.assembly.canonical_json_bytes() != canonical
            or reconstructed.assembly.canonical_json_bytes() != canonical
            or persisted.assembly.checksum != result.assembly.checksum
            or evidence.outcome is not BaseballIntelligenceAttemptOutcome.ASSEMBLED
            or evidence.assembly_checksum != result.assembly.checksum
            or evidence.selection_observed_at != result.assembly.observed_at
            or tuple(evidence.warnings)
            != self.repository._warning_payload(result.warnings)
            or manifest.candidate_inventory_checksum != inventory.checksum
            or manifest.candidate_feature_snapshot_ids
            != inventory.candidate_feature_snapshot_ids
        ):
            raise RuntimeError(
                "persisted Baseball Intelligence evidence does not match assembly"
            )
