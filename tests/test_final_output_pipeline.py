from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.final_qc import FINAL_QC_CHECK_CODES
from app.final_qc.assessment import assess_final_qc
from app.final_qc.handler import FinalQcPhaseHandler
from app.final_qc.repository import FinalQcAttemptOutcome, FinalQcRepository, FinalQcUpstreamV1
from app.database import Database
from app.daily_slate.contracts import canonical_sha256
from app.human_review import (
    HumanReviewDecision,
    HumanReviewError,
    HumanReviewNotFoundError,
    HumanReviewRecordV1,
    HumanReviewRepository,
    HumanReviewTargetV1,
)
from app.infographic.contracts import InfographicSelectionV1
from app.infographic.repository import PersistedInfographicV1
from app.infographic.artifact import infographic_artifact_bytes, publish_infographic_artifacts, verify_infographic_artifacts
from app.infographic.assembly import assemble_infographic_document
from app.pdf_report.artifact import (
    production_pdf_artifact_bytes,
    publish_production_pdf_report_artifacts,
    verify_production_pdf_report_artifacts,
)
from app.pdf_report.policy import PdfReportPolicyV1
from app.pdf_report.production import PdfReportGameV1, PdfReportOutcomeV1, ProductionPdfReportV1
from app.pdf_report.repository import PersistedPdfReportV1
from app.pre_model_evidence import PreModelEvidenceError, canonical_text
from app.run_controller.contracts import (
    CANONICAL_PIPELINE_PHASES,
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import PipelineRunRepository
from app.run_controller.service import (
    ManualRunAwaitingHumanInput,
    ManualRunController,
    ManualRunExecutionError,
    PhaseExecutionResult,
)


SHA = "a" * 64
NOW = datetime(2026, 8, 7, 16, tzinfo=timezone.utc)


def _report_game(ordinal: int, decision: str, rank: int | None = None) -> PdfReportGameV1:
    def outcome(side: str) -> PdfReportOutcomeV1:
        return PdfReportOutcomeV1(
            side=side,
            outcome_team_id=f"team-{side}-{ordinal}",
            prediction_probability=0.55 if side == "home" else 0.45,
            probability_lower=0.48 if side == "home" else 0.38,
            probability_upper=0.62 if side == "home" else 0.52,
            availability="available",
            bookmaker_count=4,
            consensus_no_vig_probability=0.51 if side == "home" else 0.49,
            best_price=-105.0 if side == "home" else 115.0,
            best_price_bookmakers=("semantic_book",),
            edge=0.04 if side == "home" else -0.04,
            expected_value_per_unit=0.08 if side == "home" else -0.08,
            lower_bound_clearance=-0.03 if side == "home" else -0.11,
            freshness_state="fresh",
            value_checksum=SHA,
            prediction_checksum="b" * 64,
            market_context_checksum="c" * 64,
        )

    outcomes = (outcome("home"), outcome("away"))
    return PdfReportGameV1(
        ordinal=ordinal,
        source_game_id=f"game-{ordinal}",
        away_team_id=f"away-{ordinal}",
        home_team_id=f"home-{ordinal}",
        scheduled_start_time=NOW + timedelta(hours=ordinal),
        decision=decision,
        selected_side="home" if decision == "recommend" else None,
        selected_team_id=f"team-home-{ordinal}" if decision == "recommend" else None,
        recommendation_rank=rank,
        quality_disposition="clear" if decision != "avoid" else "degraded",
        quality_issue_codes=() if decision != "avoid" else ("weather_evidence",),
        provider_kind="reviewed_analyst",
        provider_contract="DSE_REVIEWED_ANALYST_V1",
        provider_version="reviewed-fixture-v1",
        calibration_state="uncalibrated",
        market_independence_attested=True,
        outcomes=outcomes,
        context={"odds_weather": {"weather": {"status": "available"}}},
        upstream_ranking_entry_checksum="d" * 64,
        upstream_gate_game_checksum="e" * 64,
        upstream_value_game_checksum="f" * 64,
        upstream_prediction_game_checksum="1" * 64,
        upstream_matchup_packet_game_checksum="2" * 64,
        upstream_data_quality_game_checksum="3" * 64,
    )


def _semantic_report(
    *,
    recommendations: bool = True,
    run_id: str = "run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
) -> ProductionPdfReportV1:
    games = (
        _report_game(1, "recommend", 1) if recommendations else _report_game(1, "pass"),
        _report_game(2, "pass"),
        _report_game(3, "avoid"),
    )
    order = ("rankings", "recommendation_gate", "value_engine", "predictions", "matchup_packet", "data_quality")
    return ProductionPdfReportV1(
        run_id=run_id,
        requested_date="2026-08-07",
        as_of_time=NOW,
        generated_at=NOW + timedelta(minutes=1),
        policy=PdfReportPolicyV1(),
        upstream_snapshot_ids={key: f"{key}:fixture" for key in order},
        upstream_checksums={key: SHA for key in order},
        games=games,
    )


def _persisted_outputs(
    artifact_root: Path,
    *,
    run_id: str = "run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
) -> tuple[PersistedPdfReportV1, PersistedInfographicV1]:
    report = _semantic_report(run_id=run_id)
    pdf_artifacts = publish_production_pdf_report_artifacts(report, artifact_root)
    persisted_pdf = PersistedPdfReportV1(
        "pdf:fixture",
        report.run_id,
        1,
        report,
        pdf_artifacts,
        "4" * 64,
        report.generated_at,
        report.generated_at,
    )
    infographic = assemble_infographic_document(
        report,
        upstream_pdf_report_snapshot_id=persisted_pdf.snapshot_id,
        generated_at=NOW + timedelta(minutes=2),
    )
    info_artifacts = publish_infographic_artifacts(infographic, artifact_root)
    return persisted_pdf, PersistedInfographicV1(
        "infographic:fixture",
        report.run_id,
        1,
        infographic,
        info_artifacts,
        "5" * 64,
        infographic.generated_at,
        infographic.generated_at,
    )


def _seed_final_qc_upstream_identities(
    database: Database,
    pdf: PersistedPdfReportV1,
    infographic: PersistedInfographicV1,
) -> None:
    """Seed exact sealed identities without constructing unrelated Phase 8-13 fixtures."""

    doc = pdf.document
    info = infographic.document
    ids = doc.upstream_snapshot_ids
    checksums = doc.upstream_checksums
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER pdf_report_snapshot_validate_attempt")
        connection.execute("DROP TRIGGER infographic_snapshot_validate_attempt")
        connection.execute(
            """INSERT INTO pdf_report_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,generated_at,contract_version,render_version,phase_input_checksum,policy_json,policy_checksum,upstream_rankings_snapshot_id,upstream_rankings_checksum,upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum,upstream_value_engine_snapshot_id,upstream_value_engine_checksum,upstream_predictions_snapshot_id,upstream_predictions_checksum,upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum,upstream_data_quality_snapshot_id,upstream_data_quality_checksum,semantic_checksum,game_count,recommendation_count,warning_count,canonical_json,document_relpath,document_checksum,document_byte_count,pdf_relpath,pdf_checksum,pdf_byte_count,pdf_page_count,render_manifest_relpath,render_manifest_checksum,render_manifest_byte_count,report_status,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                pdf.snapshot_id,
                pdf.run_id,
                pdf.phase_attempt,
                doc.requested_date,
                doc.as_of_time.isoformat(),
                doc.generated_at.isoformat(),
                doc.contract_version,
                doc.render_version,
                pdf.phase_input_checksum,
                canonical_text({**doc.policy.as_dict(), "checksum": doc.policy.checksum}),
                doc.policy.checksum,
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
                doc.checksum,
                len(doc.games),
                sum(game.decision == "recommend" for game in doc.games),
                len(doc.warnings),
                doc.canonical_json_bytes().decode(),
                pdf.artifacts.document.relpath,
                pdf.artifacts.document.checksum,
                pdf.artifacts.document.byte_count,
                pdf.artifacts.pdf.relpath,
                pdf.artifacts.pdf.checksum,
                pdf.artifacts.pdf.byte_count,
                pdf.artifacts.page_count,
                pdf.artifacts.manifest.relpath,
                pdf.artifacts.manifest.checksum,
                pdf.artifacts.manifest.byte_count,
                doc.report_status,
                pdf.sealed_at.isoformat(),
                pdf.created_at.isoformat(),
            ),
        )
        connection.execute(
            """INSERT INTO infographic_snapshots(snapshot_id,run_id,phase_attempt,requested_date,as_of_time,generated_at,contract_version,render_version,phase_input_checksum,policy_json,policy_checksum,upstream_pdf_report_snapshot_id,upstream_pdf_report_checksum,infographic_checksum,recommendation_count,warning_count,canonical_json,document_relpath,document_checksum,document_byte_count,feed_relpath,feed_checksum,feed_byte_count,story_relpath,story_checksum,story_byte_count,render_manifest_relpath,render_manifest_checksum,render_manifest_byte_count,report_status,sealed_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                infographic.snapshot_id,
                infographic.run_id,
                infographic.phase_attempt,
                info.requested_date,
                info.as_of_time.isoformat(),
                info.generated_at.isoformat(),
                info.contract_version,
                info.render_version,
                infographic.phase_input_checksum,
                canonical_text({**info.policy.as_dict(), "checksum": info.policy.checksum}),
                info.policy.checksum,
                info.upstream_pdf_report_snapshot_id,
                info.upstream_pdf_report_checksum,
                info.checksum,
                info.full_report_recommendation_count,
                len(info.warnings),
                info.canonical_json_bytes().decode(),
                infographic.artifacts.document.relpath,
                infographic.artifacts.document.checksum,
                infographic.artifacts.document.byte_count,
                infographic.artifacts.feed.relpath,
                infographic.artifacts.feed.checksum,
                infographic.artifacts.feed.byte_count,
                infographic.artifacts.story.relpath,
                infographic.artifacts.story.checksum,
                infographic.artifacts.story.byte_count,
                infographic.artifacts.manifest.relpath,
                infographic.artifacts.manifest.checksum,
                infographic.artifacts.manifest.byte_count,
                info.report_status,
                infographic.sealed_at.isoformat(),
                infographic.created_at.isoformat(),
            ),
        )


def test_production_pdf_and_infographic_reuse_are_deterministic_and_v13_native(tmp_path: Path) -> None:
    report = _semantic_report()
    first = production_pdf_artifact_bytes(report)
    second = production_pdf_artifact_bytes(report)
    assert first == second
    assert first[3].page_count > 0
    semantic = report.canonical_json_bytes().lower()
    assert b'"decision":"recommend"' in semantic
    assert b'"decision":"pass"' in semantic
    assert b'"decision":"avoid"' in semantic
    for obsolete in (b'"bet"', b'"lean"', b"projected_runs", b"spread_probability", b"total_probability"):
        assert obsolete not in semantic
    artifacts = publish_production_pdf_report_artifacts(report, tmp_path)
    verify_production_pdf_report_artifacts(report, artifacts, tmp_path)

    infographic = assemble_infographic_document(
        report,
        upstream_pdf_report_snapshot_id="pdf:fixture",
        generated_at=NOW + timedelta(minutes=2),
    )
    assert infographic.variants[0].pick_of_day is not None
    assert infographic.variants[0].pick_of_day.recommendation_rank == 1
    assert infographic.full_report_recommendation_count == 1
    info_bytes = infographic_artifact_bytes(infographic)
    assert info_bytes == infographic_artifact_bytes(infographic)
    assert b'width="1080" height="1350"' in info_bytes[1]
    assert b'width="1080" height="1920"' in info_bytes[2]
    info_artifacts = publish_infographic_artifacts(infographic, tmp_path)
    verify_infographic_artifacts(infographic, info_artifacts, tmp_path)
    feed_path = tmp_path / info_artifacts.feed.relpath.replace("/", "\\")
    feed_path.write_bytes(info_bytes[1] + b"tampered")
    with pytest.raises(PreModelEvidenceError):
        verify_infographic_artifacts(infographic, info_artifacts, tmp_path)


def test_infographic_zero_recommendation_and_zero_game_semantics() -> None:
    no_recommendations = _semantic_report(recommendations=False)
    infographic = assemble_infographic_document(
        no_recommendations,
        upstream_pdf_report_snapshot_id="pdf:no-recommendations",
        generated_at=NOW + timedelta(minutes=2),
    )
    assert infographic.variants[0].pick_of_day is None
    assert infographic.variants[0].recommendations == ()
    zero = replace(no_recommendations, games=())
    zero_infographic = assemble_infographic_document(
        zero,
        upstream_pdf_report_snapshot_id="pdf:zero",
        generated_at=NOW + timedelta(minutes=2),
    )
    assert zero_infographic.full_report_game_count == 0
    assert zero_infographic.full_report_recommendation_count == 0


def test_final_qc_reconciles_exact_report_and_infographic_and_rejects_pass_promotion(tmp_path: Path) -> None:
    persisted_pdf, persisted_info = _persisted_outputs(tmp_path)
    report = persisted_pdf.document
    infographic = persisted_info.document
    checks, snapshot = assess_final_qc(
        pdf=persisted_pdf,
        infographic=persisted_info,
        evaluated_at=NOW + timedelta(minutes=3),
        artifact_root=tmp_path,
    )
    assert snapshot is not None
    assert tuple(check.code for check in checks) == FINAL_QC_CHECK_CODES
    assert all(check.passed for check in checks)

    pass_game = report.games[1]
    pass_outcome = pass_game.outcomes[0]
    promoted = InfographicSelectionV1(
        1,
        pass_game.source_game_id,
        "promoted pass fixture",
        pass_outcome.outcome_team_id,
        pass_outcome.side,
        pass_outcome.prediction_probability,
        pass_outcome.consensus_no_vig_probability,
        pass_outcome.edge,
        pass_outcome.expected_value_per_unit,
        pass_outcome.best_price,
        pass_outcome.bookmaker_count,
        pass_game.quality_disposition,
        pass_game.checksum,
        pass_game.upstream_ranking_entry_checksum,
        pass_game.upstream_gate_game_checksum,
        pass_outcome.value_checksum,
    )
    bad_feed = replace(infographic.variants[0], pick_of_day=promoted, recommendations=())
    bad_story = replace(infographic.variants[1], pick_of_day=promoted, recommendations=())
    bad_document = replace(infographic, variants=(bad_feed, bad_story))
    bad_info = replace(persisted_info, document=bad_document)
    bad_checks, bad_snapshot = assess_final_qc(
        pdf=persisted_pdf,
        infographic=bad_info,
        evaluated_at=NOW + timedelta(minutes=3),
        artifact_root=tmp_path,
    )
    assert bad_snapshot is None
    failed = {check.code for check in bad_checks if not check.passed}
    assert {"infographic_recommend_subset", "pass_not_promoted"} <= failed


def test_valid_final_qc_persists_one_sealed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(tmp_path / "qc-success.db")
    run_repository = PipelineRunRepository(database)
    controller = ManualRunController(
        run_repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"network_enabled": False},
        clock=lambda: NOW,
    )
    started = controller.start("2026-08-07")
    run_repository.transition_pipeline_run(
        started.run.run_id,
        PipelineRunStatus.RUNNING,
        reason="focused Final QC persistence fixture",
        transitioned_at=NOW.isoformat(),
    )
    run_repository.transition_pipeline_phase(
        started.run.run_id,
        PipelinePhaseKey.FINAL_QC,
        PipelinePhaseStatus.RUNNING,
        reason="focused Final QC persistence fixture",
        transitioned_at=NOW.isoformat(),
    )
    pdf, infographic = _persisted_outputs(tmp_path, run_id=started.run.run_id)
    _seed_final_qc_upstream_identities(database, pdf, infographic)
    repository = FinalQcRepository(database, artifact_root=tmp_path, clock=lambda: NOW + timedelta(minutes=3))
    exact = FinalQcUpstreamV1(pdf, infographic)
    checks, snapshot = repository.assess(exact, evaluated_at=NOW + timedelta(minutes=3))
    assert snapshot is not None
    assert all(check.passed for check in checks)
    monkeypatch.setattr(repository, "get_for_run_attempt", lambda run_id, attempt: object())
    repository.persist_success(
        run_id=started.run.run_id,
        phase_attempt=1,
        phase_input_checksum="9" * 64,
        u=exact,
        snapshot=snapshot,
    )
    with database.connect() as connection:
        row = connection.execute(
            "SELECT sealed_at,check_count FROM final_qc_snapshots WHERE run_id=?",
            (started.run.run_id,),
        ).fetchone()
        assert row is not None and row["sealed_at"] is not None
        assert int(row["check_count"]) == len(FINAL_QC_CHECK_CODES)
        assert connection.execute(
            "SELECT count(*) FROM final_qc_snapshots WHERE run_id=?",
            (started.run.run_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("tampered_pdf", "pdf_artifact_checksum"),
        ("missing_pdf", "pdf_artifact_checksum"),
        ("tampered_feed", "infographic_artifact_checksums"),
        ("missing_story", "infographic_artifact_checksums"),
    ),
)
def test_final_qc_owns_physical_artifact_failures_and_persists_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    expected_code: str,
) -> None:
    database = Database(tmp_path / "qc-failure.db")
    run_repository = PipelineRunRepository(database)
    final_repository = FinalQcRepository(database, artifact_root=tmp_path, clock=lambda: NOW + timedelta(minutes=3))
    handlers = {phase.key: _ReadyHandler() for phase in CANONICAL_PIPELINE_PHASES}
    handlers[PipelinePhaseKey.FINAL_QC] = FinalQcPhaseHandler(
        database,
        artifact_root=tmp_path,
        clock=lambda: NOW + timedelta(minutes=3),
        repository=final_repository,
    )
    controller = ManualRunController(
        run_repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"network_enabled": False},
        handlers=handlers,
        clock=lambda: NOW,
    )
    started = controller.start("2026-08-07")
    pdf, infographic = _persisted_outputs(tmp_path, run_id=started.run.run_id)
    _seed_final_qc_upstream_identities(database, pdf, infographic)
    monkeypatch.setattr(
        final_repository.infographic,
        "get_latest_for_run_for_final_qc",
        lambda run_id: infographic,
    )
    monkeypatch.setattr(
        final_repository.pdf,
        "get_by_snapshot_id_for_final_qc",
        lambda snapshot_id: pdf,
    )

    target = {
        "tampered_pdf": pdf.artifacts.pdf,
        "missing_pdf": pdf.artifacts.pdf,
        "tampered_feed": infographic.artifacts.feed,
        "missing_story": infographic.artifacts.story,
    }[mutation]
    path = tmp_path / target.relpath
    if mutation.startswith("tampered"):
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        path.unlink()

    with pytest.raises(ManualRunExecutionError) as raised:
        controller.resume(started.run.run_id)
    assert "Final QC failed" in str(raised.value)

    failed_run = controller.show(started.run.run_id)
    assert failed_run.run.failure_phase is PipelinePhaseKey.FINAL_QC
    assert failed_run.phases[13].status is PipelinePhaseStatus.FAILED
    reopened = FinalQcRepository(database, artifact_root=tmp_path, clock=lambda: NOW + timedelta(minutes=3))
    evidence = reopened.get_attempt_evidence(started.run.run_id, 1)
    assert evidence.outcome is FinalQcAttemptOutcome.VALIDATION_FAILED
    assert evidence.snapshot_checksum is None
    failed_codes = {check.code for check in evidence.checks if not check.passed}
    assert expected_code in failed_codes
    with database.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM final_qc_snapshots WHERE run_id=?", (started.run.run_id,)
        ).fetchone()[0] == 0
    with pytest.raises(HumanReviewNotFoundError):
        HumanReviewRepository(database, artifact_root=tmp_path).show_target(started.run.run_id)


def test_final_qc_reconciliation_checks_reject_display_and_decision_mismatches(tmp_path: Path) -> None:
    pdf, infographic = _persisted_outputs(tmp_path)
    original = infographic.document.variants[0].pick_of_day
    assert original is not None

    cases = (
        (replace(original, selected_team_id="wrong-team"), "selected_identity"),
        (replace(original, edge=0.99), "displayed_value_metrics"),
        (replace(original, best_price=999.0), "displayed_price_bookmakers"),
    )
    for changed, expected in cases:
        feed = replace(infographic.document.variants[0], pick_of_day=changed)
        story = replace(infographic.document.variants[1], pick_of_day=changed)
        changed_info = replace(infographic, document=replace(infographic.document, variants=(feed, story)))
        checks, snapshot = assess_final_qc(
            pdf=pdf,
            infographic=changed_info,
            evaluated_at=NOW + timedelta(minutes=3),
            artifact_root=tmp_path,
        )
        assert snapshot is None
        assert expected in {check.code for check in checks if not check.passed}

    feed_rank = replace(original)
    story_rank = replace(original)
    feed = replace(infographic.document.variants[0], pick_of_day=feed_rank)
    story = replace(infographic.document.variants[1], pick_of_day=story_rank)
    rank_info = replace(infographic, document=replace(infographic.document, variants=(feed, story)))
    object.__setattr__(feed_rank, "recommendation_rank", 2)
    object.__setattr__(story_rank, "recommendation_rank", 2)
    rank_checks, _ = assess_final_qc(
        pdf=pdf,
        infographic=rank_info,
        evaluated_at=NOW + timedelta(minutes=3),
        artifact_root=tmp_path,
    )
    assert "recommendation_ranks" in {check.code for check in rank_checks if not check.passed}

    avoid_game = pdf.document.games[2]
    avoid_outcome = avoid_game.outcomes[0]
    promoted_avoid = replace(
        original,
        source_game_id=avoid_game.source_game_id,
        selected_team_id=avoid_outcome.outcome_team_id,
        selected_side=avoid_outcome.side,
        upstream_pdf_game_checksum=avoid_game.checksum,
    )
    feed = replace(infographic.document.variants[0], pick_of_day=promoted_avoid)
    story = replace(infographic.document.variants[1], pick_of_day=promoted_avoid)
    avoid_info = replace(infographic, document=replace(infographic.document, variants=(feed, story)))
    avoid_checks, _ = assess_final_qc(
        pdf=pdf,
        infographic=avoid_info,
        evaluated_at=NOW + timedelta(minutes=3),
        artifact_root=tmp_path,
    )
    assert "avoid_not_promoted" in {check.code for check in avoid_checks if not check.passed}


class _ReadyHandler:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.calls = 0

    def awaiting_input(self, run_id: str) -> bool:
        del run_id
        return not self.ready

    def __call__(self, context) -> PhaseExecutionResult:
        self.calls += 1
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED,
            input_checksum=canonical_sha256({"phase": context.phase_key.value, "kind": "input"}),
            output_checksum=canonical_sha256({"phase": context.phase_key.value, "kind": "output"}),
        )


def test_controller_human_wait_is_not_failure_or_attempt(tmp_path: Path) -> None:
    database = Database(tmp_path / "wait.db")
    repository = PipelineRunRepository(database)
    human = _ReadyHandler(ready=False)
    handlers = {phase.key: _ReadyHandler() for phase in CANONICAL_PIPELINE_PHASES}
    handlers[PipelinePhaseKey.HUMAN_REVIEW] = human
    controller = ManualRunController(
        repository,
        timezone_name="America/Los_Angeles",
        configuration_metadata={"network_enabled": False},
        handlers=handlers,
        clock=lambda: NOW,
    )
    summary = controller.start("2026-08-07")
    with pytest.raises(ManualRunAwaitingHumanInput):
        controller.resume(summary.run.run_id)
    waiting = controller.show(summary.run.run_id)
    assert waiting.run.status is PipelineRunStatus.RUNNING
    assert waiting.phases[14].status is PipelinePhaseStatus.PENDING
    assert waiting.phases[14].attempt_count == 0
    human.ready = True
    complete = controller.resume(summary.run.run_id)
    assert complete.phases[14].status is PipelinePhaseStatus.SUCCEEDED
    assert human.calls == 1


def test_human_review_contract_binds_exact_qc_and_artifact_identity() -> None:
    target = HumanReviewTargetV1(
        "run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "2026-08-07",
        "qc:fixture",
        "1" * 64,
        "pdf:fixture",
        "2" * 64,
        "3" * 64,
        "infographic:fixture",
        "4" * 64,
        {"story": "6" * 64, "feed": "5" * 64},
    )
    approve = HumanReviewRecordV1(
        target,
        "reviewer@example.test",
        HumanReviewDecision.APPROVE,
        NOW,
        "exact evidence accepted",
    )
    reject = replace(approve, decision=HumanReviewDecision.REJECT)
    assert approve.checksum != reject.checksum
    assert approve.target.infographic_artifact_checksums == {"feed": "5" * 64, "story": "6" * 64}
    with pytest.raises(HumanReviewError, match="credential-bearing"):
        HumanReviewRecordV1(
            target,
            "reviewer@example.test",
            HumanReviewDecision.APPROVE,
            NOW,
            "contains configured-value",
            secret_values=("configured-value",),
        )
