from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.database import Database
from app.decision_evidence import (
    create_decision_manifest,
    publish_decision_manifest,
    publish_snapshot,
    verify_decision_manifest,
    verify_snapshot,
)
from app.identifiers import validate_run_id
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)
from app.rankings.production import ProductionRankingsV1, RankingEntryV1, RankingPolicyV1, build_rankings
from app.recommendation_gate.repository import PersistedRecommendationGateV1, RecommendationGateRepository
from app.value_engine.repository import ValueEngineRepository
from app.predictions.repository import PredictionsRepository

RANKINGS_ATTEMPT_MANIFEST_CONTRACT = "DSE_RANKINGS_ATTEMPT_MANIFEST_V1"


class RankingsAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    RANKING_FAILED = "ranking_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class RankingsRepositoryError(RuntimeError):
    pass


class RankingsNotFoundError(RankingsRepositoryError):
    pass


class RankingsPersistenceConflict(RankingsRepositoryError):
    pass


class RankingsIntegrityError(RankingsRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class RankingsAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: RankingsAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1


@dataclass(frozen=True, slots=True)
class PersistedRankingsV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    rankings: ProductionRankingsV1
    artifact: PreModelArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(v: object, n: str) -> datetime:
    if not isinstance(v, str):
        raise RankingsIntegrityError(f"persisted {n} is not text")
    try:
        return aware_utc(datetime.fromisoformat(v.replace("Z", "+00:00")), n)
    except ValueError as exc:
        raise RankingsIntegrityError(f"persisted {n} invalid") from exc


def _policy(v: object) -> RankingPolicyV1:
    if isinstance(v, str):
        v = json.loads(v)
    if not isinstance(v, dict):
        raise RankingsIntegrityError("ranking policy invalid")
    p = RankingPolicyV1(comparator=tuple(str(x) for x in v["comparator"]), policy_version=str(v["policy_version"]))
    if v.get("checksum") != p.checksum:
        raise RankingsIntegrityError("ranking policy checksum mismatch")
    return p


class RankingsRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: RankingPolicyV1 = RankingPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.gate = RecommendationGateRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.value = ValueEngineRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.predictions = PredictionsRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> PersistedRecommendationGateV1:
        g = self.gate.get_latest_for_run(validate_run_id(run_id))
        if g is None:
            raise RankingsIntegrityError("Rankings requires sealed Recommendation Gate")
        return g

    def rank(self, g: PersistedRecommendationGateV1, *, ranked_at: datetime) -> ProductionRankingsV1:
        value = self.value.get_by_snapshot_id(g.gate.upstream_value_snapshot_id)
        pred = self.predictions.get_by_snapshot_id(g.gate.upstream_predictions_snapshot_id)
        pred_up = self.predictions.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=pred.predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=pred.predictions.upstream_data_quality_snapshot_id,
        )
        starts: dict[str, datetime] = {}
        for game in pred_up.matchup_packet.packet.games:
            scheduled_start = game.schedule.scheduled_start_time
            if scheduled_start is None:
                raise RankingsIntegrityError("rankings require a scheduled start time")
            starts[game.source_game_id] = scheduled_start
        pred_by = {x.source_game_id: x for x in pred.predictions.games}
        metrics = {}
        for value_game in value.value_engine.games:
            gate_game = next(x for x in g.gate.games if x.source_game_id == value_game.source_game_id)
            outcome = next((x for x in value_game.outcomes if x.side == gate_game.selected_side), None)
            if gate_game.decision == "recommend" and outcome is None:
                raise RankingsIntegrityError("recommended game is missing its selected Value outcome")
            audit_outcome = value_game.outcomes[0] if outcome is None else outcome
            metrics[value_game.source_game_id] = {
                "bookmaker_count": len(audit_outcome.eligible_pairs),
                "edge": audit_outcome.edge,
                "ev": audit_outcome.expected_value_per_unit,
                "interval_width": pred_by[value_game.source_game_id].home_upper
                - pred_by[value_game.source_game_id].home_lower,
                "lower_bound_clearance": audit_outcome.lower_bound_clearance,
            }
        return build_rankings(
            g.gate,
            value_metrics_by_game=metrics,
            scheduled_start_by_game=starts,
            policy=self.policy,
            ranked_at=ranked_at,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(c: sqlite3.Connection, run_id: str, attempt: int) -> None:
        r = c.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='rankings'", (run_id,)
        ).fetchone()
        if r is None or str(r["status"]) != "running" or int(r["attempt_count"]) != attempt:
            raise RankingsIntegrityError("Rankings attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        g: PersistedRecommendationGateV1,
        snapshot: ProductionRankingsV1 | None,
        ranked_at: datetime,
        input_checksum: str,
        outcome: RankingsAttemptOutcome,
        warnings: tuple[Mapping[str, object], ...],
        created_at: datetime,
    ) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=RANKINGS_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="rankings",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=g.gate.requested_date,
            as_of_time=g.gate.as_of_time,
            observed_at=ranked_at,
            phase_input_checksum=input_checksum,
            upstream=(PreModelUpstreamIdentityV1("recommendation_gate", g.snapshot_id, g.gate.checksum),),
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=created_at,
            phase_input_evidence={"policy": self.policy.as_dict()},
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(c: sqlite3.Connection, m: PreModelAttemptManifestV1, a: PreModelArtifactV1) -> None:
        u = m.upstream[0]
        p = m.phase_input_evidence["policy"]
        if not isinstance(p, Mapping):
            raise RankingsIntegrityError("ranking policy missing")
        c.execute(
            """INSERT INTO rankings_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,ranked_at,phase_input_checksum,policy_json,policy_checksum,upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.run_id,
                m.phase_attempt,
                m.requested_date,
                m.as_of_time.isoformat(),
                m.observed_at.isoformat(),
                m.phase_input_checksum,
                canonical_text(p),
                p["checksum"],
                u.snapshot_id,
                u.checksum,
                m.outcome,
                m.snapshot_checksum,
                a.relpath,
                a.checksum,
                a.byte_count,
                canonical_text(list(m.warnings)),
                len(m.warnings),
                m.created_at.isoformat(),
                m.completed_at.isoformat(),
            ),
        )

    def persist_assembly(
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, snapshot: ProductionRankingsV1
    ) -> PersistedRankingsV1:
        try:
            e = self.get_for_run_attempt(run_id, phase_attempt)
        except RankingsNotFoundError:
            e = None
        if e:
            if (
                e.phase_input_checksum == phase_input_checksum
                and e.rankings.canonical_json_bytes() == snapshot.canonical_json_bytes()
            ):
                return e
            raise RankingsPersistenceConflict("conflicting Rankings replay")
        g = self.resolve_upstream(run_id)
        replay = self.rank(g, ranked_at=snapshot.ranked_at)
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise RankingsIntegrityError("Rankings result is not reproducible")
        now = self._now()
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            g=g,
            snapshot=snapshot,
            ranked_at=snapshot.ranked_at,
            input_checksum=phase_input_checksum,
            outcome=RankingsAttemptOutcome.ASSEMBLED,
            warnings=snapshot.warnings,
            created_at=now,
        )
        ma = None
        sa = None
        try:
            with self.database.connect() as c:
                self._active(c, run_id, phase_attempt)
            ma = publish_decision_manifest(m, self.artifact_root, "rankings", secret_values=self.secret_values)
            rel = f"rankings/snapshots/{snapshot.checksum}/rankings_v1.json"
            sa = publish_snapshot(
                artifact_root=self.artifact_root,
                relpath=rel,
                payload=snapshot.as_dict(),
                content=snapshot.canonical_json_bytes(),
                secret_values=self.secret_values,
            )
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, m, ma)
                sid = f"rankings:{snapshot.checksum}"
                c.execute(
                    """INSERT INTO ranking_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,ranked_at,contract_version,phase_input_checksum,policy_json,policy_checksum,upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum,snapshot_checksum,entry_count,eligible_count,warning_count,warnings_json,canonical_json,artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        sid,
                        run_id,
                        phase_attempt,
                        snapshot.requested_date,
                        snapshot.as_of_time.isoformat(),
                        snapshot.ranked_at.isoformat(),
                        snapshot.contract_version,
                        phase_input_checksum,
                        canonical_text(snapshot.policy.as_dict()),
                        snapshot.policy.checksum,
                        snapshot.upstream_gate_snapshot_id,
                        snapshot.upstream_gate_checksum,
                        snapshot.checksum,
                        len(snapshot.entries),
                        sum(x.rank_eligible for x in snapshot.entries),
                        len(snapshot.warnings),
                        canonical_text(list(snapshot.warnings)),
                        snapshot.canonical_json_bytes().decode(),
                        sa.relpath,
                        sa.checksum,
                        sa.byte_count,
                        now.isoformat(),
                    ),
                )
                for x in snapshot.entries:
                    c.execute(
                        "INSERT INTO ranking_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            run_id,
                            phase_attempt,
                            x.ordinal,
                            x.source_game_id,
                            x.decision,
                            x.selected_side,
                            x.selected_team_id,
                            int(x.rank_eligible),
                            x.recommendation_rank,
                            x.upstream_gate_game_checksum,
                            x.checksum,
                            canonical_text(x.as_dict()),
                        ),
                    )
                rows = c.execute(
                    "SELECT canonical_json,entry_checksum FROM ranking_entries WHERE snapshot_id=? ORDER BY ordinal",
                    (sid,),
                ).fetchall()
                if [(str(r["canonical_json"]), str(r["entry_checksum"])) for r in rows] != [
                    (canonical_text(x.as_dict()), x.checksum) for x in snapshot.entries
                ]:
                    raise RankingsIntegrityError("ranking entry mismatch")
                c.execute(
                    "UPDATE ranking_snapshots SET sealed_at=? WHERE snapshot_id=?", (self._now().isoformat(), sid)
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(sa, ma)
            raise RankingsPersistenceConflict("conflicting Rankings evidence") from exc
        except Exception:
            self._cleanup(sa, ma)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(self, *a: PreModelArtifactV1 | None) -> None:
        for v in a:
            if v:
                cleanup_owned_artifact(self.artifact_root, v)

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        ranked_at: datetime,
        outcome: RankingsAttemptOutcome,
        upstream: PersistedRecommendationGateV1,
        warnings: tuple[Mapping[str, object], ...],
    ) -> RankingsAttemptEvidenceV1:
        if outcome is RankingsAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt cannot be assembled")
        try:
            e = self.get_attempt_evidence(run_id, phase_attempt)
        except RankingsNotFoundError:
            e = None
        if e:
            if e.outcome is outcome and e.phase_input_checksum == phase_input_checksum and e.warnings == warnings:
                return e
            raise RankingsPersistenceConflict("conflicting failed Rankings attempt")
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            g=upstream,
            snapshot=None,
            ranked_at=ranked_at,
            input_checksum=phase_input_checksum,
            outcome=outcome,
            warnings=warnings,
            created_at=self._now(),
        )
        a = publish_decision_manifest(m, self.artifact_root, "rankings", secret_values=self.secret_values)
        try:
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, m, a)
        except Exception:
            cleanup_owned_artifact(self.artifact_root, a)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _attempt_row(self, run_id: str, attempt: int) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM rankings_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if r is None:
            raise RankingsNotFoundError("Rankings attempt not found")
        return r

    def _manifest_from_row(self, r: sqlite3.Row) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=RANKINGS_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="rankings",
            run_id=str(r["run_id"]),
            phase_attempt=int(r["phase_attempt"]),
            requested_date=str(r["requested_date"]),
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=_time(r["ranked_at"], "ranked_at"),
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "recommendation_gate",
                    str(r["upstream_recommendation_gate_snapshot_id"]),
                    str(r["upstream_recommendation_gate_checksum"]),
                ),
            ),
            outcome=str(r["outcome"]),
            snapshot_checksum=None if r["snapshot_checksum"] is None else str(r["snapshot_checksum"]),
            warnings=tuple(dict(v) for v in json.loads(str(r["warnings_json"]))),
            created_at=_time(r["created_at"], "created_at"),
            phase_input_evidence={"policy": _policy(r["policy_json"]).as_dict()},
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> PreModelAttemptManifestV1:
        r = self._attempt_row(run_id, attempt)
        m = self._manifest_from_row(r)
        verify_decision_manifest(
            m,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
            self.artifact_root,
            secret_values=self.secret_values,
        )
        return m

    def get_attempt_evidence(self, run_id: str, attempt: int) -> RankingsAttemptEvidenceV1:
        r = self._attempt_row(run_id, attempt)
        m = self.get_attempt_manifest(run_id, attempt)
        return RankingsAttemptEvidenceV1(
            m.run_id,
            m.phase_attempt,
            RankingsAttemptOutcome(m.outcome),
            m.snapshot_checksum,
            m.phase_input_checksum,
            m.warnings,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[RankingsAttemptEvidenceV1, ...]:
        with self.database.connect() as c:
            a = tuple(
                int(r[0])
                for r in c.execute(
                    "SELECT phase_attempt FROM rankings_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, n) for n in a)

    def _snapshot_row(self, w: str, v: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(f"SELECT * FROM ranking_snapshots WHERE {w}", v).fetchone()
        if r is None or r["sealed_at"] is None:
            raise RankingsNotFoundError("sealed Rankings snapshot not found")
        return r

    def _verify(self, r: sqlite3.Row) -> PersistedRankingsV1:
        gate = self.gate.get_by_snapshot_id(str(r["upstream_recommendation_gate_snapshot_id"]))
        if gate.gate.checksum != str(r["upstream_recommendation_gate_checksum"]):
            raise RankingsIntegrityError("historical Rankings upstream mismatch")
        with self.database.connect() as c:
            rows = c.execute(
                "SELECT canonical_json,entry_checksum FROM ranking_entries WHERE snapshot_id=? ORDER BY ordinal",
                (str(r["snapshot_id"]),),
            ).fetchall()
        entries = []
        for row in rows:
            p = json.loads(str(row["canonical_json"]))
            x = RankingEntryV1(
                int(p["ordinal"]),
                str(p["source_game_id"]),
                str(p["decision"]),
                None if p["selected_side"] is None else str(p["selected_side"]),
                None if p["selected_team_id"] is None else str(p["selected_team_id"]),
                bool(p["rank_eligible"]),
                None if p["recommendation_rank"] is None else int(p["recommendation_rank"]),
                str(p["upstream_gate_game_checksum"]),
                p["ranking_keys"] if isinstance(p["ranking_keys"], Mapping) else {},
            )
            if x.checksum != str(row["entry_checksum"]):
                raise RankingsIntegrityError("ranking entry reconstruction mismatch")
            entries.append(x)
        s = ProductionRankingsV1(
            str(r["run_id"]),
            str(r["requested_date"]),
            _time(r["as_of_time"], "as_of_time"),
            _time(r["ranked_at"], "ranked_at"),
            _policy(r["policy_json"]),
            str(r["upstream_recommendation_gate_snapshot_id"]),
            str(r["upstream_recommendation_gate_checksum"]),
            tuple(entries),
            tuple(dict(v) for v in json.loads(str(r["warnings_json"]))),
            secret_values=self.secret_values,
        )
        if s.checksum != str(r["snapshot_checksum"]) or s.canonical_json_bytes().decode() != str(r["canonical_json"]):
            raise RankingsIntegrityError("Rankings canonical reconstruction mismatch")
        value = self.value.get_by_snapshot_id(gate.gate.upstream_value_snapshot_id)
        pred = self.predictions.get_by_snapshot_id(gate.gate.upstream_predictions_snapshot_id)
        exact_pred_up = self.predictions.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=pred.predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=pred.predictions.upstream_data_quality_snapshot_id,
        )
        scheduled: dict[str, datetime] = {}
        for game in exact_pred_up.matchup_packet.packet.games:
            if game.schedule.scheduled_start_time is None:
                raise RankingsIntegrityError("historical Rankings schedule is incomplete")
            scheduled[game.source_game_id] = game.schedule.scheduled_start_time
        predictions_by_game = {game.source_game_id: game for game in pred.predictions.games}
        metrics: dict[str, Mapping[str, object]] = {}
        for value_game in value.value_engine.games:
            gate_game = next(game for game in gate.gate.games if game.source_game_id == value_game.source_game_id)
            outcome = next((item for item in value_game.outcomes if item.side == gate_game.selected_side), None)
            if gate_game.decision == "recommend" and outcome is None:
                raise RankingsIntegrityError("historical recommended game lacks selected Value outcome")
            audit_outcome = value_game.outcomes[0] if outcome is None else outcome
            prediction = predictions_by_game[value_game.source_game_id]
            metrics[value_game.source_game_id] = {
                "bookmaker_count": len(audit_outcome.eligible_pairs),
                "edge": audit_outcome.edge,
                "ev": audit_outcome.expected_value_per_unit,
                "interval_width": prediction.home_upper - prediction.home_lower,
                "lower_bound_clearance": audit_outcome.lower_bound_clearance,
            }
        replay = build_rankings(
            gate.gate,
            value_metrics_by_game=metrics,
            scheduled_start_by_game=scheduled,
            policy=s.policy,
            ranked_at=s.ranked_at,
            secret_values=self.secret_values,
        )
        if replay.canonical_json_bytes() != s.canonical_json_bytes():
            raise RankingsIntegrityError("historical Rankings deterministic replay mismatch")
        self.get_attempt_evidence(str(r["run_id"]), int(r["phase_attempt"]))
        a = PreModelArtifactV1(str(r["artifact_relpath"]), str(r["artifact_checksum"]), int(r["artifact_byte_count"]))
        verify_snapshot(
            artifact_root=self.artifact_root,
            artifact=a,
            expected_relpath=f"rankings/snapshots/{s.checksum}/rankings_v1.json",
            payload=s.as_dict(),
            content=s.canonical_json_bytes(),
            secret_values=self.secret_values,
        )
        return PersistedRankingsV1(
            str(r["snapshot_id"]),
            str(r["run_id"]),
            int(r["phase_attempt"]),
            s,
            a,
            str(r["phase_input_checksum"]),
            _time(r["created_at"], "created_at"),
            _time(r["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedRankingsV1:
        return self._verify(self._snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedRankingsV1:
        return self._verify(self._snapshot_row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedRankingsV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM ranking_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._verify(r)
