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
from app.infographic.artifact import InfographicArtifactsV1, publish_infographic_artifacts, verify_infographic_artifacts
from app.infographic.assembly import assemble_infographic_document
from app.infographic.contracts import (
    INFOGRAPHIC_ATTEMPT_MANIFEST_CONTRACT,
    InfographicDocumentV1,
    InfographicSelectionV1,
    InfographicVariantType,
    InfographicVariantV1,
)
from app.infographic.policy import InfographicPolicyV1
from app.pdf_report.repository import PdfReportRepository, PersistedPdfReportV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)


class InfographicRepositoryError(RuntimeError):
    pass


class InfographicNotFoundError(InfographicRepositoryError):
    pass


class InfographicPersistenceConflict(InfographicRepositoryError):
    pass


class InfographicIntegrityError(InfographicRepositoryError):
    pass


class InfographicAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    ASSEMBLY_FAILED = "assembly_failed"
    RENDERING_FAILED = "rendering_failed"
    VERIFICATION_FAILED = "verification_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True, slots=True)
class PersistedInfographicV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    document: InfographicDocumentV1
    artifacts: InfographicArtifactsV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class InfographicAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: InfographicAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, name: str) -> datetime:
    try:
        return aware_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise InfographicIntegrityError(f"persisted {name} is invalid") from exc


def _policy(value: object) -> InfographicPolicyV1:
    p = json.loads(value) if isinstance(value, str) else value
    if not isinstance(p, dict):
        raise InfographicIntegrityError("persisted Infographic policy is invalid")
    result = InfographicPolicyV1(**{k: v for k, v in p.items() if k != "checksum"})
    if p.get("checksum") != result.checksum:
        raise InfographicIntegrityError("Infographic policy checksum mismatch")
    return result


def _selection(p: Mapping[str, object]) -> InfographicSelectionV1:
    return InfographicSelectionV1(
        int(cast(int | str, p["recommendation_rank"])),
        str(p["source_game_id"]),
        str(p["matchup_label"]),
        str(p["selected_team_id"]),
        str(p["selected_side"]),
        float(cast(int | float | str, p["prediction_probability"])),
        None if p["market_probability"] is None else float(cast(int | float | str, p["market_probability"])),
        None if p["edge"] is None else float(cast(int | float | str, p["edge"])),
        None if p["expected_value_per_unit"] is None else float(cast(int | float | str, p["expected_value_per_unit"])),
        None if p["best_price"] is None else float(cast(int | float | str, p["best_price"])),
        int(cast(int | str, p["bookmaker_count"])),
        str(p["quality_disposition"]),
        str(p["upstream_pdf_game_checksum"]),
        str(p["upstream_ranking_entry_checksum"]),
        str(p["upstream_gate_game_checksum"]),
        str(p["upstream_value_checksum"]),
    )


def _variant(p: Mapping[str, object]) -> InfographicVariantV1:
    pick = p["pick_of_day"]
    recs = p["recommendations"]
    if pick is not None and not isinstance(pick, dict) or not isinstance(recs, list):
        raise InfographicIntegrityError("persisted variant cards are invalid")
    return InfographicVariantV1(
        InfographicVariantType(str(p["variant"])),
        int(cast(int | str, p["width"])),
        int(cast(int | str, p["height"])),
        None if pick is None else _selection(pick),
        tuple(_selection(item) for item in recs if isinstance(item, dict)),
        str(p["weather_headline"]),
        str(p["weather_detail"]),
        str(p["report_cta"]),
    )


class InfographicRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        policy: InfographicPolicyV1 = InfographicPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(v) for v in secret_values if str(v))
        self.clock = clock
        self.policy = policy
        self.pdf = PdfReportRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> PersistedPdfReportV1:
        value = self.pdf.get_latest_for_run(validate_run_id(run_id))
        if value is None:
            raise InfographicIntegrityError("Infographic requires sealed PDF Report")
        return value

    def assemble(self, pdf: PersistedPdfReportV1, *, generated_at: datetime) -> InfographicDocumentV1:
        return assemble_infographic_document(
            pdf.document,
            upstream_pdf_report_snapshot_id=pdf.snapshot_id,
            generated_at=generated_at,
            policy=self.policy,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(c: sqlite3.Connection, run_id: str, attempt: int) -> None:
        r = c.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='infographic'", (run_id,)
        ).fetchone()
        if r is None or str(r["status"]) != "running" or int(r["attempt_count"]) != attempt:
            raise InfographicIntegrityError("Infographic attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        pdf: PersistedPdfReportV1,
        generated_at: datetime,
        input_checksum: str,
        outcome: InfographicAttemptOutcome,
        snapshot: InfographicDocumentV1 | None,
        warnings: tuple[Mapping[str, object], ...],
    ) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=INFOGRAPHIC_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="infographic",
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=pdf.document.requested_date,
            as_of_time=pdf.document.as_of_time,
            observed_at=generated_at,
            phase_input_checksum=input_checksum,
            upstream=(PreModelUpstreamIdentityV1("pdf_report", pdf.snapshot_id, pdf.document.checksum),),
            outcome=outcome.value,
            snapshot_checksum=None if snapshot is None else snapshot.checksum,
            warnings=warnings,
            created_at=self._now(),
            phase_input_evidence={"policy": {**self.policy.as_dict(), "checksum": self.policy.checksum}},
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(c: sqlite3.Connection, m: PreModelAttemptManifestV1, a: PreModelArtifactV1) -> None:
        p = m.phase_input_evidence["policy"]
        u = m.upstream[0]
        if not isinstance(p, Mapping):
            raise InfographicIntegrityError("Infographic policy evidence invalid")
        c.execute(
            """INSERT INTO infographic_attempt_evidence(run_id,phase_attempt,requested_date,as_of_time,generated_at,phase_input_checksum,upstream_pdf_report_snapshot_id,upstream_pdf_report_checksum,policy_json,policy_checksum,outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                m.run_id,
                m.phase_attempt,
                m.requested_date,
                m.as_of_time.isoformat(),
                m.observed_at.isoformat(),
                m.phase_input_checksum,
                u.snapshot_id,
                u.checksum,
                canonical_text(p),
                p["checksum"],
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
        self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, snapshot: InfographicDocumentV1
    ) -> PersistedInfographicV1:
        try:
            existing = self.get_for_run_attempt(run_id, phase_attempt)
        except InfographicNotFoundError:
            existing = None
        if existing:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.document.canonical_json_bytes() == snapshot.canonical_json_bytes()
            ):
                return existing
            raise InfographicPersistenceConflict("conflicting Infographic replay")
        pdf = self.pdf.get_by_snapshot_id(snapshot.upstream_pdf_report_snapshot_id)
        replay = self.assemble(pdf, generated_at=snapshot.generated_at)
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise InfographicIntegrityError("Infographic is not reproducible")
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            pdf=pdf,
            generated_at=snapshot.generated_at,
            input_checksum=phase_input_checksum,
            outcome=InfographicAttemptOutcome.ASSEMBLED,
            snapshot=snapshot,
            warnings=snapshot.warnings,
        )
        ma = None
        arts = None
        now = self._now()
        try:
            with self.database.connect() as c:
                self._active(c, run_id, phase_attempt)
            ma = publish_decision_manifest(m, self.artifact_root, "infographic", secret_values=self.secret_values)
            arts = publish_infographic_artifacts(snapshot, self.artifact_root)
            with self.database.connect(write=True) as c:
                self._active(c, run_id, phase_attempt)
                self._insert_attempt(c, m, ma)
                sid = f"infographic:{snapshot.checksum}"
                c.execute(
                    """INSERT INTO infographic_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,generated_at,contract_version,render_version,phase_input_checksum,policy_json,policy_checksum,upstream_pdf_report_snapshot_id,upstream_pdf_report_checksum,infographic_checksum,recommendation_count,warning_count,canonical_json,document_relpath,document_checksum,document_byte_count,feed_relpath,feed_checksum,feed_byte_count,story_relpath,story_checksum,story_byte_count,render_manifest_relpath,render_manifest_checksum,render_manifest_byte_count,report_status,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                    (
                        sid,
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
                        pdf.snapshot_id,
                        pdf.document.checksum,
                        snapshot.checksum,
                        snapshot.full_report_recommendation_count,
                        len(snapshot.warnings),
                        snapshot.canonical_json_bytes().decode(),
                        arts.document.relpath,
                        arts.document.checksum,
                        arts.document.byte_count,
                        arts.feed.relpath,
                        arts.feed.checksum,
                        arts.feed.byte_count,
                        arts.story.relpath,
                        arts.story.checksum,
                        arts.story.byte_count,
                        arts.manifest.relpath,
                        arts.manifest.checksum,
                        arts.manifest.byte_count,
                        snapshot.report_status,
                        now.isoformat(),
                    ),
                )
                for ordinal, v in enumerate(snapshot.variants, 1):
                    c.execute(
                        "INSERT INTO infographic_variants VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            sid,
                            run_id,
                            phase_attempt,
                            ordinal,
                            v.variant.value,
                            v.width,
                            v.height,
                            v.checksum,
                            canonical_text(v.as_dict()),
                        ),
                    )
                c.execute(
                    "UPDATE infographic_snapshots SET sealed_at=? WHERE snapshot_id=?", (self._now().isoformat(), sid)
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(arts, ma)
            raise InfographicPersistenceConflict("conflicting Infographic evidence") from exc
        except Exception:
            self._cleanup(arts, ma)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(self, arts: InfographicArtifactsV1 | None, manifest: PreModelArtifactV1 | None) -> None:
        if arts:
            for a in (arts.manifest, arts.story, arts.feed, arts.document):
                cleanup_owned_artifact(self.artifact_root, a)
        if manifest:
            cleanup_owned_artifact(self.artifact_root, manifest)

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        generated_at: datetime,
        outcome: InfographicAttemptOutcome,
        pdf: PersistedPdfReportV1,
        warnings: tuple[Mapping[str, object], ...],
    ) -> InfographicAttemptEvidenceV1:
        if outcome is InfographicAttemptOutcome.ASSEMBLED:
            raise ValueError("failed Infographic attempt cannot be assembled")
        try:
            e = self.get_attempt_evidence(run_id, phase_attempt)
        except InfographicNotFoundError:
            e = None
        if e:
            if e.outcome is outcome and e.phase_input_checksum == phase_input_checksum and e.warnings == warnings:
                return e
            raise InfographicPersistenceConflict("conflicting failed Infographic attempt")
        m = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            pdf=pdf,
            generated_at=generated_at,
            input_checksum=phase_input_checksum,
            outcome=outcome,
            snapshot=None,
            warnings=warnings,
        )
        a = publish_decision_manifest(m, self.artifact_root, "infographic", secret_values=self.secret_values)
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
                "SELECT * FROM infographic_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if r is None:
            raise InfographicNotFoundError("Infographic attempt not found")
        return r

    def _manifest_from_row(self, r: sqlite3.Row) -> PreModelAttemptManifestV1:
        return create_decision_manifest(
            contract_version=INFOGRAPHIC_ATTEMPT_MANIFEST_CONTRACT,
            phase_key="infographic",
            run_id=str(r["run_id"]),
            phase_attempt=int(r["phase_attempt"]),
            requested_date=str(r["requested_date"]),
            as_of_time=_time(r["as_of_time"], "as_of_time"),
            observed_at=_time(r["generated_at"], "generated_at"),
            phase_input_checksum=str(r["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "pdf_report", str(r["upstream_pdf_report_snapshot_id"]), str(r["upstream_pdf_report_checksum"])
                ),
            ),
            outcome=str(r["outcome"]),
            snapshot_checksum=None if r["snapshot_checksum"] is None else str(r["snapshot_checksum"]),
            warnings=tuple(dict(x) for x in json.loads(str(r["warnings_json"]))),
            created_at=_time(r["created_at"], "created_at"),
            phase_input_evidence={
                "policy": {**_policy(r["policy_json"]).as_dict(), "checksum": str(r["policy_checksum"])}
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

    def get_attempt_evidence(self, run_id: str, attempt: int) -> InfographicAttemptEvidenceV1:
        r = self._attempt_row(run_id, attempt)
        m = self.get_attempt_manifest(run_id, attempt)
        return InfographicAttemptEvidenceV1(
            m.run_id,
            m.phase_attempt,
            InfographicAttemptOutcome(m.outcome),
            m.snapshot_checksum,
            m.phase_input_checksum,
            m.warnings,
            PreModelArtifactV1(
                str(r["evidence_manifest_relpath"]),
                str(r["evidence_manifest_checksum"]),
                int(r["evidence_manifest_byte_count"]),
            ),
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[InfographicAttemptEvidenceV1, ...]:
        with self.database.connect() as c:
            a = tuple(
                int(r[0])
                for r in c.execute(
                    "SELECT phase_attempt FROM infographic_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, n) for n in a)

    def _row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as c:
            r = c.execute(f"SELECT * FROM infographic_snapshots WHERE {where}", values).fetchone()
        if r is None or r["sealed_at"] is None:
            raise InfographicNotFoundError("sealed Infographic snapshot not found")
        return r

    def _verify(self, r: sqlite3.Row) -> PersistedInfographicV1:
        p = json.loads(str(r["canonical_json"]))
        variants = p["variants"]
        if not isinstance(variants, list) or len(variants) != 2:
            raise InfographicIntegrityError("Infographic variants invalid")
        doc = InfographicDocumentV1(
            str(p["run_id"]),
            str(p["requested_date"]),
            _time(p["as_of_time"], "as_of_time"),
            _time(p["generated_at"], "generated_at"),
            str(p["upstream_pdf_report_snapshot_id"]),
            str(p["upstream_pdf_report_checksum"]),
            _policy(p["policy"]),
            (_variant(variants[0]), _variant(variants[1])),
            int(p["full_report_game_count"]),
            int(p["full_report_recommendation_count"]),
            tuple(dict(x) for x in p["warnings"]),
            str(p["report_status"]),
            str(p["contract_version"]),
            str(p["render_version"]),
            secret_values=self.secret_values,
        )
        if doc.checksum != str(r["infographic_checksum"]) or doc.canonical_json_bytes().decode() != str(
            r["canonical_json"]
        ):
            raise InfographicIntegrityError("Infographic canonical reconstruction mismatch")
        pdf = self.pdf.get_by_snapshot_id(str(r["upstream_pdf_report_snapshot_id"]))
        replay = self.assemble(pdf, generated_at=doc.generated_at)
        if replay.canonical_json_bytes() != doc.canonical_json_bytes():
            raise InfographicIntegrityError("historical Infographic replay mismatch")
        arts = InfographicArtifactsV1(
            PreModelArtifactV1(str(r["document_relpath"]), str(r["document_checksum"]), int(r["document_byte_count"])),
            PreModelArtifactV1(str(r["feed_relpath"]), str(r["feed_checksum"]), int(r["feed_byte_count"])),
            PreModelArtifactV1(str(r["story_relpath"]), str(r["story_checksum"]), int(r["story_byte_count"])),
            PreModelArtifactV1(
                str(r["render_manifest_relpath"]),
                str(r["render_manifest_checksum"]),
                int(r["render_manifest_byte_count"]),
            ),
        )
        verify_infographic_artifacts(doc, arts, self.artifact_root)
        self.get_attempt_evidence(str(r["run_id"]), int(r["phase_attempt"]))
        return PersistedInfographicV1(
            str(r["snapshot_id"]),
            str(r["run_id"]),
            int(r["phase_attempt"]),
            doc,
            arts,
            str(r["phase_input_checksum"]),
            _time(r["created_at"], "created_at"),
            _time(r["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedInfographicV1:
        return self._verify(self._row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedInfographicV1:
        return self._verify(self._row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedInfographicV1 | None:
        with self.database.connect() as c:
            r = c.execute(
                "SELECT * FROM infographic_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if r is None else self._verify(r)
