from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.matchup_packet.repository import (
    MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION,
    MatchupPacketAttemptOutcome,
    MatchupPacketRepository,
)
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult

MATCHUP_PACKET_PHASE_INPUT_CONTRACT = "DSE_MATCHUP_PACKET_PHASE_INPUT_V1"


class MatchupPacketPhaseHandlerError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MatchupPacketPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        repository: MatchupPacketRepository | None = None,
    ) -> None:
        self.clock = clock
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.repository = repository or MatchupPacketRepository(
            database,
            artifact_root=artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )

    @staticmethod
    def _context(context: PhaseExecutionContext) -> datetime:
        if context.phase_key is not PipelinePhaseKey.MATCHUP_PACKET:
            raise MatchupPacketPhaseHandlerError("handler requires MATCHUP_PACKET phase")
        if context.attempt_number < 1:
            raise MatchupPacketPhaseHandlerError("phase attempt must be positive")
        validate_run_id(context.run_id)
        parse_requested_date(context.requested_date)
        parsed = datetime.fromisoformat(context.as_of_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise MatchupPacketPhaseHandlerError("context as_of_time must be aware")
        return parsed.astimezone(timezone.utc)

    def _observed(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise MatchupPacketPhaseHandlerError("handler clock must be aware")
        return value.astimezone(timezone.utc)

    def _input_checksum(self, run_id: str, observed_at: datetime) -> str:
        quality, identities = self.repository._upstream(run_id)
        return canonical_sha256(
            {
                "as_of_time": quality.snapshot.as_of_time.isoformat(),
                "assembly_policy_version": MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION,
                "contract_version": MATCHUP_PACKET_PHASE_INPUT_CONTRACT,
                "observed_at": observed_at.isoformat(),
                "requested_date": quality.snapshot.requested_date,
                "upstream": [item.as_dict() for item in identities],
            }
        )

    def _warning(self, code: str, exc: Exception) -> tuple[Mapping[str, object], ...]:
        message = redact_text(str(exc), self.secret_values).strip() or "Matchup Packet failed"
        return ({"code": code, "error_type": type(exc).__name__, "message": message},)

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        context_as_of = self._context(context)
        quality, _ = self.repository._upstream(context.run_id)
        if quality.snapshot.requested_date != context.requested_date or quality.snapshot.as_of_time != context_as_of:
            raise MatchupPacketPhaseHandlerError("context does not match Data Quality")
        observed_at = self._observed()
        if observed_at < quality.snapshot.observed_at:
            raise MatchupPacketPhaseHandlerError("packet boundary precedes Data Quality")
        input_checksum = self._input_checksum(context.run_id, observed_at)
        try:
            packet = self.repository.assemble_for_run(context.run_id, observed_at=observed_at)
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=MatchupPacketAttemptOutcome.ASSEMBLY_FAILED,
                    warnings=self._warning("matchup_packet_assembly_failed", exc),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        try:
            persisted = self.repository.persist_packet(
                run_id=context.run_id,
                phase_attempt=context.attempt_number,
                phase_input_checksum=input_checksum,
                packet=packet,
            )
        except Exception as exc:
            try:
                self.repository.persist_failed_attempt(
                    run_id=context.run_id,
                    phase_attempt=context.attempt_number,
                    phase_input_checksum=input_checksum,
                    observed_at=observed_at,
                    outcome=MatchupPacketAttemptOutcome.PERSISTENCE_FAILED,
                    warnings=self._warning("matchup_packet_persistence_failed", exc),
                )
            except Exception as retained_exc:
                exc.add_note(f"failed-attempt evidence error: {type(retained_exc).__name__}")
            raise
        warnings = [
            {"source_game_id": game.source_game_id, **issue.as_dict()}
            for game in persisted.packet.games
            for issue in game.data_quality.issues
        ]
        return PhaseExecutionResult(
            status=(
                PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
                if warnings
                else PipelinePhaseStatus.SUCCEEDED
            ),
            input_checksum=input_checksum,
            output_checksum=persisted.packet.checksum,
            artifact_relpath=persisted.artifact.relpath,
            warnings=warnings or None,
            continue_pipeline=True,
        )
