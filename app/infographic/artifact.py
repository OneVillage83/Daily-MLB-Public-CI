from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.daily_slate.contracts import canonical_json_bytes
from app.infographic.contracts import InfographicContractError, InfographicDocumentV1, InfographicVariantType
from app.infographic.renderer import render_infographic_svg
from app.pre_model_evidence import (
    PreModelArtifactV1,
    cleanup_owned_artifact,
    publish_canonical_bytes,
    verify_canonical_bytes,
)


@dataclass(frozen=True, slots=True)
class InfographicArtifactsV1:
    document: PreModelArtifactV1
    feed: PreModelArtifactV1
    story: PreModelArtifactV1
    manifest: PreModelArtifactV1


def infographic_artifact_bytes(document: InfographicDocumentV1) -> tuple[bytes, bytes, bytes, bytes]:
    document_bytes = document.canonical_json_bytes()
    feed = render_infographic_svg(document, InfographicVariantType.FEED_4X5)
    story = render_infographic_svg(document, InfographicVariantType.STORY_9X16)
    base = f"infographic/snapshots/{document.checksum}"
    import hashlib

    manifest = canonical_json_bytes(
        {
            "contract_version": "DSE_INFOGRAPHIC_RENDER_MANIFEST_V1",
            "document_checksum": document.checksum,
            "document_relpath": f"{base}/infographic_document_v1.json",
            "feed": {
                "byte_count": len(feed),
                "height": 1350,
                "relpath": f"{base}/daily_mlb_feed_4x5.svg",
                "sha256": hashlib.sha256(feed).hexdigest(),
                "width": 1080,
            },
            "policy_checksum": document.policy.checksum,
            "render_version": document.render_version,
            "report_status": document.report_status,
            "story": {
                "byte_count": len(story),
                "height": 1920,
                "relpath": f"{base}/daily_mlb_story_9x16.svg",
                "sha256": hashlib.sha256(story).hexdigest(),
                "width": 1080,
            },
            "upstream_pdf_report_checksum": document.upstream_pdf_report_checksum,
            "upstream_pdf_report_snapshot_id": document.upstream_pdf_report_snapshot_id,
        }
    )
    return document_bytes, feed, story, manifest


def publish_infographic_artifacts(document: InfographicDocumentV1, artifact_root: Path) -> InfographicArtifactsV1:
    content = infographic_artifact_bytes(document)
    base = f"infographic/snapshots/{document.checksum}"
    paths = (
        f"{base}/infographic_document_v1.json",
        f"{base}/daily_mlb_feed_4x5.svg",
        f"{base}/daily_mlb_story_9x16.svg",
        f"{base}/render_manifest_v1.json",
    )
    published: list[PreModelArtifactV1] = []
    try:
        for relpath, value in zip(paths, content, strict=True):
            published.append(publish_canonical_bytes(artifact_root, relpath, value))
    except Exception:
        for artifact in reversed(published):
            cleanup_owned_artifact(artifact_root, artifact)
        raise
    return InfographicArtifactsV1(*published)


def verify_infographic_artifacts(
    document: InfographicDocumentV1, artifacts: InfographicArtifactsV1, artifact_root: Path
) -> None:
    content = infographic_artifact_bytes(document)
    base = f"infographic/snapshots/{document.checksum}"
    paths = (
        f"{base}/infographic_document_v1.json",
        f"{base}/daily_mlb_feed_4x5.svg",
        f"{base}/daily_mlb_story_9x16.svg",
        f"{base}/render_manifest_v1.json",
    )
    for artifact, relpath, value in zip(
        (artifacts.document, artifacts.feed, artifacts.story, artifacts.manifest), paths, content, strict=True
    ):
        if artifact.relpath != relpath:
            raise InfographicContractError("Infographic artifact path identity mismatch")
        verify_canonical_bytes(artifact_root, artifact, value)
