from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.data_quality.repository import DataQualityRepository, PersistedDataQualityV1
from app.database import Database
from app.decision_evidence import (
    create_decision_manifest,
    publish_decision_manifest,
    publish_snapshot,
    verify_decision_manifest,
    verify_snapshot,
)
from app.identifiers import validate_run_id
from app.matchup_packet.repository import MatchupPacketRepository
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)
from app.predictions.repository import PersistedPredictionsV1, PredictionsRepository
from app.recommendation_gate.production import (
    GateGameV1,
    GateResultV1,
    GateSideEvaluationV1,
    ProductionRecommendationGateV1,
    RecommendationPolicyV1,
    evaluate_recommendation_gate,
)
from app.value_engine.repository import PersistedValueEngineV1, ValueEngineRepository

RECOMMENDATION_GATE_ATTEMPT_MANIFEST_CONTRACT = "DSE_RECOMMENDATION_GATE_ATTEMPT_MANIFEST_V1"


class RecommendationGateAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    EVALUATION_FAILED = "evaluation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class RecommendationGateRepositoryError(RuntimeError):
    pass


class RecommendationGateNotFoundError(RecommendationGateRepositoryError):
    pass


class RecommendationGatePersistenceConflict(RecommendationGateRepositoryError):
    pass


class RecommendationGateIntegrityError(RecommendationGateRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class RecommendationGateUpstreamV1:
    value: PersistedValueEngineV1
    predictions: PersistedPredictionsV1
    quality: PersistedDataQualityV1

    def identities(self) -> tuple[PreModelUpstreamIdentityV1, ...]:
        return (
            PreModelUpstreamIdentityV1("value_engine", self.value.snapshot_id, self.value.value_engine.checksum),
            PreModelUpstreamIdentityV1(
                "predictions", self.predictions.snapshot_id, self.predictions.predictions.checksum
            ),
            PreModelUpstreamIdentityV1("data_quality", self.quality.snapshot_id, self.quality.snapshot.checksum),
        )


@dataclass(frozen=True, slots=True)
class RecommendationGateAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: RecommendationGateAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1


@dataclass(frozen=True, slots=True)
class PersistedRecommendationGateV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    gate: ProductionRecommendationGateV1
    artifact: PreModelArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(v: object, n: str) -> datetime:
    if not isinstance(v, str):
        raise RecommendationGateIntegrityError(f"persisted {n} is not text")
    try:
        return aware_utc(datetime.fromisoformat(v.replace("Z", "+00:00")), n)
    except ValueError as exc:
        raise RecommendationGateIntegrityError(f"persisted {n} invalid") from exc


def _policy(v: object) -> RecommendationPolicyV1:
    if isinstance(v, str):
        v = json.loads(v)
    if not isinstance(v, dict):
        raise RecommendationGateIntegrityError("gate policy invalid")
    p = RecommendationPolicyV1(**{k: x for k, x in v.items() if k != "checksum"})
    if v.get("checksum") != p.checksum:
        raise RecommendationGateIntegrityError("gate policy checksum mismatch")
    return p


class RecommendationGateRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: RecommendationPolicyV1 = RecommendationPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.value = ValueEngineRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.predictions = PredictionsRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.quality = DataQualityRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.packet = MatchupPacketRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> RecommendationGateUpstreamV1:
        value = self.value.get_latest_for_run(validate_run_id(run_id))
        if value is None:
            raise RecommendationGateIntegrityError("Gate requires sealed Value Engine")
        predictions = self.predictions.get_by_snapshot_id(value.value_engine.upstream_predictions_snapshot_id)
        quality = self.quality.get_by_snapshot_id(value.value_engine.upstream_data_quality_snapshot_id)
        if (
            predictions.predictions.checksum != value.value_engine.upstream_predictions_checksum
            or quality.snapshot.checksum != value.value_engine.upstream_data_quality_checksum
        ):
            raise RecommendationGateIntegrityError("Gate upstream lineage mismatch")
        return RecommendationGateUpstreamV1(value, predictions, quality)

    def evaluate(self, u: RecommendationGateUpstreamV1, *, evaluated_at: datetime) -> ProductionRecommendationGateV1:
        byq = {g.source_game_id: g for g in u.quality.snapshot.games}
        predictions_by_game = {g.source_game_id: g for g in u.predictions.predictions.games}
        pred_up = self.predictions.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=u.predictions.predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=u.predictions.predictions.upstream_data_quality_snapshot_id,
        )
        starts: dict[str, datetime] = {}
        for game in pred_up.matchup_packet.packet.games:
            scheduled_start = game.schedule.scheduled_start_time
            if scheduled_start is None:
                raise RecommendationGateIntegrityError("gate requires a scheduled start time")
            starts[game.source_game_id] = scheduled_start
        return evaluate_recommendation_gate(
            u.value.value_engine,
            predictions_by_game=predictions_by_game,
            quality_games_by_id=byq,
            scheduled_start_by_game=starts,
            policy=self.policy,
            evaluated_at=evaluated_at,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(c: sqlite3.Connection, run_id: str, attempt: int) -> None:
        r = c.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='recommendation_gate'",
            (run_id,),
        ).fetchone()
        if r is None or str(r["status"]) != "running" or int(r["attempt_count"]) != attempt:
            raise RecommendationGateIntegrityError("Gate attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        u: RecommendationGateUpstreamV1,
        snapshot: ProductionRecommendationGateV1 | None,
        evaluated_at: datetime,
        phase_input_checksum: str,
        outcome: RecommendationGateAttemptOutcome,
        warnings: tuple[Mapping[str, object], ...],
        created_at: datetime,
    ) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=RECOMMENDATION_GATE_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="recommendation_gate",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=u.value.value_engine.requested_date,
            as_of_time=u.value.value_engine.as_of_time,
            observed_at=evaluated_at,
            phase_input_checksum=phase_input_checksum,
            upstream=u.identities(),
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=created_at,
            phase_input_evidence={"policy": self.policy.as_dict()},
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(c: sqlite3.Connection, m: PreModelAttemptManifestV1, a: PreModelArtifactV1) -> None:
        u = {v.phase_key: v for v in m.upstream}
        p = m.phase_input_evidence["policy"]
        if not isinstance(p, Mapping):
            raise RecommendationGateIntegrityError("gate manifest policy missing")
        c.execute(
            """INSERT INTO recommendation_gate_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,evaluated_at,phase_input_checksum,policy_json,policy_checksum,upstream_value_engine_snapshot_id,upstream_value_engine_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.run_id,
                m.phase_attempt,
                m.requested_date,
                m.as_of_time.isoformat(),
                m.observed_at.isoformat(),
                m.phase_input_checksum,
                canonical_text(p),
                p["checksum"],
                u["value_engine"].snapshot_id,
                u["value_engine"].checksum,
                u["predictions"].snapshot_id,
                u["predictions"].checksum,
                u["data_quality"].snapshot_id,
                u["data_quality"].checksum,
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
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, snapshot: ProductionRecommendationGateV1
    ) -> PersistedRecommendationGateV1:
        try:
            e = self.get_for_run_attempt(run_id, phase_attempt)
        except RecommendationGateNotFoundError:
            e = None
        if e:
            if (
                e.phase_input_checksum == phase_input_checksum
                and e.gate.canonical_json_bytes() == snapshot.canonical_json_bytes()
            ):
                return e
            raise RecommendationGatePersistenceConflict("conflicting Gate replay")
        u = self.resolve_upstream(run_id)
        replay = self.evaluate(u, evaluated_at=snapshot.evaluated_at)
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise RecommendationGateIntegrityError("Gate result is not reproducible")
        now = self._now()
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            u=u,
            snapshot=snapshot,
            evaluated_at=snapshot.evaluated_at,
            phase_input_checksum=phase_input_checksum,
            outcome=RecommendationGateAttemptOutcome.ASSEMBLED,
            warnings=snapshot.warnings,
            created_at=now,
        )
        ma = None
        sa = None
        try:
            with self.database.connect() as c:
                self._active(c, run_id, phase_attempt)
            ma = publish_decision_manifest(
                m, self.artifact_root, "recommendation_gate", secret_values=self.secret_values
            )
            rel = f"recommendation_gate/snapshots/{snapshot.checksum}/recommendation_gate_v1.json"
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
                sid = f"recommendation-gate:{snapshot.checksum}"
                c.execute(
                    """INSERT INTO recommendation_gate_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,evaluated_at,contract_version,phase_input_checksum,policy_json,policy_checksum,upstream_value_engine_snapshot_id,upstream_value_engine_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,snapshot_checksum,game_count,recommended_game_count,warning_count,warnings_json,canonical_json,artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        sid,
                        run_id,
                        phase_attempt,
                        snapshot.requested_date,
                        snapshot.as_of_time.isoformat(),
                        snapshot.evaluated_at.isoformat(),
                        snapshot.contract_version,
                        phase_input_checksum,
                        canonical_text(snapshot.policy.as_dict()),
                        snapshot.policy.checksum,
                        snapshot.upstream_value_snapshot_id,
                        snapshot.upstream_value_checksum,
                        snapshot.upstream_predictions_snapshot_id,
                        snapshot.upstream_predictions_checksum,
                        snapshot.upstream_data_quality_snapshot_id,
                        snapshot.upstream_data_quality_checksum,
                        snapshot.checksum,
                        len(snapshot.games),
                        sum(g.decision == "recommend" for g in snapshot.games),
                        len(snapshot.warnings),
                        canonical_text(list(snapshot.warnings)),
                        snapshot.canonical_json_bytes().decode(),
                        sa.relpath,
                        sa.checksum,
                        sa.byte_count,
                        now.isoformat(),
                    ),
                )
                for game in snapshot.games:
                    c.execute(
                        "INSERT INTO recommendation_gate_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            run_id,
                            phase_attempt,
                            game.ordinal,
                            game.source_game_id,
                            game.decision,
                            game.selected_side,
                            game.selected_team_id,
                            game.value_game_checksum,
                            game.prediction_checksum,
                            canonical_text(game.as_dict()),
                            game.checksum,
                        ),
                    )
                    for ordinal, side in enumerate(game.sides, 1):
                        c.execute(
                            "INSERT INTO recommendation_gate_sides VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                sid,
                                game.source_game_id,
                                ordinal,
                                side.side,
                                side.outcome_team_id,
                                side.decision,
                                side.value_checksum,
                                len(side.results),
                                canonical_text(list(side.reason_codes)),
                                side.checksum,
                                canonical_text(side.as_dict()),
                            ),
                        )
                        for result in side.results:
                            c.execute(
                                "INSERT INTO recommendation_gate_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    sid,
                                    game.source_game_id,
                                    side.side,
                                    result.ordinal,
                                    result.code,
                                    int(result.passed),
                                    canonical_text(result.threshold),
                                    canonical_text(result.observed_value),
                                    result.reason,
                                    result.evaluated_at.isoformat(),
                                    result.policy_checksum,
                                    result.source_checksum,
                                    result.checksum,
                                    canonical_text(result.as_dict()),
                                ),
                            )
                self._verify_relational(c, sid, snapshot)
                c.execute(
                    "UPDATE recommendation_gate_snapshots SET sealed_at=? WHERE snapshot_id=?",
                    (self._now().isoformat(), sid),
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(sa, ma)
            raise RecommendationGatePersistenceConflict("conflicting Gate evidence") from exc
        except Exception:
            self._cleanup(sa, ma)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    @staticmethod
    def _verify_relational(c: sqlite3.Connection, sid: str, s: ProductionRecommendationGateV1) -> None:
        rows = c.execute(
            "SELECT canonical_json,row_checksum FROM recommendation_gate_games WHERE snapshot_id=? ORDER BY ordinal",
            (sid,),
        ).fetchall()
        if [(str(r["canonical_json"]), str(r["row_checksum"])) for r in rows] != [
            (canonical_text(g.as_dict()), g.checksum) for g in s.games
        ]:
            raise RecommendationGateIntegrityError("Gate game relational mismatch")
        for g in s.games:
            sides = c.execute(
                "SELECT canonical_json,side_checksum FROM recommendation_gate_sides WHERE snapshot_id=? AND source_game_id=? ORDER BY ordinal",
                (sid, g.source_game_id),
            ).fetchall()
            if [(str(r["canonical_json"]), str(r["side_checksum"])) for r in sides] != [
                (canonical_text(x.as_dict()), x.checksum) for x in g.sides
            ]:
                raise RecommendationGateIntegrityError("Gate side mismatch")
            for x in g.sides:
                results = c.execute(
                    "SELECT canonical_json,result_checksum FROM recommendation_gate_results WHERE snapshot_id=? AND source_game_id=? AND side=? ORDER BY ordinal",
                    (sid, g.source_game_id, x.side),
                ).fetchall()
                if [(str(r["canonical_json"]), str(r["result_checksum"])) for r in results] != [
                    (canonical_text(y.as_dict()), y.checksum) for y in x.results
                ]:
                    raise RecommendationGateIntegrityError("Gate result mismatch")

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
        evaluated_at: datetime,
        outcome: RecommendationGateAttemptOutcome,
        upstream: RecommendationGateUpstreamV1,
        warnings: tuple[Mapping[str, object], ...],
    ) -> RecommendationGateAttemptEvidenceV1:
        if outcome is RecommendationGateAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt cannot be assembled")
        try:
            e = self.get_attempt_evidence(run_id, phase_attempt)
        except RecommendationGateNotFoundError:
            e = None
        if e:
            if e.outcome is outcome and e.phase_input_checksum == phase_input_checksum and e.warnings == warnings:
                return e
            raise RecommendationGatePersistenceConflict("conflicting failed Gate attempt")
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            u=upstream,
            snapshot=None,
            evaluated_at=evaluated_at,
            phase_input_checksum=phase_input_checksum,
            outcome=outcome,
            warnings=warnings,
            created_at=self._now(),
        )
        a = publish_decision_manifest(m, self.artifact_root, "recommendation_gate", secret_values=self.secret_values)
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
                "SELECT * FROM recommendation_gate_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if r is None:
            raise RecommendationGateNotFoundError("Gate attempt not found")
        return r

    def _manifest_from_row(self, r: sqlite3.Row) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=RECOMMENDATION_GATE_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="recommendation_gate",
            run_id=str(r["run_id"]),
            phase_attempt=int(r["phase_attempt"]),
            requested_date=str(r["requested_date"]),
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=_time(r["evaluated_at"], "evaluated_at"),
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "value_engine",
                    str(r["upstream_value_engine_snapshot_id"]),
                    str(r["upstream_value_engine_checksum"]),
                ),
                PreModelUpstreamIdentityV1(
                    "predictions", str(r["upstream_predictions_snapshot_id"]), str(r["upstream_predictions_checksum"])
                ),
                PreModelUpstreamIdentityV1(
                    "data_quality",
                    str(r["upstream_data_quality_snapshot_id"]),
                    str(r["upstream_data_quality_checksum"]),
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

    def get_attempt_evidence(self, run_id: str, attempt: int) -> RecommendationGateAttemptEvidenceV1:
        r = self._attempt_row(run_id, attempt)
        m = self.get_attempt_manifest(run_id, attempt)
        return RecommendationGateAttemptEvidenceV1(
            m.run_id,
            m.phase_attempt,
            RecommendationGateAttemptOutcome(m.outcome),
            m.snapshot_checksum,
            m.phase_input_checksum,
            m.warnings,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[RecommendationGateAttemptEvidenceV1, ...]:
        with self.database.connect() as c:
            a = tuple(
                int(r[0])
                for r in c.execute(
                    "SELECT phase_attempt FROM recommendation_gate_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, n) for n in a)

    @staticmethod
    def _result(p: Mapping[str, object]) -> GateResultV1:
        ordinal = p["ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise RecommendationGateIntegrityError("gate result ordinal must be an integer")
        return GateResultV1(
            str(p["code"]),
            p["threshold"],
            p["observed_value"],
            bool(p["passed"]),
            str(p["reason"]),
            str(p["source_checksum"]),
            _time(p["evaluated_at"], "evaluated_at"),
            str(p["policy_checksum"]),
            ordinal,
        )

    @classmethod
    def _side(cls, p: Mapping[str, object]) -> GateSideEvaluationV1:
        raw_results = p["results"]
        results = raw_results if isinstance(raw_results, (list, tuple)) else ()
        raw_reasons = p["reason_codes"]
        reasons = raw_reasons if isinstance(raw_reasons, (list, tuple)) else ()
        return GateSideEvaluationV1(
            str(p["side"]),
            str(p["outcome_team_id"]),
            str(p["decision"]),
            str(p["value_checksum"]),
            tuple(cls._result(v) for v in results if isinstance(v, Mapping)),
            tuple(str(v) for v in reasons),
        )

    def _snapshot_row(self, w: str, v: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(f"SELECT * FROM recommendation_gate_snapshots WHERE {w}", v).fetchone()
        if r is None or r["sealed_at"] is None:
            raise RecommendationGateNotFoundError("sealed Gate snapshot not found")
        return r

    def _verify(self, r: sqlite3.Row) -> PersistedRecommendationGateV1:
        value = self.value.get_by_snapshot_id(str(r["upstream_value_engine_snapshot_id"]))
        pred = self.predictions.get_by_snapshot_id(str(r["upstream_predictions_snapshot_id"]))
        quality = self.quality.get_by_snapshot_id(str(r["upstream_data_quality_snapshot_id"]))
        if (
            value.value_engine.checksum != str(r["upstream_value_engine_checksum"])
            or pred.predictions.checksum != str(r["upstream_predictions_checksum"])
            or quality.snapshot.checksum != str(r["upstream_data_quality_checksum"])
        ):
            raise RecommendationGateIntegrityError("historical Gate upstream mismatch")
        with self.database.connect() as c:
            rows = c.execute(
                "SELECT canonical_json,row_checksum FROM recommendation_gate_games WHERE snapshot_id=? ORDER BY ordinal",
                (str(r["snapshot_id"]),),
            ).fetchall()
        games = []
        for row in rows:
            p = json.loads(str(row["canonical_json"]))
            sides = tuple(self._side(v) for v in p["sides"] if isinstance(v, Mapping))
            g = GateGameV1(
                int(p["ordinal"]),
                str(p["source_game_id"]),
                str(p["decision"]),
                None if p["selected_side"] is None else str(p["selected_side"]),
                None if p["selected_team_id"] is None else str(p["selected_team_id"]),
                str(p["value_game_checksum"]),
                str(p["prediction_checksum"]),
                str(p["quality_disposition"]),
                (sides[0], sides[1]),
            )
            if g.checksum != str(row["row_checksum"]):
                raise RecommendationGateIntegrityError("Gate game reconstruction mismatch")
            games.append(g)
        s = ProductionRecommendationGateV1(
            str(r["run_id"]),
            str(r["requested_date"]),
            _time(r["as_of_time"], "as_of_time"),
            _time(r["evaluated_at"], "evaluated_at"),
            _policy(r["policy_json"]),
            str(r["upstream_value_engine_snapshot_id"]),
            str(r["upstream_value_engine_checksum"]),
            str(r["upstream_predictions_snapshot_id"]),
            str(r["upstream_predictions_checksum"]),
            str(r["upstream_data_quality_snapshot_id"]),
            str(r["upstream_data_quality_checksum"]),
            tuple(games),
            tuple(dict(v) for v in json.loads(str(r["warnings_json"]))),
            secret_values=self.secret_values,
        )
        if s.checksum != str(r["snapshot_checksum"]) or s.canonical_json_bytes().decode() != str(r["canonical_json"]):
            raise RecommendationGateIntegrityError("Gate canonical reconstruction mismatch")
        replay_repository = RecommendationGateUpstreamV1(value, pred, quality)
        exact_pred_up = self.predictions.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=pred.predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=pred.predictions.upstream_data_quality_snapshot_id,
        )
        scheduled: dict[str, datetime] = {}
        for game in exact_pred_up.matchup_packet.packet.games:
            if game.schedule.scheduled_start_time is None:
                raise RecommendationGateIntegrityError("historical Gate schedule is incomplete")
            scheduled[game.source_game_id] = game.schedule.scheduled_start_time
        replay = evaluate_recommendation_gate(
            replay_repository.value.value_engine,
            predictions_by_game={game.source_game_id: game for game in pred.predictions.games},
            quality_games_by_id={game.source_game_id: game for game in quality.snapshot.games},
            scheduled_start_by_game=scheduled,
            policy=s.policy,
            evaluated_at=s.evaluated_at,
            secret_values=self.secret_values,
        )
        if replay.canonical_json_bytes() != s.canonical_json_bytes():
            raise RecommendationGateIntegrityError("historical Gate deterministic replay mismatch")
        with self.database.connect() as connection:
            self._verify_relational(connection, str(r["snapshot_id"]), s)
        self.get_attempt_evidence(str(r["run_id"]), int(r["phase_attempt"]))
        a = PreModelArtifactV1(str(r["artifact_relpath"]), str(r["artifact_checksum"]), int(r["artifact_byte_count"]))
        verify_snapshot(
            artifact_root=self.artifact_root,
            artifact=a,
            expected_relpath=f"recommendation_gate/snapshots/{s.checksum}/recommendation_gate_v1.json",
            payload=s.as_dict(),
            content=s.canonical_json_bytes(),
            secret_values=self.secret_values,
        )
        return PersistedRecommendationGateV1(
            str(r["snapshot_id"]),
            str(r["run_id"]),
            int(r["phase_attempt"]),
            s,
            a,
            str(r["phase_input_checksum"]),
            _time(r["created_at"], "created_at"),
            _time(r["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedRecommendationGateV1:
        return self._verify(self._snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedRecommendationGateV1:
        return self._verify(self._snapshot_row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedRecommendationGateV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM recommendation_gate_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._verify(r)
