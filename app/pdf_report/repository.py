from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import cast

from app.database import Database
from app.decision_evidence import create_decision_manifest, publish_decision_manifest, verify_decision_manifest
from app.identifiers import validate_run_id
from app.pdf_report.artifact import (
    ProductionPdfReportArtifactsV1,
    publish_production_pdf_report_artifacts,
    verify_production_pdf_report_artifacts,
)
from app.pdf_report.policy import PdfReportPolicyV1
from app.pdf_report.production import (
    PDF_REPORT_ATTEMPT_MANIFEST_CONTRACT,
    PdfReportGameV1,
    PdfReportOutcomeV1,
    ProductionPdfReportV1,
    assemble_production_pdf_report,
)
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)
from app.data_quality.repository import PersistedDataQualityV1
from app.matchup_packet.repository import PersistedMatchupPacketV1
from app.predictions.repository import PersistedPredictionsV1, PredictionsRepository
from app.rankings.repository import PersistedRankingsV1, RankingsRepository
from app.recommendation_gate.repository import PersistedRecommendationGateV1, RecommendationGateRepository
from app.value_engine.repository import PersistedValueEngineV1, ValueEngineRepository


class PdfReportRepositoryError(RuntimeError):
    pass


class PdfReportNotFoundError(PdfReportRepositoryError):
    pass


class PdfReportPersistenceConflict(PdfReportRepositoryError):
    pass


class PdfReportIntegrityError(PdfReportRepositoryError):
    pass


class PdfReportAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    ASSEMBLY_FAILED = "assembly_failed"
    RENDERING_FAILED = "rendering_failed"
    VERIFICATION_FAILED = "verification_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True, slots=True)
class PdfReportUpstreamV1:
    rankings: PersistedRankingsV1
    gate: PersistedRecommendationGateV1
    value: PersistedValueEngineV1
    predictions: PersistedPredictionsV1
    matchup_packet: PersistedMatchupPacketV1
    data_quality: PersistedDataQualityV1


@dataclass(frozen=True, slots=True)
class PersistedPdfReportV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    document: ProductionPdfReportV1
    artifacts: ProductionPdfReportArtifactsV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class PdfReportAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: PdfReportAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise PdfReportIntegrityError(f"persisted {name} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise PdfReportIntegrityError(f"persisted {name} is invalid") from exc


def _policy(value: object) -> PdfReportPolicyV1:
    payload = json.loads(value) if isinstance(value, str) else value
    if not isinstance(payload, dict):
        raise PdfReportIntegrityError("persisted PDF policy is invalid")
    identity = {key: item for key, item in payload.items() if key != "checksum"}
    policy = PdfReportPolicyV1(**identity)
    if payload.get("checksum") != policy.checksum:
        raise PdfReportIntegrityError("persisted PDF policy checksum mismatch")
    return policy


def _outcome(payload: Mapping[str, object]) -> PdfReportOutcomeV1:
    return PdfReportOutcomeV1(
        side=str(payload["side"]),
        outcome_team_id=str(payload["outcome_team_id"]),
        prediction_probability=float(cast(int | float | str, payload["prediction_probability"])),
        probability_lower=float(cast(int | float | str, payload["probability_lower"])),
        probability_upper=float(cast(int | float | str, payload["probability_upper"])),
        availability=str(payload["availability"]),
        bookmaker_count=int(cast(int | str, payload["bookmaker_count"])),
        consensus_no_vig_probability=None
        if payload["consensus_no_vig_probability"] is None
        else float(cast(int | float | str, payload["consensus_no_vig_probability"])),
        best_price=None if payload["best_price"] is None else float(cast(int | float | str, payload["best_price"])),
        best_price_bookmakers=tuple(str(item) for item in cast(list[object], payload["best_price_bookmakers"])),
        edge=None if payload["edge"] is None else float(cast(int | float | str, payload["edge"])),
        expected_value_per_unit=None
        if payload["expected_value_per_unit"] is None
        else float(cast(int | float | str, payload["expected_value_per_unit"])),
        lower_bound_clearance=None
        if payload["lower_bound_clearance"] is None
        else float(cast(int | float | str, payload["lower_bound_clearance"])),
        freshness_state=str(payload["freshness_state"]),
        value_checksum=str(payload["value_checksum"]),
        prediction_checksum=str(payload["prediction_checksum"]),
        market_context_checksum=str(payload["market_context_checksum"]),
    )


def _game(payload: Mapping[str, object]) -> PdfReportGameV1:
    outcomes = payload["outcomes"]
    if not isinstance(outcomes, list) or len(outcomes) != 2 or not all(isinstance(item, dict) for item in outcomes):
        raise PdfReportIntegrityError("persisted PDF outcome inventory is invalid")
    context = payload["context"]
    if not isinstance(context, dict):
        raise PdfReportIntegrityError("persisted PDF game context is invalid")
    scheduled = payload["scheduled_start_time"]
    return PdfReportGameV1(
        ordinal=int(cast(int | str, payload["ordinal"])),
        source_game_id=str(payload["source_game_id"]),
        away_team_id=str(payload["away_team_id"]),
        home_team_id=str(payload["home_team_id"]),
        scheduled_start_time=None if scheduled is None else _time(scheduled, "scheduled_start_time"),
        decision=str(payload["decision"]),
        selected_side=None if payload["selected_side"] is None else str(payload["selected_side"]),
        selected_team_id=None if payload["selected_team_id"] is None else str(payload["selected_team_id"]),
        recommendation_rank=None
        if payload["recommendation_rank"] is None
        else int(cast(int | str, payload["recommendation_rank"])),
        quality_disposition=str(payload["quality_disposition"]),
        quality_issue_codes=tuple(str(item) for item in cast(list[object], payload["quality_issue_codes"])),
        provider_kind=str(payload["provider_kind"]),
        provider_contract=str(payload["provider_contract"]),
        provider_version=str(payload["provider_version"]),
        calibration_state=str(payload["calibration_state"]),
        market_independence_attested=payload["market_independence_attested"] is True,
        outcomes=(_outcome(outcomes[0]), _outcome(outcomes[1])),
        context=context,
        upstream_ranking_entry_checksum=str(payload["upstream_ranking_entry_checksum"]),
        upstream_gate_game_checksum=str(payload["upstream_gate_game_checksum"]),
        upstream_value_game_checksum=str(payload["upstream_value_game_checksum"]),
        upstream_prediction_game_checksum=str(payload["upstream_prediction_game_checksum"]),
        upstream_matchup_packet_game_checksum=str(payload["upstream_matchup_packet_game_checksum"]),
        upstream_data_quality_game_checksum=str(payload["upstream_data_quality_game_checksum"]),
    )


class PdfReportRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: PdfReportPolicyV1 = PdfReportPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.policy = policy
        self.rankings = RankingsRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
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

    def resolve_upstream(self, run_id: str) -> PdfReportUpstreamV1:
        rankings = self.rankings.get_latest_for_run(validate_run_id(run_id))
        if rankings is None:
            raise PdfReportIntegrityError("PDF Report requires sealed Rankings")
        return self.resolve_upstream_by_snapshot_ids(
            rankings_snapshot_id=rankings.snapshot_id,
            recommendation_gate_snapshot_id=rankings.rankings.upstream_gate_snapshot_id,
        )

    def resolve_upstream_by_snapshot_ids(
        self,
        *,
        rankings_snapshot_id: str,
        recommendation_gate_snapshot_id: str,
        value_engine_snapshot_id: str | None = None,
        predictions_snapshot_id: str | None = None,
        matchup_packet_snapshot_id: str | None = None,
        data_quality_snapshot_id: str | None = None,
    ) -> PdfReportUpstreamV1:
        rankings = self.rankings.get_by_snapshot_id(rankings_snapshot_id)
        if rankings.rankings.upstream_gate_snapshot_id != recommendation_gate_snapshot_id:
            raise PdfReportIntegrityError("stored Rankings/Gate snapshot identity mismatch")
        gate = self.gate.get_by_snapshot_id(recommendation_gate_snapshot_id)
        expected_value = gate.gate.upstream_value_snapshot_id
        expected_predictions = gate.gate.upstream_predictions_snapshot_id
        expected_quality = gate.gate.upstream_data_quality_snapshot_id
        if value_engine_snapshot_id is not None and value_engine_snapshot_id != expected_value:
            raise PdfReportIntegrityError("stored Value snapshot identity mismatch")
        if predictions_snapshot_id is not None and predictions_snapshot_id != expected_predictions:
            raise PdfReportIntegrityError("stored Predictions snapshot identity mismatch")
        if data_quality_snapshot_id is not None and data_quality_snapshot_id != expected_quality:
            raise PdfReportIntegrityError("stored Data Quality snapshot identity mismatch")
        value = self.value.get_by_snapshot_id(expected_value)
        predictions = self.predictions.get_by_snapshot_id(expected_predictions)
        upstream = self.predictions.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=predictions.predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=expected_quality,
        )
        if matchup_packet_snapshot_id is not None and upstream.matchup_packet.snapshot_id != matchup_packet_snapshot_id:
            raise PdfReportIntegrityError("stored Matchup Packet snapshot identity mismatch")
        return PdfReportUpstreamV1(rankings, gate, value, predictions, upstream.matchup_packet, upstream.data_quality)

    def assemble(self, upstream: PdfReportUpstreamV1, *, generated_at: datetime) -> ProductionPdfReportV1:
        return assemble_production_pdf_report(
            run_id=upstream.rankings.run_id,
            rankings_snapshot_id=upstream.rankings.snapshot_id,
            rankings=upstream.rankings.rankings,
            gate_snapshot_id=upstream.gate.snapshot_id,
            gate=upstream.gate.gate,
            value_snapshot_id=upstream.value.snapshot_id,
            value=upstream.value.value_engine,
            predictions_snapshot_id=upstream.predictions.snapshot_id,
            predictions=upstream.predictions.predictions,
            matchup_packet_snapshot_id=upstream.matchup_packet.snapshot_id,
            matchup_packet=upstream.matchup_packet.packet,
            data_quality_snapshot_id=upstream.data_quality.snapshot_id,
            data_quality=upstream.data_quality.snapshot,
            generated_at=generated_at,
            policy=self.policy,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(connection: sqlite3.Connection, run_id: str, attempt: int) -> None:
        row = connection.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='pdf_report'",
            (run_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "running" or int(row["attempt_count"]) != attempt:
            raise PdfReportIntegrityError("PDF Report attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        upstream: PdfReportUpstreamV1,
        generated_at: datetime,
        input_checksum: str,
        outcome: PdfReportAttemptOutcome,
        snapshot: ProductionPdfReportV1 | None,
        warnings: tuple[Mapping[str, object], ...],
    ) -> PreModelAttemptManifestV1:
        identities = (
            PreModelUpstreamIdentityV1("rankings", upstream.rankings.snapshot_id, upstream.rankings.rankings.checksum),
            PreModelUpstreamIdentityV1("recommendation_gate", upstream.gate.snapshot_id, upstream.gate.gate.checksum),
            PreModelUpstreamIdentityV1(
                "value_engine", upstream.value.snapshot_id, upstream.value.value_engine.checksum
            ),
            PreModelUpstreamIdentityV1(
                "predictions", upstream.predictions.snapshot_id, upstream.predictions.predictions.checksum
            ),
            PreModelUpstreamIdentityV1(
                "matchup_packet", upstream.matchup_packet.snapshot_id, upstream.matchup_packet.packet.checksum
            ),
            PreModelUpstreamIdentityV1(
                "data_quality", upstream.data_quality.snapshot_id, upstream.data_quality.snapshot.checksum
            ),
        )
        now = self._now()
        return create_decision_manifest(
            contract_version=PDF_REPORT_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="pdf_report",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=upstream.rankings.rankings.requested_date,
            as_of_time=upstream.rankings.rankings.as_of_time,
            observed_at=generated_at,
            phase_input_checksum=input_checksum,
            upstream=identities,
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=now,
            phase_input_evidence={"policy": {**self.policy.as_dict(), "checksum": self.policy.checksum}},
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(
        connection: sqlite3.Connection, manifest: PreModelAttemptManifestV1, artifact: PreModelArtifactV1
    ) -> None:
        upstream = {item.phase_key: item for item in manifest.upstream}
        policy = manifest.phase_input_evidence["policy"]
        if not isinstance(policy, Mapping):
            raise PdfReportIntegrityError("PDF policy evidence is invalid")
        connection.execute(
            """INSERT INTO pdf_report_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,generated_at,phase_input_checksum,upstream_rankings_snapshot_id,upstream_rankings_checksum,upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum,upstream_value_engine_snapshot_id,upstream_value_engine_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,policy_json,policy_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                manifest.run_id,
                manifest.phase_attempt,
                manifest.requested_date,
                manifest.as_of_time.isoformat(),
                manifest.observed_at.isoformat(),
                manifest.phase_input_checksum,
                upstream["rankings"].snapshot_id,
                upstream["rankings"].checksum,
                upstream["recommendation_gate"].snapshot_id,
                upstream["recommendation_gate"].checksum,
                upstream["value_engine"].snapshot_id,
                upstream["value_engine"].checksum,
                upstream["predictions"].snapshot_id,
                upstream["predictions"].checksum,
                upstream["matchup_packet"].snapshot_id,
                upstream["matchup_packet"].checksum,
                upstream["data_quality"].snapshot_id,
                upstream["data_quality"].checksum,
                canonical_text(policy),
                str(policy["checksum"]),
                manifest.outcome,
                manifest.snapshot_checksum,
                artifact.relpath,
                artifact.checksum,
                artifact.byte_count,
                canonical_text(list(manifest.warnings)),
                len(manifest.warnings),
                manifest.created_at.isoformat(),
                manifest.completed_at.isoformat(),
            ),
        )

    def persist_assembly(
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, snapshot: ProductionPdfReportV1
    ) -> PersistedPdfReportV1:
        try:
            existing = self.get_for_run_attempt(run_id, phase_attempt)
        except PdfReportNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.document.canonical_json_bytes() == snapshot.canonical_json_bytes()
            ):
                return existing
            raise PdfReportPersistenceConflict("conflicting PDF Report replay")
        upstream = self.resolve_upstream_by_snapshot_ids(
            rankings_snapshot_id=snapshot.upstream_snapshot_ids["rankings"],
            recommendation_gate_snapshot_id=snapshot.upstream_snapshot_ids["recommendation_gate"],
            value_engine_snapshot_id=snapshot.upstream_snapshot_ids["value_engine"],
            predictions_snapshot_id=snapshot.upstream_snapshot_ids["predictions"],
            matchup_packet_snapshot_id=snapshot.upstream_snapshot_ids["matchup_packet"],
            data_quality_snapshot_id=snapshot.upstream_snapshot_ids["data_quality"],
        )
        replay = self.assemble(upstream, generated_at=snapshot.generated_at)
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise PdfReportIntegrityError("PDF Report is not reproducible from exact upstream")
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            upstream=upstream,
            generated_at=snapshot.generated_at,
            input_checksum=phase_input_checksum,
            outcome=PdfReportAttemptOutcome.ASSEMBLED,
            snapshot=snapshot,
            warnings=snapshot.warnings,
        )
        manifest_artifact: PreModelArtifactV1 | None = None
        artifacts: ProductionPdfReportArtifactsV1 | None = None
        now = self._now()
        try:
            with self.database.connect() as connection:
                self._active(connection, run_id, phase_attempt)
            manifest_artifact = publish_decision_manifest(
                manifest, self.artifact_root, "pdf_report", secret_values=self.secret_values
            )
            artifacts = publish_production_pdf_report_artifacts(snapshot, self.artifact_root)
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt)
                self._insert_attempt(connection, manifest, manifest_artifact)
                snapshot_id = f"pdf-report:{snapshot.checksum}"
                ids, checksums = snapshot.upstream_snapshot_ids, snapshot.upstream_checksums
                connection.execute(
                    """INSERT INTO pdf_report_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,generated_at,contract_version,render_version,phase_input_checksum,policy_json,policy_checksum,upstream_rankings_snapshot_id,upstream_rankings_checksum,upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum,upstream_value_engine_snapshot_id,upstream_value_engine_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,semantic_checksum,game_count,recommendation_count,warning_count,canonical_json,document_relpath,document_checksum,document_byte_count,pdf_relpath,pdf_checksum,pdf_byte_count,pdf_page_count,render_manifest_relpath,render_manifest_checksum,render_manifest_byte_count,report_status,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        snapshot_id,
                        run_id,
                        phase_attempt,
                        snapshot.requested_date,
                        snapshot.as_of_time.isoformat(),
                        snapshot.generated_at.isoformat(),
                        snapshot.contract_version,
                        snapshot.render_version,
                        phase_input_checksum,
                        canonical_text({**snapshot.policy.as_dict(), "checksum": snapshot.policy.checksum}),
                        snapshot.policy.checksum,
                        ids["rankings"],
                        checksums["rankings"],
                        ids["recommendation_gate"],
                        checksums["recommendation_gate"],
                        ids["value_engine"],
                        checksums["value_engine"],
                        ids["predictions"],
                        checksums["predictions"],
                        ids["matchup_packet"],
                        checksums["matchup_packet"],
                        ids["data_quality"],
                        checksums["data_quality"],
                        snapshot.checksum,
                        len(snapshot.games),
                        len(snapshot.recommendation_game_ids),
                        len(snapshot.warnings),
                        snapshot.canonical_json_bytes().decode(),
                        artifacts.document.relpath,
                        artifacts.document.checksum,
                        artifacts.document.byte_count,
                        artifacts.pdf.relpath,
                        artifacts.pdf.checksum,
                        artifacts.pdf.byte_count,
                        artifacts.page_count,
                        artifacts.manifest.relpath,
                        artifacts.manifest.checksum,
                        artifacts.manifest.byte_count,
                        snapshot.report_status,
                        now.isoformat(),
                    ),
                )
                for game in snapshot.games:
                    connection.execute(
                        "INSERT INTO pdf_report_games VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            snapshot_id,
                            run_id,
                            phase_attempt,
                            game.ordinal,
                            game.source_game_id,
                            game.decision,
                            game.recommendation_rank,
                            game.selected_side,
                            game.selected_team_id,
                            game.checksum,
                            canonical_text(game.as_dict()),
                        ),
                    )
                connection.execute(
                    "UPDATE pdf_report_snapshots SET sealed_at=? WHERE snapshot_id=?",
                    (self._now().isoformat(), snapshot_id),
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(artifacts, manifest_artifact)
            raise PdfReportPersistenceConflict("conflicting PDF Report evidence") from exc
        except Exception:
            self._cleanup(artifacts, manifest_artifact)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(self, artifacts: ProductionPdfReportArtifactsV1 | None, manifest: PreModelArtifactV1 | None) -> None:
        if artifacts is not None:
            for artifact in (artifacts.manifest, artifacts.pdf, artifacts.document):
                cleanup_owned_artifact(self.artifact_root, artifact)
        if manifest is not None:
            cleanup_owned_artifact(self.artifact_root, manifest)

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        generated_at: datetime,
        outcome: PdfReportAttemptOutcome,
        upstream: PdfReportUpstreamV1,
        warnings: tuple[Mapping[str, object], ...],
    ) -> PdfReportAttemptEvidenceV1:
        if outcome is PdfReportAttemptOutcome.ASSEMBLED:
            raise ValueError("failed PDF Report attempt cannot be assembled")
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except PdfReportNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.outcome is outcome
                and existing.phase_input_checksum == phase_input_checksum
                and existing.warnings == warnings
            ):
                return existing
            raise PdfReportPersistenceConflict("conflicting failed PDF Report attempt")
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            upstream=upstream,
            generated_at=generated_at,
            input_checksum=phase_input_checksum,
            outcome=outcome,
            snapshot=None,
            warnings=warnings,
        )
        artifact = publish_decision_manifest(
            manifest, self.artifact_root, "pdf_report", secret_values=self.secret_values
        )
        try:
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt)
                self._insert_attempt(connection, manifest, artifact)
        except Exception:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _attempt_row(self, run_id: str, attempt: int) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pdf_report_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if row is None:
            raise PdfReportNotFoundError("PDF Report attempt not found")
        return row

    def _manifest_from_row(self, row: sqlite3.Row) -> PreModelAttemptManifestV1:
        upstream = tuple(
            PreModelUpstreamIdentityV1(
                phase, str(row[f"upstream_{column}_snapshot_id"]), str(row[f"upstream_{column}_checksum"])
            )
            for phase, column in (
                ("rankings", "rankings"),
                ("recommendation_gate", "recommendation_gate"),
                ("value_engine", "value_engine"),
                ("predictions", "predictions"),
                ("matchup_packet", "matchup_packet"),
                ("data_quality", "data_quality"),
            )
        )
        return create_decision_manifest(
            contract_version=PDF_REPORT_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="pdf_report",
            run_id=str(row["run_id"]),
            phase_attempt=int(row["phase_attempt"]),
            requested_date=str(row["requested_date"]),
            as_of_time=_time(row["as_of_time"], "as_of_time"),
            observed_at=_time(row["generated_at"], "generated_at"),
            phase_input_checksum=str(row["phase_input_checksum"]),
            upstream=upstream,
            outcome=str(row["outcome"]),
            snapshot_checksum=None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            warnings=tuple(dict(item) for item in json.loads(str(row["warnings_json"]))),
            created_at=_time(row["created_at"], "created_at"),
            phase_input_evidence={
                "policy": {**_policy(row["policy_json"]).as_dict(), "checksum": str(row["policy_checksum"])}
            },
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> PreModelAttemptManifestV1:
        row = self._attempt_row(run_id, attempt)
        manifest = self._manifest_from_row(row)
        verify_decision_manifest(
            manifest,
            PreModelArtifactV1(
                str(row["evidence_manifest_relpath"]),
                str(row["evidence_manifest_checksum"]),
                int(row["evidence_manifest_byte_count"]),
            ),
            self.artifact_root,
            secret_values=self.secret_values,
        )
        return manifest

    def get_attempt_evidence(self, run_id: str, attempt: int) -> PdfReportAttemptEvidenceV1:
        row = self._attempt_row(run_id, attempt)
        manifest = self.get_attempt_manifest(run_id, attempt)
        return PdfReportAttemptEvidenceV1(
            manifest.run_id,
            manifest.phase_attempt,
            PdfReportAttemptOutcome(manifest.outcome),
            manifest.snapshot_checksum,
            manifest.phase_input_checksum,
            manifest.warnings,
            PreModelArtifactV1(
                str(row["evidence_manifest_relpath"]),
                str(row["evidence_manifest_checksum"]),
                int(row["evidence_manifest_byte_count"]),
            ),
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[PdfReportAttemptEvidenceV1, ...]:
        with self.database.connect() as connection:
            attempts = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT phase_attempt FROM pdf_report_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, attempt) for attempt in attempts)

    def _snapshot_row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT * FROM pdf_report_snapshots WHERE {where}", values).fetchone()
        if row is None or row["sealed_at"] is None:
            raise PdfReportNotFoundError("sealed PDF Report snapshot not found")
        return row

    def _verify(
        self,
        row: sqlite3.Row,
        *,
        verify_artifacts: bool = True,
    ) -> PersistedPdfReportV1:
        payload = json.loads(str(row["canonical_json"]))
        with self.database.connect() as connection:
            children = connection.execute(
                "SELECT canonical_json,game_checksum FROM pdf_report_games WHERE snapshot_id=? ORDER BY ordinal",
                (str(row["snapshot_id"]),),
            ).fetchall()
        games = tuple(_game(json.loads(str(child["canonical_json"]))) for child in children)
        if any(game.checksum != str(child["game_checksum"]) for game, child in zip(games, children, strict=True)):
            raise PdfReportIntegrityError("PDF Report relational game checksum mismatch")
        ids = payload["upstream_snapshot_ids"]
        checksums = payload["upstream_checksums"]
        if not isinstance(ids, dict) or not isinstance(checksums, dict):
            raise PdfReportIntegrityError("PDF Report lineage JSON is invalid")
        upstream_order = (
            "rankings",
            "recommendation_gate",
            "value_engine",
            "predictions",
            "matchup_packet",
            "data_quality",
        )
        if set(ids) != set(upstream_order) or set(checksums) != set(upstream_order):
            raise PdfReportIntegrityError("PDF Report lineage inventory is invalid")
        document = ProductionPdfReportV1(
            run_id=str(payload["run_id"]),
            requested_date=str(payload["requested_date"]),
            as_of_time=_time(payload["as_of_time"], "as_of_time"),
            generated_at=_time(payload["generated_at"], "generated_at"),
            policy=_policy(payload["policy"]),
            upstream_snapshot_ids={key: str(ids[key]) for key in upstream_order},
            upstream_checksums={key: str(checksums[key]) for key in upstream_order},
            games=games,
            warnings=tuple(dict(item) for item in payload["warnings"]),
            report_status=str(payload["report_status"]),
            contract_version=str(payload["contract_version"]),
            render_version=str(payload["render_version"]),
            secret_values=self.secret_values,
        )
        if document.checksum != str(row["semantic_checksum"]) or document.canonical_json_bytes().decode() != str(
            row["canonical_json"]
        ):
            raise PdfReportIntegrityError("PDF Report canonical reconstruction mismatch")
        upstream = self.resolve_upstream_by_snapshot_ids(
            rankings_snapshot_id=str(row["upstream_rankings_snapshot_id"]),
            recommendation_gate_snapshot_id=str(row["upstream_recommendation_gate_snapshot_id"]),
            value_engine_snapshot_id=str(row["upstream_value_engine_snapshot_id"]),
            predictions_snapshot_id=str(row["upstream_predictions_snapshot_id"]),
            matchup_packet_snapshot_id=str(row["upstream_matchup_packet_snapshot_id"]),
            data_quality_snapshot_id=str(row["upstream_data_quality_snapshot_id"]),
        )
        replay = self.assemble(upstream, generated_at=document.generated_at)
        if replay.canonical_json_bytes() != document.canonical_json_bytes():
            raise PdfReportIntegrityError("historical PDF Report replay mismatch")
        artifacts = ProductionPdfReportArtifactsV1(
            PreModelArtifactV1(
                str(row["document_relpath"]), str(row["document_checksum"]), int(row["document_byte_count"])
            ),
            PreModelArtifactV1(str(row["pdf_relpath"]), str(row["pdf_checksum"]), int(row["pdf_byte_count"])),
            PreModelArtifactV1(
                str(row["render_manifest_relpath"]),
                str(row["render_manifest_checksum"]),
                int(row["render_manifest_byte_count"]),
            ),
            int(row["pdf_page_count"]),
        )
        if verify_artifacts:
            verify_production_pdf_report_artifacts(document, artifacts, self.artifact_root)
        self.get_attempt_evidence(str(row["run_id"]), int(row["phase_attempt"]))
        return PersistedPdfReportV1(
            str(row["snapshot_id"]),
            str(row["run_id"]),
            int(row["phase_attempt"]),
            document,
            artifacts,
            str(row["phase_input_checksum"]),
            _time(row["created_at"], "created_at"),
            _time(row["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedPdfReportV1:
        return self._verify(self._snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_by_snapshot_id_for_final_qc(self, snapshot_id: str) -> PersistedPdfReportV1:
        """Load exact semantic evidence while leaving physical checks to Final QC."""

        return self._verify(
            self._snapshot_row("snapshot_id=?", (snapshot_id,)),
            verify_artifacts=False,
        )

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedPdfReportV1:
        return self._verify(self._snapshot_row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedPdfReportV1 | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM pdf_report_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if row is None else self._verify(row)
