from __future__ import annotations

from dataclasses import replace

import pytest

from app.data_quality.contracts import DataQualityDisposition
from app.pdf_report.assembly import assemble_pdf_report_document
from app.pdf_report.contracts import PDF_REPORT_STATUS, PdfReportContractError
from app.pdf_report.renderer import (
    CONTENT_WIDTH,
    PAIR_CARD_WIDTH,
    _fact_card,
    _facts_table,
    _styles,
)
from app.pdf_report.policy import PdfReportPolicyError, PdfReportPolicyV1
from app.pdf_report.contracts import ReportFactV1
from app.rankings.engine import rank_recommendations
from app.recommendation_gate.contracts import RecommendationDecision
from app.recommendation_gate.engine import evaluate_recommendation_gate
from tests.test_recommendation_gate import _base_value_engine, _eligible_value_engine
from tests.test_value_engine import _packet_predictions


def _report_inputs():
    packet, predictions = _packet_predictions()
    gate = evaluate_recommendation_gate(_eligible_value_engine())
    rankings = rank_recommendations(gate)
    return rankings, gate, predictions, packet


def _document():
    rankings, gate, predictions, packet = _report_inputs()
    return assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=gate,
        predictions=predictions,
        matchup_packet=packet,
    )


def test_report_retains_every_rankings_entry_exactly_once() -> None:
    rankings, gate, predictions, packet = _report_inputs()
    document = assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=gate,
        predictions=predictions,
        matchup_packet=packet,
    )
    assert document.row_count == rankings.entry_count
    assert {
        row.upstream_ranking_entry_checksum for row in document.full_slate_rows
    } == {
        entry.checksum for game in rankings.games for entry in game.entries
    }
    assert {
        row.upstream_ranking_entry_checksum
        for game in document.games
        for row in game.decisions
    } == {
        row.upstream_ranking_entry_checksum for row in document.full_slate_rows
    }


def test_ranked_boards_preserve_exact_rank_order() -> None:
    rankings, gate, predictions, packet = _report_inputs()
    document = assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=gate,
        predictions=predictions,
        matchup_packet=packet,
    )
    by_report_checksum = {row.checksum: row for row in document.full_slate_rows}
    assert [
        by_report_checksum[checksum].top_confidence_rank
        for checksum in document.top_confidence_row_checksums
    ] == list(range(1, document.actionable_count + 1))
    assert [
        by_report_checksum[checksum].best_value_rank
        for checksum in document.best_value_row_checksums
    ] == list(range(1, document.actionable_count + 1))
    assert {
        by_report_checksum[checksum].upstream_ranking_entry_checksum
        for checksum in document.top_confidence_row_checksums
    } == {
        placement.ranking_entry_checksum
        for placement in rankings.top_confidence.placements
    }


def test_report_does_not_change_decisions_or_publication_state() -> None:
    rankings, _, _, _ = _report_inputs()
    document = _document()
    upstream = {
        entry.checksum: entry for game in rankings.games for entry in game.entries
    }
    for row in document.full_slate_rows:
        entry = upstream[row.upstream_ranking_entry_checksum]
        assert row.decision is entry.decision
        assert row.review_required is entry.review_required
        assert row.publication_candidate is entry.publication_candidate
    assert document.report_status == PDF_REPORT_STATUS


def test_customer_explanations_are_deterministic_and_plain_language() -> None:
    document = _document()
    assert document.full_slate_rows
    for row in document.full_slate_rows:
        assert row.primary_reason_code in row.reason_codes
        assert row.customer_explanation.endswith(".")
        assert "guarantee" not in row.customer_explanation.lower()
    first = _document()
    second = _document()
    assert first.checksum == second.checksum
    assert first.canonical_json_bytes() == second.canonical_json_bytes()


def test_every_game_has_customer_dossier_and_required_sections() -> None:
    _, _, _, packet = _report_inputs()
    document = _document()
    assert document.game_count == len(packet.games)
    assert {game.source_game_id for game in document.games} == {
        game.source_game_id for game in packet.games
    }
    for game in document.games:
        assert game.game_facts
        assert game.starter_facts
        assert game.lineup_facts
        assert game.bullpen_facts
        assert game.weather_facts
        assert game.odds_facts
        assert game.model_facts
        assert game.analysis


def test_fact_tables_fit_compact_and_full_layout_widths() -> None:
    styles = _styles()
    facts = (
        ReportFactV1(
            label="Game information",
            value="A deliberately long value that should wrap cleanly inside the compact dossier card width.",
        ),
        ReportFactV1(
            label="Starting pitchers",
            value="Another long value that needs to remain inside the nested table without spilling into the neighboring card.",
        ),
    )
    compact_card = _fact_card("Game information", facts, styles)
    compact_width, _ = compact_card.wrap(PAIR_CARD_WIDTH, 2000)
    assert compact_width <= PAIR_CARD_WIDTH

    full_table = _facts_table(facts, styles, width=CONTENT_WIDTH)
    full_width, _ = full_table.wrap(CONTENT_WIDTH, 2000)
    assert full_width <= CONTENT_WIDTH


def test_reference_model_pass_rows_remain_visible_without_ranks() -> None:
    packet, predictions = _packet_predictions()
    value_engine = _base_value_engine()
    ready_game = replace(
        value_engine.games[0],
        quality_disposition=DataQualityDisposition.READY,
    )
    gate = evaluate_recommendation_gate(replace(value_engine, games=(ready_game,)))
    rankings = rank_recommendations(gate)
    document = assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=gate,
        predictions=predictions,
        matchup_packet=packet,
    )
    assert document.actionable_count == 0
    assert document.full_slate_rows
    assert all(
        row.decision is RecommendationDecision.PASS
        for row in document.full_slate_rows
    )
    assert all(row.top_confidence_rank is None for row in document.full_slate_rows)
    assert all(row.best_value_rank is None for row in document.full_slate_rows)


def test_policy_rejects_promotion_rank_rewrite_and_missing_data_fabrication() -> None:
    with pytest.raises(PdfReportPolicyError, match="prohibited"):
        PdfReportPolicyV1(allow_decision_promotion=True)
    with pytest.raises(PdfReportPolicyError, match="prohibited"):
        PdfReportPolicyV1(allow_rank_rewrite=True)
    with pytest.raises(PdfReportPolicyError, match="prohibited"):
        PdfReportPolicyV1(allow_missing_data_fabrication=True)


def test_input_lineage_mismatch_is_rejected() -> None:
    rankings, gate, predictions, packet = _report_inputs()
    with pytest.raises(PdfReportContractError, match="different requested dates"):
        assemble_pdf_report_document(
            rankings=replace(rankings, requested_date="2026-07-28"),
            recommendation_gate=gate,
            predictions=predictions,
            matchup_packet=packet,
        )


def test_report_remains_pre_review_even_when_bet_candidate_exists() -> None:
    document = _document()
    assert any(
        row.decision is RecommendationDecision.BET
        for row in document.full_slate_rows
    )
    assert document.report_status == "pre_review"
    assert "Human Review" in document.responsible_use_notice
