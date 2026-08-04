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
from app.model_feature_set.repository import ModelFeatureSetRepository, PersistedModelFeatureSetV1
from app.odds_weather.contracts import thaw_mapping
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)
from app.predictions.repository import PersistedPredictionsV1, PredictionsRepository
from app.value_engine.production import (
    EligibleBookPairV1,
    MoneylineGameValueV1,
    MoneylineOutcomeValueV1,
    ProductionValueEngineV1,
    ValuePolicyV1,
    build_value_engine,
)

VALUE_ENGINE_ATTEMPT_MANIFEST_CONTRACT = "DSE_VALUE_ENGINE_ATTEMPT_MANIFEST_V1"


class ValueEngineAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    CALCULATION_FAILED = "calculation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class ValueEngineRepositoryError(RuntimeError):
    pass


class ValueEngineNotFoundError(ValueEngineRepositoryError):
    pass


class ValueEnginePersistenceConflict(ValueEngineRepositoryError):
    pass


class ValueEngineIntegrityError(ValueEngineRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class ValueEngineUpstreamV1:
    predictions: PersistedPredictionsV1
    model_feature_set: PersistedModelFeatureSetV1
    data_quality: PersistedDataQualityV1

    def identities(self) -> tuple[PreModelUpstreamIdentityV1, ...]:
        return (
            PreModelUpstreamIdentityV1(
                "predictions", self.predictions.snapshot_id, self.predictions.predictions.checksum
            ),
            PreModelUpstreamIdentityV1(
                "model_feature_set", self.model_feature_set.snapshot_id, self.model_feature_set.feature_set.checksum
            ),
            PreModelUpstreamIdentityV1(
                "data_quality", self.data_quality.snapshot_id, self.data_quality.snapshot.checksum
            ),
        )


@dataclass(frozen=True, slots=True)
class ValueEngineAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: ValueEngineAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    market_inventory_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1


@dataclass(frozen=True, slots=True)
class PersistedValueEngineV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    value_engine: ProductionValueEngineV1
    artifact: PreModelArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueEngineIntegrityError(f"persisted {name} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise ValueEngineIntegrityError(f"persisted {name} is invalid") from exc


def _policy(payload: object) -> ValuePolicyV1:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueEngineIntegrityError("persisted value policy is invalid")
    value = ValuePolicyV1(**{key: item for key, item in payload.items() if key != "checksum"})
    if payload.get("checksum") != value.checksum:
        raise ValueEngineIntegrityError("value policy checksum mismatch")
    return value


class ValueEngineRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: ValuePolicyV1 = ValuePolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.predictions = PredictionsRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.model_feature_set = ModelFeatureSetRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.data_quality = DataQualityRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> ValueEngineUpstreamV1:
        predictions = self.predictions.get_latest_for_run(validate_run_id(run_id))
        if predictions is None:
            raise ValueEngineIntegrityError("Value Engine requires sealed Predictions")
        model = self.model_feature_set.get_by_snapshot_id(
            predictions.predictions.upstream_model_feature_set_snapshot_id
        )
        quality = self.data_quality.get_by_snapshot_id(predictions.predictions.upstream_data_quality_snapshot_id)
        if (
            model.feature_set.checksum != predictions.predictions.upstream_model_feature_set_checksum
            or quality.snapshot.checksum != predictions.predictions.upstream_data_quality_checksum
        ):
            raise ValueEngineIntegrityError("Value Engine upstream lineage mismatch")
        return ValueEngineUpstreamV1(predictions, model, quality)

    @staticmethod
    def _market_inputs(upstream: ValueEngineUpstreamV1) -> tuple[dict[str, Mapping[str, object]], dict[str, str]]:
        contexts: dict[str, Mapping[str, object]] = {}
        for game in upstream.model_feature_set.feature_set.games:
            context = game.market_context
            contexts[game.source_game_id] = {} if context is None else thaw_mapping(context)
        checksums = {g.source_game_id: g.market_context_checksum for g in upstream.model_feature_set.feature_set.games}
        return contexts, checksums

    def calculate(self, upstream: ValueEngineUpstreamV1, *, evaluated_at: datetime) -> ProductionValueEngineV1:
        contexts, checksums = self._market_inputs(upstream)
        return build_value_engine(
            upstream.predictions.predictions,
            contexts,
            checksums,
            evaluated_at=evaluated_at,
            policy=self.policy,
            model_feature_set_snapshot_id=upstream.model_feature_set.snapshot_id,
            model_feature_set_checksum=upstream.model_feature_set.feature_set.checksum,
            data_quality_snapshot_id=upstream.data_quality.snapshot_id,
            data_quality_checksum=upstream.data_quality.snapshot.checksum,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(connection: sqlite3.Connection, run_id: str, attempt: int) -> None:
        row = connection.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='value_engine'",
            (run_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "running" or int(row["attempt_count"]) != attempt:
            raise ValueEngineIntegrityError("Value Engine attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        snapshot: ProductionValueEngineV1 | None,
        upstream: ValueEngineUpstreamV1,
        requested_date: str,
        as_of_time: datetime,
        evaluated_at: datetime,
        phase_input_checksum: str,
        market_inventory_checksum: str,
        outcome: ValueEngineAttemptOutcome,
        warnings: tuple[Mapping[str, object], ...],
        created_at: datetime,
    ) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=VALUE_ENGINE_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="value_engine",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=requested_date,
            as_of_time=as_of_time,
            observed_at=evaluated_at,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream.identities(),
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=created_at,
            phase_input_evidence={
                "market_inventory_checksum": market_inventory_checksum,
                "policy": self.policy.as_dict(),
            },
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(c: sqlite3.Connection, m: PreModelAttemptManifestV1, a: PreModelArtifactV1) -> None:
        u = {v.phase_key: v for v in m.upstream}
        p = m.phase_input_evidence["policy"]
        if not isinstance(p, Mapping):
            raise ValueEngineIntegrityError("value manifest policy missing")
        c.execute(
            """INSERT INTO value_engine_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,evaluated_at,phase_input_checksum,policy_json,policy_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_model_feature_set_snapshot_id,upstream_model_feature_set_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,market_inventory_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.run_id,
                m.phase_attempt,
                m.requested_date,
                m.as_of_time.isoformat(),
                m.observed_at.isoformat(),
                m.phase_input_checksum,
                canonical_text(p),
                p["checksum"],
                u["predictions"].snapshot_id,
                u["predictions"].checksum,
                u["model_feature_set"].snapshot_id,
                u["model_feature_set"].checksum,
                u["data_quality"].snapshot_id,
                u["data_quality"].checksum,
                m.phase_input_evidence["market_inventory_checksum"],
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
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, snapshot: ProductionValueEngineV1
    ) -> PersistedValueEngineV1:
        try:
            existing = self.get_for_run_attempt(run_id, phase_attempt)
        except ValueEngineNotFoundError:
            existing = None
        if existing:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.value_engine.canonical_json_bytes() == snapshot.canonical_json_bytes()
            ):
                return existing
            raise ValueEnginePersistenceConflict("conflicting Value Engine replay")
        upstream = self.resolve_upstream(run_id)
        replay = self.calculate(upstream, evaluated_at=snapshot.evaluated_at)
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise ValueEngineIntegrityError("Value Engine result is not reproducible")
        now = self._now()
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            snapshot=snapshot,
            upstream=upstream,
            requested_date=snapshot.requested_date,
            as_of_time=snapshot.as_of_time,
            evaluated_at=snapshot.evaluated_at,
            phase_input_checksum=phase_input_checksum,
            market_inventory_checksum=snapshot.market_inventory_checksum,
            outcome=ValueEngineAttemptOutcome.ASSEMBLED,
            warnings=snapshot.warnings,
            created_at=now,
        )
        ma: PreModelArtifactV1 | None = None
        sa: PreModelArtifactV1 | None = None
        try:
            with self.database.connect() as c:
                self._active(c, run_id, phase_attempt)
            ma = publish_decision_manifest(
                manifest, self.artifact_root, "value_engine", secret_values=self.secret_values
            )
            rel = f"value_engine/snapshots/{snapshot.checksum}/value_engine_v1.json"
            sa = publish_snapshot(
                artifact_root=self.artifact_root,
                relpath=rel,
                payload=snapshot.as_dict(),
                content=snapshot.canonical_json_bytes(),
                secret_values=self.secret_values,
            )
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, manifest, ma)
                sid = f"value-engine:{snapshot.checksum}"
                c.execute(
                    """INSERT INTO value_engine_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,evaluated_at,contract_version,phase_input_checksum,policy_json,policy_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_model_feature_set_snapshot_id,upstream_model_feature_set_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,market_inventory_checksum,snapshot_checksum,game_count,outcome_count,warning_count,warnings_json,canonical_json,artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
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
                        snapshot.upstream_predictions_snapshot_id,
                        snapshot.upstream_predictions_checksum,
                        snapshot.upstream_model_feature_set_snapshot_id,
                        snapshot.upstream_model_feature_set_checksum,
                        snapshot.upstream_data_quality_snapshot_id,
                        snapshot.upstream_data_quality_checksum,
                        snapshot.market_inventory_checksum,
                        snapshot.checksum,
                        len(snapshot.games),
                        2 * len(snapshot.games),
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
                        "INSERT INTO value_engine_games VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            run_id,
                            phase_attempt,
                            game.ordinal,
                            game.source_game_id,
                            game.prediction_checksum,
                            game.market_context_checksum,
                            canonical_text(game.as_dict()),
                            game.checksum,
                        ),
                    )
                    for outcome_ordinal, outcome in enumerate(game.outcomes, 1):
                        c.execute(
                            """INSERT INTO value_engine_outcomes(snapshot_id,source_game_id,ordinal,side,outcome_team_id,prediction_probability,probability_lower,probability_upper,availability,eligible_bookmaker_count,best_price,consensus_no_vig_probability,break_even_probability,edge,expected_value_per_unit,lower_bound_clearance,freshness_state,market_context_checksum,prediction_checksum,exclusion_json,canonical_json,row_checksum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                sid,
                                game.source_game_id,
                                outcome_ordinal,
                                outcome.side,
                                outcome.outcome_team_id,
                                outcome.prediction_probability,
                                outcome.probability_lower,
                                outcome.probability_upper,
                                outcome.availability,
                                len(outcome.eligible_pairs),
                                outcome.best_price,
                                outcome.consensus_no_vig_probability,
                                outcome.break_even_probability,
                                outcome.edge,
                                outcome.expected_value_per_unit,
                                outcome.lower_bound_clearance,
                                outcome.freshness_state,
                                outcome.market_context_checksum,
                                outcome.prediction_checksum,
                                canonical_text(dict(outcome.exclusion_counts)),
                                canonical_text(outcome.as_dict()),
                                outcome.checksum,
                            ),
                        )
                        for ordinal, pair in enumerate(outcome.eligible_pairs, 1):
                            c.execute(
                                "INSERT INTO value_engine_book_pairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    sid,
                                    game.source_game_id,
                                    outcome.side,
                                    ordinal,
                                    pair.bookmaker_key,
                                    pair.home_price,
                                    pair.away_price,
                                    pair.home_implied_probability,
                                    pair.away_implied_probability,
                                    pair.home_no_vig_probability,
                                    pair.away_no_vig_probability,
                                    pair.effective_timestamp.isoformat(),
                                    pair.retrieval_timestamp.isoformat(),
                                    pair.checksum,
                                    canonical_text(pair.as_dict()),
                                ),
                            )
                self._verify_relational(c, sid, snapshot)
                c.execute(
                    "UPDATE value_engine_snapshots SET sealed_at=? WHERE snapshot_id=?", (self._now().isoformat(), sid)
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(sa, ma)
            raise ValueEnginePersistenceConflict("conflicting Value Engine evidence") from exc
        except Exception:
            self._cleanup(sa, ma)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    @staticmethod
    def _verify_relational(c: sqlite3.Connection, sid: str, s: ProductionValueEngineV1) -> None:
        games = c.execute("SELECT * FROM value_engine_games WHERE snapshot_id=? ORDER BY ordinal", (sid,)).fetchall()
        if len(games) != len(s.games):
            raise ValueEngineIntegrityError("value game count mismatch")
        for row, game in zip(games, s.games, strict=True):
            if str(row["row_checksum"]) != game.checksum or str(row["canonical_json"]) != canonical_text(
                game.as_dict()
            ):
                raise ValueEngineIntegrityError("value game row mismatch")
            outcomes = c.execute(
                "SELECT * FROM value_engine_outcomes WHERE snapshot_id=? AND source_game_id=? ORDER BY CASE side WHEN 'home' THEN 1 ELSE 2 END",
                (sid, game.source_game_id),
            ).fetchall()
            if len(outcomes) != 2:
                raise ValueEngineIntegrityError("value outcome count mismatch")
            for orow, outcome in zip(outcomes, game.outcomes, strict=True):
                if str(orow["row_checksum"]) != outcome.checksum or str(orow["canonical_json"]) != canonical_text(
                    outcome.as_dict()
                ):
                    raise ValueEngineIntegrityError("value outcome row mismatch")
                pairs = c.execute(
                    "SELECT canonical_json,pair_checksum FROM value_engine_book_pairs WHERE snapshot_id=? AND source_game_id=? AND side=? ORDER BY ordinal",
                    (sid, game.source_game_id, outcome.side),
                ).fetchall()
                if [(str(r["canonical_json"]), str(r["pair_checksum"])) for r in pairs] != [
                    (canonical_text(p.as_dict()), p.checksum) for p in outcome.eligible_pairs
                ]:
                    raise ValueEngineIntegrityError("value book-pair evidence mismatch")

    def _cleanup(self, *arts: PreModelArtifactV1 | None) -> None:
        for art in arts:
            if art:
                cleanup_owned_artifact(self.artifact_root, art)

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        evaluated_at: datetime,
        outcome: ValueEngineAttemptOutcome,
        upstream: ValueEngineUpstreamV1,
        market_inventory_checksum: str,
        warnings: tuple[Mapping[str, object], ...],
    ) -> ValueEngineAttemptEvidenceV1:
        if outcome is ValueEngineAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt cannot be assembled")
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except ValueEngineNotFoundError:
            existing = None
        if existing:
            if (
                existing.outcome is outcome
                and existing.phase_input_checksum == phase_input_checksum
                and existing.market_inventory_checksum == market_inventory_checksum
                and existing.warnings == warnings
            ):
                return existing
            raise ValueEnginePersistenceConflict("conflicting failed Value Engine attempt")
        now = self._now()
        p = upstream.predictions.predictions
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            snapshot=None,
            upstream=upstream,
            requested_date=p.requested_date,
            as_of_time=p.as_of_time,
            evaluated_at=evaluated_at,
            phase_input_checksum=phase_input_checksum,
            market_inventory_checksum=market_inventory_checksum,
            outcome=outcome,
            warnings=warnings,
            created_at=now,
        )
        art = publish_decision_manifest(manifest, self.artifact_root, "value_engine", secret_values=self.secret_values)
        try:
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, manifest, art)
        except Exception:
            cleanup_owned_artifact(self.artifact_root, art)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _attempt_row(self, run_id: str, attempt: int) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM value_engine_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if r is None:
            raise ValueEngineNotFoundError("Value Engine attempt not found")
        return r

    def _manifest_from_row(self, r: sqlite3.Row) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=VALUE_ENGINE_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="value_engine",
            run_id=str(r["run_id"]),
            phase_attempt=int(r["phase_attempt"]),
            requested_date=str(r["requested_date"]),
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=_time(r["evaluated_at"], "evaluated_at"),
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "predictions", str(r["upstream_predictions_snapshot_id"]), str(r["upstream_predictions_checksum"])
                ),
                PreModelUpstreamIdentityV1(
                    "model_feature_set",
                    str(r["upstream_model_feature_set_snapshot_id"]),
                    str(r["upstream_model_feature_set_checksum"]),
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
            phase_input_evidence={
                "market_inventory_checksum": str(r["market_inventory_checksum"]),
                "policy": _policy(r["policy_json"]).as_dict(),
            },
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

    def get_attempt_evidence(self, run_id: str, attempt: int) -> ValueEngineAttemptEvidenceV1:
        r = self._attempt_row(run_id, attempt)
        m = self.get_attempt_manifest(run_id, attempt)
        return ValueEngineAttemptEvidenceV1(
            m.run_id,
            m.phase_attempt,
            ValueEngineAttemptOutcome(m.outcome),
            m.snapshot_checksum,
            m.phase_input_checksum,
            str(r["market_inventory_checksum"]),
            m.warnings,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[ValueEngineAttemptEvidenceV1, ...]:
        with self.database.connect() as c:
            a = tuple(
                int(r[0])
                for r in c.execute(
                    "SELECT phase_attempt FROM value_engine_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, n) for n in a)

    @staticmethod
    def _pair_from(payload: Mapping[str, object]) -> EligibleBookPairV1:
        def number(name: str) -> float:
            value = payload[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueEngineIntegrityError(f"{name} must be numeric")
            return float(value)

        return EligibleBookPairV1(
            str(payload["bookmaker_key"]),
            number("home_price"),
            number("away_price"),
            number("home_implied_probability"),
            number("away_implied_probability"),
            number("home_no_vig_probability"),
            number("away_no_vig_probability"),
            _time(payload["effective_timestamp"], "effective_timestamp"),
            _time(payload["retrieval_timestamp"], "retrieval_timestamp"),
            str(payload["evidence_checksum"]),
        )

    @classmethod
    def _outcome_from(cls, p: Mapping[str, object]) -> MoneylineOutcomeValueV1:
        def number(name: str) -> float:
            value = p[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueEngineIntegrityError(f"{name} must be numeric")
            return float(value)

        def optional_number(name: str) -> float | None:
            return None if p[name] is None else number(name)

        raw_pairs = p["eligible_pairs"]
        pairs = raw_pairs if isinstance(raw_pairs, (list, tuple)) else ()
        raw_books = p["best_price_bookmakers"]
        books = raw_books if isinstance(raw_books, (list, tuple)) else ()
        return MoneylineOutcomeValueV1(
            str(p["side"]),
            str(p["outcome_team_id"]),
            number("prediction_probability"),
            number("probability_lower"),
            number("probability_upper"),
            str(p["availability"]),
            tuple(cls._pair_from(v) for v in pairs if isinstance(v, Mapping)),
            p["excluded_offer_counts"] if isinstance(p["excluded_offer_counts"], Mapping) else {},
            optional_number("best_price"),
            tuple(str(v) for v in books),
            optional_number("consensus_no_vig_probability"),
            optional_number("break_even_probability"),
            optional_number("edge"),
            optional_number("expected_value_per_unit"),
            optional_number("lower_bound_clearance"),
            str(p["freshness_state"]),
            str(p["market_context_checksum"]),
            str(p["prediction_checksum"]),
        )

    def _snapshot_row(self, where: str, vals: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(f"SELECT * FROM value_engine_snapshots WHERE {where}", vals).fetchone()
        if r is None or r["sealed_at"] is None:
            raise ValueEngineNotFoundError("sealed Value Engine snapshot not found")
        return r

    def _verify(self, r: sqlite3.Row) -> PersistedValueEngineV1:
        pred = self.predictions.get_by_snapshot_id(str(r["upstream_predictions_snapshot_id"]))
        model = self.model_feature_set.get_by_snapshot_id(str(r["upstream_model_feature_set_snapshot_id"]))
        quality = self.data_quality.get_by_snapshot_id(str(r["upstream_data_quality_snapshot_id"]))
        if (
            pred.predictions.checksum != str(r["upstream_predictions_checksum"])
            or model.feature_set.checksum != str(r["upstream_model_feature_set_checksum"])
            or quality.snapshot.checksum != str(r["upstream_data_quality_checksum"])
        ):
            raise ValueEngineIntegrityError("historical Value Engine upstream mismatch")
        with self.database.connect() as c:
            rows = c.execute(
                "SELECT canonical_json,row_checksum FROM value_engine_games WHERE snapshot_id=? ORDER BY ordinal",
                (str(r["snapshot_id"]),),
            ).fetchall()
        games = []
        for row in rows:
            p = json.loads(str(row["canonical_json"]))
            outs = tuple(self._outcome_from(v) for v in p["outcomes"] if isinstance(v, Mapping))
            g = MoneylineGameValueV1(
                int(p["ordinal"]),
                str(p["source_game_id"]),
                str(p["market_context_checksum"]),
                str(p["prediction_checksum"]),
                (outs[0], outs[1]),
            )
            if g.checksum != str(row["row_checksum"]):
                raise ValueEngineIntegrityError("value game reconstruction mismatch")
            games.append(g)
        s = ProductionValueEngineV1(
            str(r["run_id"]),
            str(r["requested_date"]),
            _time(r["as_of_time"], "as_of_time"),
            _time(r["evaluated_at"], "evaluated_at"),
            _policy(r["policy_json"]),
            str(r["upstream_predictions_snapshot_id"]),
            str(r["upstream_predictions_checksum"]),
            str(r["upstream_model_feature_set_snapshot_id"]),
            str(r["upstream_model_feature_set_checksum"]),
            str(r["upstream_data_quality_snapshot_id"]),
            str(r["upstream_data_quality_checksum"]),
            str(r["market_inventory_checksum"]),
            tuple(games),
            tuple(dict(v) for v in json.loads(str(r["warnings_json"]))),
            secret_values=self.secret_values,
        )
        if s.checksum != str(r["snapshot_checksum"]) or s.canonical_json_bytes().decode() != str(r["canonical_json"]):
            raise ValueEngineIntegrityError("Value Engine canonical reconstruction mismatch")
        exact_upstream = ValueEngineUpstreamV1(pred, model, quality)
        contexts, checksums = self._market_inputs(exact_upstream)
        replay = build_value_engine(
            pred.predictions,
            contexts,
            checksums,
            evaluated_at=s.evaluated_at,
            policy=s.policy,
            model_feature_set_snapshot_id=model.snapshot_id,
            model_feature_set_checksum=model.feature_set.checksum,
            data_quality_snapshot_id=quality.snapshot_id,
            data_quality_checksum=quality.snapshot.checksum,
            secret_values=self.secret_values,
        )
        if replay.canonical_json_bytes() != s.canonical_json_bytes():
            raise ValueEngineIntegrityError("historical Value Engine deterministic replay mismatch")
        with self.database.connect() as connection:
            self._verify_relational(connection, str(r["snapshot_id"]), s)
        self.get_attempt_evidence(str(r["run_id"]), int(r["phase_attempt"]))
        a = PreModelArtifactV1(str(r["artifact_relpath"]), str(r["artifact_checksum"]), int(r["artifact_byte_count"]))
        verify_snapshot(
            artifact_root=self.artifact_root,
            artifact=a,
            expected_relpath=f"value_engine/snapshots/{s.checksum}/value_engine_v1.json",
            payload=s.as_dict(),
            content=s.canonical_json_bytes(),
            secret_values=self.secret_values,
        )
        return PersistedValueEngineV1(
            str(r["snapshot_id"]),
            str(r["run_id"]),
            int(r["phase_attempt"]),
            s,
            a,
            str(r["phase_input_checksum"]),
            _time(r["created_at"], "created_at"),
            _time(r["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedValueEngineV1:
        return self._verify(self._snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedValueEngineV1:
        return self._verify(self._snapshot_row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedValueEngineV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM value_engine_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._verify(r)
