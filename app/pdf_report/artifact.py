from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.daily_slate.contracts import canonical_json_bytes
from app.pdf_report.contracts import PdfReportContractError, PdfReportDocumentV1
from app.pdf_report.renderer import render_pdf_report

PDF_REPORT_BASE_RELPATH = "pdf_report/snapshots/{report_checksum}"
PDF_REPORT_DOCUMENT_FILENAME = "pdf_report_document_v1.json"
PDF_REPORT_PDF_FILENAME = "daily_mlb_report_v1.pdf"
PDF_REPORT_MANIFEST_FILENAME = "render_manifest_v1.json"
_PAGE_PATTERN = re.compile(rb"/Type\s*/Page\b")


@dataclass(frozen=True, slots=True)
class PdfPreflightV1:
    byte_count: int
    sha256: str
    page_count: int
    header_valid: bool
    eof_valid: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "eof_valid": self.eof_valid,
            "header_valid": self.header_valid,
            "page_count": self.page_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PdfReportArtifactsV1:
    document_relpath: str
    pdf_relpath: str
    manifest_relpath: str
    document_sha256: str
    pdf_sha256: str
    manifest_sha256: str
    page_count: int


def pdf_preflight(content: bytes) -> PdfPreflightV1:
    header_valid = content.startswith(b"%PDF-")
    eof_valid = content.rstrip().endswith(b"%%EOF")
    page_count = len(_PAGE_PATTERN.findall(content))
    result = PdfPreflightV1(
        byte_count=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        page_count=page_count,
        header_valid=header_valid,
        eof_valid=eof_valid,
    )
    if not header_valid or not eof_valid or page_count <= 0:
        raise PdfReportContractError("rendered PDF failed structural preflight")
    return result


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def write_pdf_report_artifacts(
    document: PdfReportDocumentV1,
    artifact_root: Path,
) -> PdfReportArtifactsV1:
    base = PDF_REPORT_BASE_RELPATH.format(report_checksum=document.checksum)
    document_relpath = validate_artifact_relpath(
        f"{base}/{PDF_REPORT_DOCUMENT_FILENAME}"
    )
    pdf_relpath = validate_artifact_relpath(f"{base}/{PDF_REPORT_PDF_FILENAME}")
    manifest_relpath = validate_artifact_relpath(
        f"{base}/{PDF_REPORT_MANIFEST_FILENAME}"
    )
    document_bytes = document.canonical_json_bytes()
    pdf_bytes = render_pdf_report(document)
    preflight = pdf_preflight(pdf_bytes)
    document_sha = hashlib.sha256(document_bytes).hexdigest()
    manifest = {
        "contract_version": "DSE_PDF_REPORT_RENDER_MANIFEST_V1",
        "document_checksum": document.checksum,
        "document_relpath": document_relpath,
        "document_sha256": document_sha,
        "pdf_relpath": pdf_relpath,
        "pdf_preflight": preflight.as_dict(),
        "policy_checksum": document.policy.checksum,
        "report_status": document.report_status,
        "upstream_matchup_packet_checksum": document.upstream_matchup_packet_checksum,
        "upstream_predictions_checksum": document.upstream_predictions_checksum,
        "upstream_rankings_checksum": document.upstream_rankings_checksum,
        "upstream_recommendation_gate_checksum": (
            document.upstream_recommendation_gate_checksum
        ),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    destinations = (
        (resolve_contained_path(artifact_root, document_relpath), document_bytes),
        (resolve_contained_path(artifact_root, pdf_relpath), pdf_bytes),
        (resolve_contained_path(artifact_root, manifest_relpath), manifest_bytes),
    )
    for destination, content in destinations:
        _atomic_write(destination, content)
    return PdfReportArtifactsV1(
        document_relpath=document_relpath,
        pdf_relpath=pdf_relpath,
        manifest_relpath=manifest_relpath,
        document_sha256=document_sha,
        pdf_sha256=preflight.sha256,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        page_count=preflight.page_count,
    )
