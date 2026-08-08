from __future__ import annotations

from pathlib import Path

import pytest

from app.infographic.artifact import publish_infographic_artifacts, verify_infographic_artifacts
from app.infographic.assembly import assemble_infographic_document
from app.pre_model_evidence import PreModelEvidenceError
from tests.test_final_output_pipeline import NOW, _semantic_report


def test_infographic_artifacts_are_content_addressed_contained_and_idempotent(tmp_path: Path) -> None:
    document = assemble_infographic_document(
        _semantic_report(),
        upstream_pdf_report_snapshot_id="pdf-report:fixture",
        generated_at=NOW,
    )
    first = publish_infographic_artifacts(document, tmp_path)
    second = publish_infographic_artifacts(document, tmp_path)
    first_inventory = (first.document, first.feed, first.story, first.manifest)
    second_inventory = (second.document, second.feed, second.story, second.manifest)
    assert tuple((item.relpath, item.checksum, item.byte_count) for item in first_inventory) == tuple(
        (item.relpath, item.checksum, item.byte_count) for item in second_inventory
    )
    for artifact in (first.document, first.feed, first.story, first.manifest):
        assert document.checksum in artifact.relpath
        assert (tmp_path / artifact.relpath).resolve().is_relative_to(tmp_path.resolve())
    verify_infographic_artifacts(document, first, tmp_path)


def test_infographic_artifact_tamper_fails_closed(tmp_path: Path) -> None:
    document = assemble_infographic_document(
        _semantic_report(),
        upstream_pdf_report_snapshot_id="pdf-report:fixture",
        generated_at=NOW,
    )
    artifacts = publish_infographic_artifacts(document, tmp_path)
    (tmp_path / artifacts.story.relpath).write_bytes(b"<svg>tampered</svg>")
    with pytest.raises(PreModelEvidenceError):
        verify_infographic_artifacts(document, artifacts, tmp_path)
