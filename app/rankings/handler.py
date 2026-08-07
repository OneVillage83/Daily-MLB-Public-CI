from __future__ import annotations
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date, validate_run_id
from app.rankings.production import RANKINGS_PHASE_INPUT_CONTRACT, RankingPolicyV1
from app.rankings.repository import RankingsAttemptOutcome, RankingsRepository
from app.redaction import redact_text
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import PhaseExecutionContext, PhaseExecutionResult


class RankingsPhaseHandlerError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RankingsPhaseHandler:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        repository: RankingsRepository | None = None,
        policy: RankingPolicyV1 = RankingPolicyV1(),
    ) -> None:
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.repository = repository or RankingsRepository(
            database, artifact_root=artifact_root, secret_values=self.secret_values, clock=clock, policy=policy
        )

    def _warning(self, code: str, e: Exception) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "code": code,
                "error_type": type(e).__name__,
                "message": redact_text(str(e), self.secret_values).strip() or "Rankings failed",
            },
        )

    def __call__(self, context: PhaseExecutionContext) -> PhaseExecutionResult:
        c = context
        if c.phase_key is not PipelinePhaseKey.RANKINGS:
            raise RankingsPhaseHandlerError("handler requires RANKINGS")
        if isinstance(c.attempt_number, bool) or not isinstance(c.attempt_number, int) or c.attempt_number < 1:
            raise RankingsPhaseHandlerError("phase attempt must be positive")
        validate_run_id(c.run_id)
        parse_requested_date(c.requested_date)
        asof = datetime.fromisoformat(c.as_of_time.replace("Z", "+00:00"))
        if asof.tzinfo is None or asof.utcoffset() is None:
            raise RankingsPhaseHandlerError("context as_of_time must be aware")
        asof = asof.astimezone(timezone.utc)
        g = self.repository.resolve_upstream(c.run_id)
        ranked = self.clock()
        if ranked.tzinfo is None or ranked.utcoffset() is None:
            raise RankingsPhaseHandlerError("handler clock must be timezone-aware")
        ranked = ranked.astimezone(timezone.utc)
        input_checksum = canonical_sha256(
            {
                "as_of_time": asof.isoformat(),
                "contract_version": RANKINGS_PHASE_INPUT_CONTRACT,
                "policy": self.policy.as_dict(),
                "ranked_at": ranked.isoformat(),
                "requested_date": c.requested_date,
                "upstream_gate_checksum": g.gate.checksum,
                "upstream_gate_snapshot_id": g.snapshot_id,
            }
        )
        try:
            if g.gate.requested_date != c.requested_date or g.gate.as_of_time != asof or ranked < g.gate.evaluated_at:
                raise RankingsPhaseHandlerError("context or ranking boundary mismatch")
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    ranked_at=ranked,
                    outcome=RankingsAttemptOutcome.INPUT_FAILED,
                    upstream=g,
                    warnings=self._warning("rankings_input_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        try:
            s = self.repository.rank(g, ranked_at=ranked)
        except Exception as e:
            try:
                self.repository.persist_failed_attempt(
                    run_id=c.run_id,
                    phase_attempt=c.attempt_number,
                    phase_input_checksum=input_checksum,
                    ranked_at=ranked,
                    outcome=RankingsAttemptOutcome.RANKING_FAILED,
                    upstream=g,
                    warnings=self._warning("rankings_ranking_failed", e),
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
                    ranked_at=ranked,
                    outcome=RankingsAttemptOutcome.PERSISTENCE_FAILED,
                    upstream=g,
                    warnings=self._warning("rankings_persistence_failed", e),
                )
            except Exception as x:
                e.add_note(f"failed-attempt evidence error: {type(x).__name__}")
            raise
        w = [dict(v) for v in p.rankings.warnings]
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS if w else PipelinePhaseStatus.SUCCEEDED,
            input_checksum=input_checksum,
            output_checksum=p.rankings.checksum,
            artifact_relpath=p.artifact.relpath,
            warnings=w or None,
            continue_pipeline=True,
        )
