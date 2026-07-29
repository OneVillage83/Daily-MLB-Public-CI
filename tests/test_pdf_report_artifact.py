from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.pdf_report.artifact import pdf_preflight, write_pdf_report_artifacts
from app.pdf_report.assembly import assemble_pdf_report_document
from app.pdf_report.renderer import render_pdf_report
from app.rankings.engine import rank_recommendations
from app.recommendation_gate.engine import evaluate_recommendation_gate
from tests.test_recommendation_gate import _eligible_value_engine
from tests.test_value_engine import _packet_predictions


def _document():
    packet, predictions = _packet_predictions()
    gate = evaluate_recommendation_gate(_eligible_value_engine())
    rankings = rank_recommendations(gate)
    return assemble_pdf_report_document(
        rankings=rankings,
        recommendation_gate=gate,
        predictions=predictions,
        matchup_packet=packet,
    )


def test_renderer_produces_structurally_valid_pdf() -> None:
    content = render_pdf_report(_document())
    preflight = pdf_preflight(content)
    assert content.startswith(b"%PDF-")
    assert content.rstrip().endswith(b"%%EOF")
    assert preflight.page_count >= 4
    assert preflight.byte_count == len(content)
    assert preflight.sha256 == hashlib.sha256(content).hexdigest()


def test_renderer_is_byte_deterministic() -> None:
    document = _document()
    first = render_pdf_report(document)
    second = render_pdf_report(document)
    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()


def test_artifacts_write_semantic_json_pdf_and_manifest(tmp_path: Path) -> None:
    document = _document()
    result = write_pdf_report_artifacts(document, tmp_path)
    document_path = tmp_path / result.document_relpath
    pdf_path = tmp_path / result.pdf_relpath
    manifest_path = tmp_path / result.manifest_relpath
    assert document_path.read_bytes() == document.canonical_json_bytes()
    assert pdf_path.read_bytes().startswith(b"%PDF-")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["document_checksum"] == document.checksum
    assert manifest["report_status"] == "pre_review"
    assert manifest["pdf_preflight"]["sha256"] == result.pdf_sha256
    assert manifest["pdf_preflight"]["page_count"] == result.page_count
    assert result.document_sha256 == hashlib.sha256(document_path.read_bytes()).hexdigest()
    assert result.manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def test_artifact_write_is_idempotent(tmp_path: Path) -> None:
    document = _document()
    first = write_pdf_report_artifacts(document, tmp_path)
    first_pdf = (tmp_path / first.pdf_relpath).read_bytes()
    first_manifest = (tmp_path / first.manifest_relpath).read_bytes()
    second = write_pdf_report_artifacts(document, tmp_path)
    assert first == second
    assert (tmp_path / second.pdf_relpath).read_bytes() == first_pdf
    assert (tmp_path / second.manifest_relpath).read_bytes() == first_manifest


def test_artifact_paths_are_content_addressed(tmp_path: Path) -> None:
    document = _document()
    result = write_pdf_report_artifacts(document, tmp_path)
    expected_segment = f"pdf_report/snapshots/{document.checksum}/"
    assert result.document_relpath.startswith(expected_segment)
    assert result.pdf_relpath.startswith(expected_segment)
    assert result.manifest_relpath.startswith(expected_segment)
