from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.final_qc import FINAL_QC_CHECK_CODES
from app.final_qc.assessment import assess_final_qc
from app.database import Database
from app.daily_slate.contracts import canonical_sha256
from app.human_review import (
    HumanReviewDecision,
    HumanReviewError,
    HumanReviewRecordV1,
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
from app.pre_model_evidence import PreModelEvidenceError
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
    PhaseExecutionResult,
)


SHA = "a" * 64
NOW = datetime(2026, 8, 7, 16, tzinfo=timezone.utc)


def _report_game(ordinal: int, decision: str, rank: int | None = None) -> PdfReportGameV1:
    outcomes = tuple(
        PdfReportOutcomeV1(
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
        for side in ("home", "away")
    )
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


def _semantic_report(*, recommendations: bool = True) -> ProductionPdfReportV1:
    games = (
        _report_game(1, "recommend", 1) if recommendations else _report_game(1, "pass"),
        _report_game(2, "pass"),
        _report_game(3, "avoid"),
    )
    order = ("rankings", "recommendation_gate", "value_engine", "predictions", "matchup_packet", "data_quality")
    return ProductionPdfReportV1(
        run_id="run_20260807_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        requested_date="2026-08-07",
        as_of_time=NOW,
        generated_at=NOW + timedelta(minutes=1),
        policy=PdfReportPolicyV1(),
        upstream_snapshot_ids={key: f"{key}:fixture" for key in order},
        upstream_checksums={key: SHA for key in order},
        games=games,
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
    report = _semantic_report()
    pdf_artifacts = publish_production_pdf_report_artifacts(report, tmp_path)
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
    info_artifacts = publish_infographic_artifacts(infographic, tmp_path)
    persisted_info = PersistedInfographicV1(
        "infographic:fixture",
        report.run_id,
        1,
        infographic,
        info_artifacts,
        "5" * 64,
        infographic.generated_at,
        infographic.generated_at,
    )
    checks, snapshot = assess_final_qc(
        pdf=persisted_pdf,
        infographic=persisted_info,
        evaluated_at=NOW + timedelta(minutes=3),
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
    )
    assert bad_snapshot is None
    failed = {check.code for check in bad_checks if not check.passed}
    assert {"infographic_recommend_subset", "pass_not_promoted"} <= failed


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

