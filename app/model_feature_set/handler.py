from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.model_feature_set.contracts import (
    MODEL_FEATURE_ENCODING_POLICY_VERSION,
    MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
    MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
    ModelFeatureSourceV1,
)
from app.matchup_packet.repository import PersistedMatchupPacketV1
from app.pre_model_evidence import PreModelUpstreamIdentityV1
from app.model_feature_set.repository import (
    ModelFeatureSetAttemptOutcome,
    ModelFeatureSetRepository,
    selected_feature_inventory_checksum,
)
from app.model_feature_set.schema import (
    MODEL_FEATURE_SCHEMA_CHECKSUM,
    MODEL_FEATURE_SCHEMA_VERSION,
    MODEL_FEATURE_SET_CONTRACT_VERSION,
)
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.stats.features import FEATURE_VERSION_V3

MODEL_FEATURE_SET_PHASE_INPUT_CONTRACT = "DSE_MODEL_FEATURE_SET_PHASE_INPUT_V1"


class ModelFeatureSetPhaseHandlerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ModelFeatureSetPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        repository: ModelFeatureSetRepository | None = None,
    ) -> None:
        self.clock = clock
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.repository = repository or ModelFeatureSetRepository(
            database,
            artifact_root=artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )

    @staticmethod
    def _context(context: PhaseExecutionContext) -> datetime:
        if context.phase_key is not PipelinePhaseKey.MODEL_FEATURE_SET:
            raise ModelFeatureSetPhaseHandlerError("handler requires MODEL_FEATURE_SET phase")
        if isinstance(context.attempt_number, bool) or not isinstance(context.attempt_number, int) or context.attempt_number < 1:
            raise ModelFeatureSetPhaseHandlerError("phase attempt must be positive")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        parsed = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ModelFeatureSetPhaseHandlerError("context as_of_time must be aware")
        return parsed.astimezone(timezone.utc)

    def _observed(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ModelFeatureSetPhaseHandlerError("handler clock must be aware")
        return value.astimezone(timezone.utc)

    def _input_checksum(
        self,
        packet: PersistedMatchupPacketV1,
        quality: PreModelUpstreamIdentityV1,
        inventory: tuple[ModelFeatureSourceV1, ...],
        observed_at: datetime,
    ) -> str:
        return canonical_sha256(
            {
                "as_of_time": packet.packet.as_of_time.isoformat(),
                "contract_version": MODEL_FEATURE_SET_PHASE_INPUT_CONTRACT,
                "encoding_policy_version": MODEL_FEATURE_ENCODING_POLICY_VERSION,
                "feature_contract_version": MODEL_FEATURE_SET_CONTRACT_VERSION,
                "feature_schema_checksum": MODEL_FEATURE_SCHEMA_CHECKSUM,
                "feature_schema_version": MODEL_FEATURE_SCHEMA_VERSION,
                "feature_version": FEATURE_VERSION_V3,
                "missing_value_policy_version": MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
                "observed_at": observed_at.isoformat(),
                "requested_date": packet.packet.requested_date,
                "selected_feature_inventory": [value.as_dict() for value in inventory],
                "selected_feature_inventory_checksum": selected_feature_inventory_checksum(inventory),
                "transformation_policy_version": MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
                "upstream_data_quality_checksum": quality.checksum,
                "upstream_data_quality_snapshot_id": quality.snapshot_id,
                "upstream_matchup_packet_checksum": packet.packet.checksum,
                "upstream_matchup_packet_snapshot_id": packet.snapshot_id,
            }
        )

    def _warning(self, code: str, exc: Exception) -> tuple[Mapping[str, object], ...]:
        message = redact_text(str(exc), self.secret_values).strip() or "Model Feature Set failed"
        return ({"code": code, "error_type": type(exc).__name__, "message": message},)

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        context_as_of = self._context(context)
        packet, quality, packet_identity = self.repository._upstream(context.run_id)
        if packet.packet.requested_date != context.requested_date or packet.packet.as_of_time != context_as_of:
            raise ModelFeatureSetPhaseHandlerError("context does not match Matchup Packet")
        observed_at = self._observed()
        if observed_at < packet.packet.observed_at:
            raise ModelFeatureSetPhaseHandlerError("feature boundary precedes Matchup Packet")
        inventory = self.repository.resolve_selected_inventory(packet)
        input_checksum = self._input_checksum(packet, quality, inventory, observed_at)
        try:
            feature_set = self.repository.build_from_exact_packet(
                packet, observed_at=observed_at
            )
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=ModelFeatureSetAttemptOutcome.TRANSFORMATION_FAILED,
                    warnings=self._warning("model_feature_set_transformation_failed", exc),
                    packet_snapshot=packet,
                    selected_inventory=inventory,
                    upstream_identities=(quality, packet_identity),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        try:
            persisted = self.repository.persist_feature_set(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=input_checksum,
                feature_set=feature_set,
            )
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=ModelFeatureSetAttemptOutcome.PERSISTENCE_FAILED,
                    warnings=self._warning("model_feature_set_persistence_failed", exc),
                    packet_snapshot=packet,
                    selected_inventory=inventory,
                    upstream_identities=(quality, packet_identity),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        warnings = [
            {"source_game_id": game.source_game_id, "issue_code": code}
            for game in persisted.feature_set.games
            for code in game.quality_issue_codes
        ]
        degraded = any(
            game.quality_disposition.value == "insufficient"
            for game in persisted.feature_set.games
        )
        return PhaseExecutionResult(
            status=(
                PipelinePhaseStatus.DEGRADED
                if degraded
                else PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
                if warnings
                else PipelinePhaseStatus.SUCCEEDED
            ),
            input_checksum=input_checksum,
            output_checksum=persisted.feature_set.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )
