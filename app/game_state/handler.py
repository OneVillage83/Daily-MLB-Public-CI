from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from app.daily_slate.acquisition import verified_mlb_player_identity_resolver
from app.daily_slate.artifact import DailySlateArtifactV1, verify_daily_slate_artifact
from app.daily_slate.repository import DailySlateRepository
from app.database import Database
from app.game_state.acquisition import normalize_mlb_game_state
from app.game_state.artifact import write_game_state_artifact
from app.game_state.batch import (
    GameStateBatchAcquisitionError,
    MlbGameStateEvidenceSetV1,
    acquire_mlb_game_state_feeds,
)
from app.game_state.raw_link import (
    GameStateRawLinkOutcome,
    write_game_state_raw_link,
)
from app.game_state.repository import GameStateRepository
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult
from app.stats.contracts import StatsTransport
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import HttpStatsTransport


GAME_STATE_RAW_PROVIDER_DIRECTORY = "game_state_provider_raw"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GameStatePhaseHandler:
    """Production Manual Run Controller handler for authoritative MLB game feeds."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        request_timeout_seconds: float,
        user_agent: str,
        secret_values: Iterable[str] = (),
        max_attempts: int = 3,
        transport: StatsTransport | None = None,
        raw_store: RawArtifactStore | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if (transport is None) != (raw_store is None):
            raise ValueError("transport and raw_store must be supplied together")
        normalized_user_agent = user_agent.strip()
        if not normalized_user_agent:
            raise ValueError("GameState MLB user_agent must not be blank")

        self.database = database
        self.artifact_root = Path(artifact_root)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.request_timeout_seconds = request_timeout_seconds
        self.max_attempts = max_attempts
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        if raw_store is None:
            raw_store = RawArtifactStore(
                self.artifact_root / GAME_STATE_RAW_PROVIDER_DIRECTORY
            )
        if transport is None:
            transport = HttpStatsTransport(
                raw_store,
                user_agent=normalized_user_agent,
                clock=clock,
            )
        self.raw_store = raw_store
        self.transport = transport
        self.daily_slate_repository = DailySlateRepository(
            database,
            secret_values=self.secret_values,
            clock=clock,
        )
        self.repository = GameStateRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )
        self.player_identity_resolver = verified_mlb_player_identity_resolver(database)

    def _load_daily_slate(self, context: PhaseExecutionContext):
        persisted = self.daily_slate_repository.get_latest_daily_slate_for_run(
            context.run_id
        )
        if persisted is None:
            raise RuntimeError("sealed DailySlate snapshot does not exist for run")
        if persisted.slate.requested_date != context.requested_date:
            raise RuntimeError("sealed DailySlate requested date does not match run")
        if persisted.slate.as_of_time.isoformat() != context.as_of_time:
            raise RuntimeError("sealed DailySlate as_of_time does not match run")
        if (
            persisted.artifact_relpath is None
            or persisted.artifact_checksum is None
        ):
            raise RuntimeError("sealed DailySlate artifact metadata is incomplete")
        verify_daily_slate_artifact(
            persisted.slate,
            DailySlateArtifactV1(
                persisted.artifact_relpath,
                persisted.artifact_checksum,
                len(persisted.slate.canonical_json_bytes()),
            ),
            self.artifact_root,
        )
        return persisted

    def _persist_failed_attempt(
        self,
        context: PhaseExecutionContext,
        *,
        slate_checksum: str,
        outcome: GameStateRawLinkOutcome,
        evidence_set: MlbGameStateEvidenceSetV1 | None = None,
        batch_error: GameStateBatchAcquisitionError | None = None,
        normalization_error: BaseException | None = None,
    ) -> None:
        raw_link = write_game_state_raw_link(
            artifact_root=self.artifact_root,
            run_id=context.run_id,
            requested_date=context.requested_date,
            phase_attempt=context.attempt_number,
            upstream_daily_slate_checksum=slate_checksum,
            outcome=outcome,
            evidence_set=evidence_set,
            batch_error=batch_error,
            normalization_error=normalization_error,
            secret_values=self.secret_values,
        )
        self.repository.persist_attempt_evidence(
            run_id=context.run_id,
            phase_attempt=context.attempt_number,
            requested_date=context.requested_date,
            upstream_daily_slate_checksum=slate_checksum,
            outcome=outcome,
            raw_link_relpath=raw_link,
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        if context.phase_key is not PipelinePhaseKey.GAME_STATE:
            raise ValueError("GameStatePhaseHandler may only execute GAME_STATE")
        if context.attempt_number < 1:
            raise ValueError("GAME_STATE phase attempt must be positive")

        daily_slate = self._load_daily_slate(context)
        try:
            evidence_set = acquire_mlb_game_state_feeds(
                transport=self.transport,
                raw_store=self.raw_store,
                slate=daily_slate.slate,
                timeout_seconds=self.request_timeout_seconds,
                max_attempts=self.max_attempts,
            )
        except GameStateBatchAcquisitionError as exc:
            self._persist_failed_attempt(
                context,
                slate_checksum=daily_slate.slate.checksum,
                outcome=GameStateRawLinkOutcome.ACQUISITION_FAILED,
                batch_error=exc,
            )
            raise
        except Exception as exc:
            failed_source_game_id = (
                daily_slate.slate.games[0].source_game_id
                if daily_slate.slate.games
                else "unknown"
            )
            batch_error = GameStateBatchAcquisitionError(
                f"authoritative MLB game-state acquisition failed: {exc}",
                failed_source_game_id=failed_source_game_id,
                completed_evidence={},
            )
            self._persist_failed_attempt(
                context,
                slate_checksum=daily_slate.slate.checksum,
                outcome=GameStateRawLinkOutcome.ACQUISITION_FAILED,
                batch_error=batch_error,
            )
            raise exc from None

        try:
            normalized = normalize_mlb_game_state(
                slate=daily_slate.slate,
                evidence_by_game=evidence_set.evidence_by_game,
                player_identity_resolver=self.player_identity_resolver,
            )
        except Exception as exc:
            self._persist_failed_attempt(
                context,
                slate_checksum=daily_slate.slate.checksum,
                outcome=GameStateRawLinkOutcome.NORMALIZATION_FAILED,
                evidence_set=evidence_set,
                normalization_error=exc,
            )
            raise

        raw_link = write_game_state_raw_link(
            artifact_root=self.artifact_root,
            run_id=context.run_id,
            requested_date=context.requested_date,
            phase_attempt=context.attempt_number,
            upstream_daily_slate_checksum=daily_slate.slate.checksum,
            outcome=GameStateRawLinkOutcome.NORMALIZED,
            evidence_set=evidence_set,
            game_state_checksum=normalized.state.checksum,
            secret_values=self.secret_values,
        )
        self.repository.persist_attempt_evidence(
            run_id=context.run_id,
            phase_attempt=context.attempt_number,
            requested_date=context.requested_date,
            upstream_daily_slate_checksum=daily_slate.slate.checksum,
            outcome=GameStateRawLinkOutcome.NORMALIZED,
            normalized_snapshot_checksum=normalized.state.checksum,
            raw_link_relpath=raw_link,
            warnings=normalized.warning_payload(),
        )
        artifact = write_game_state_artifact(
            normalized.state,
            self.artifact_root,
            secret_values=self.secret_values,
        )
        persisted = self.repository.persist_game_state(
            run_id=context.run_id,
            phase_attempt=context.attempt_number,
            state=normalized.state,
            artifact=artifact,
        )
        reread = self.repository.get_game_state_snapshot(persisted.snapshot_id)
        if reread.state != normalized.state:
            raise RuntimeError("persisted GameStateV1 does not match normalized evidence")
        if reread.state.canonical_json_bytes() != normalized.state.canonical_json_bytes():
            raise RuntimeError("reconstructed GameStateV1 canonical bytes do not match")
        if reread.artifact != artifact:
            raise RuntimeError("persisted GameState artifact metadata does not match writer")
        warnings = normalized.warning_payload()
        return PhaseExecutionResult(
            status=(
                PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
                if warnings
                else PipelinePhaseStatus.SUCCEEDED
            ),
            input_checksum=daily_slate.slate.checksum,
            output_checksum=normalized.state.checksum,
            artifact_relpath=artifact.relpath,
            warnings=warnings or None,
        )
