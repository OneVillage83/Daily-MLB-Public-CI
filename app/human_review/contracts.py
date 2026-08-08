from __future__ import annotations
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_value

HUMAN_REVIEW_CONTRACT_VERSION = "DSE_HUMAN_REVIEW_V1"
HUMAN_REVIEW_PHASE_INPUT_CONTRACT = "DSE_HUMAN_REVIEW_PHASE_INPUT_V1"
HUMAN_REVIEW_ATTEMPT_MANIFEST_CONTRACT = "DSE_HUMAN_REVIEW_ATTEMPT_MANIFEST_V1"


class HumanReviewError(ValueError):
    pass


class HumanReviewDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class HumanReviewTargetV1:
    run_id: str
    requested_date: str
    final_qc_snapshot_id: str
    final_qc_checksum: str
    pdf_report_snapshot_id: str
    pdf_report_checksum: str
    pdf_artifact_checksum: str
    infographic_snapshot_id: str
    infographic_checksum: str
    infographic_artifact_checksums: Mapping[str, str]

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        checksums = (
            self.final_qc_checksum,
            self.pdf_report_checksum,
            self.pdf_artifact_checksum,
            self.infographic_checksum,
            *self.infographic_artifact_checksums.values(),
        )
        if any(len(v) != 64 or v.lower() != v or any(c not in "0123456789abcdef" for c in v) for v in checksums):
            raise HumanReviewError("Human Review target checksum invalid")
        if set(self.infographic_artifact_checksums) != {"feed", "story"}:
            raise HumanReviewError("Human Review target requires feed and story artifacts")
        if any(
            not v or v != v.strip()
            for v in (self.final_qc_snapshot_id, self.pdf_report_snapshot_id, self.infographic_snapshot_id)
        ):
            raise HumanReviewError("Human Review target snapshot identity invalid")
        object.__setattr__(
            self, "infographic_artifact_checksums", dict(sorted(self.infographic_artifact_checksums.items()))
        )

    def identity_dict(self) -> dict[str, object]:
        return {
            "final_qc_checksum": self.final_qc_checksum,
            "final_qc_snapshot_id": self.final_qc_snapshot_id,
            "infographic_artifact_checksums": dict(self.infographic_artifact_checksums),
            "infographic_checksum": self.infographic_checksum,
            "infographic_snapshot_id": self.infographic_snapshot_id,
            "pdf_artifact_checksum": self.pdf_artifact_checksum,
            "pdf_report_checksum": self.pdf_report_checksum,
            "pdf_report_snapshot_id": self.pdf_report_snapshot_id,
            "requested_date": self.requested_date,
            "run_id": self.run_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())


@dataclass(frozen=True, slots=True)
class HumanReviewRecordV1:
    target: HumanReviewTargetV1
    reviewer_id: str
    decision: HumanReviewDecision
    reviewed_at: datetime
    notes: str | None = None
    contract_version: str = HUMAN_REVIEW_CONTRACT_VERSION
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.target.run_id)
        parse_requested_date(self.target.requested_date)
        if not self.reviewer_id or self.reviewer_id != self.reviewer_id.strip():
            raise HumanReviewError("reviewer_id must be trimmed text")
        if self.reviewed_at.tzinfo is None or self.reviewed_at.utcoffset() is None:
            raise HumanReviewError("reviewed_at must be aware")
        object.__setattr__(self, "reviewed_at", self.reviewed_at.astimezone(timezone.utc))
        if self.notes is not None and (not self.notes.strip() or self.notes != self.notes.strip()):
            raise HumanReviewError("notes must be trimmed text when present")
        payload = self.identity_dict()
        configured = tuple(str(v) for v in secret_values if str(v))
        if redact_value(payload, configured) != payload:
            raise HumanReviewError("Human Review contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "decision": self.decision.value,
            "notes": self.notes,
            "reviewed_at": self.reviewed_at.isoformat(),
            "reviewer_id": self.reviewer_id,
            "target": self.target.identity_dict(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
