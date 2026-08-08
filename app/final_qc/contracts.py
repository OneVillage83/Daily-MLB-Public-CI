from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_value

FINAL_QC_CONTRACT_VERSION = "DSE_FINAL_QC_V1"
FINAL_QC_POLICY_VERSION = "DSE_FINAL_QC_POLICY_V1"
FINAL_QC_PHASE_INPUT_CONTRACT = "DSE_FINAL_QC_PHASE_INPUT_V1"
FINAL_QC_ATTEMPT_MANIFEST_CONTRACT = "DSE_FINAL_QC_ATTEMPT_MANIFEST_V1"
FINAL_QC_CHECK_CODES = (
    "pdf_snapshot_exists",
    "pdf_artifact_checksum",
    "pdf_byte_count",
    "pdf_preflight",
    "infographic_snapshot_exists",
    "infographic_artifact_checksums",
    "infographic_dimensions",
    "run_identity",
    "game_inventory",
    "recommendation_identity",
    "recommendation_ranks",
    "selected_identity",
    "displayed_value_metrics",
    "displayed_price_bookmakers",
    "infographic_recommend_subset",
    "infographic_rank_order",
    "pick_of_day_rank_one",
    "pass_not_promoted",
    "avoid_not_promoted",
    "no_unknown_recommendation",
    "lineage_reconciled",
    "timestamps_valid",
    "paths_contained",
    "artifact_sizes",
    "secret_free",
    "no_generation_exception",
)


class FinalQcError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FinalQcPolicyV1:
    required_checks: tuple[str, ...] = FINAL_QC_CHECK_CODES
    policy_version: str = FINAL_QC_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.required_checks != FINAL_QC_CHECK_CODES or self.policy_version != FINAL_QC_POLICY_VERSION:
            raise FinalQcError("Final QC V1 policy is frozen")

    def as_dict(self) -> dict[str, object]:
        return {"policy_version": self.policy_version, "required_checks": list(self.required_checks)}

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class FinalQcCheckV1:
    ordinal: int
    code: str
    passed: bool
    observed: object
    source_checksum: str

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise FinalQcError("QC check ordinal must be positive")
        if self.code not in FINAL_QC_CHECK_CODES or self.ordinal != FINAL_QC_CHECK_CODES.index(self.code) + 1:
            raise FinalQcError("QC check identity/order is invalid")
        if not isinstance(self.passed, bool):
            raise FinalQcError("QC passed must be Boolean")
        if len(self.source_checksum) != 64 or any(c not in "0123456789abcdef" for c in self.source_checksum):
            raise FinalQcError("QC source checksum is invalid")
        canonical_json_bytes(self.observed)

    def identity_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "observed": self.observed,
            "ordinal": self.ordinal,
            "passed": self.passed,
            "source_checksum": self.source_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class FinalQcV1:
    run_id: str
    requested_date: str
    as_of_time: datetime
    evaluated_at: datetime
    policy: FinalQcPolicyV1
    upstream_pdf_report_snapshot_id: str
    upstream_pdf_report_checksum: str
    upstream_infographic_snapshot_id: str
    upstream_infographic_checksum: str
    checks: tuple[FinalQcCheckV1, ...]
    contract_version: str = FINAL_QC_CONTRACT_VERSION
    overall_result: str = "pass"
    secret_values: InitVar[Iterable[str]] = ()

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        validate_run_id(self.run_id)
        parse_requested_date(self.requested_date)
        for n in ("as_of_time", "evaluated_at"):
            v = getattr(self, n)
            if v.tzinfo is None or v.utcoffset() is None:
                raise FinalQcError(f"{n} must be aware")
            object.__setattr__(self, n, v.astimezone(timezone.utc))
        if tuple(c.code for c in self.checks) != FINAL_QC_CHECK_CODES or not all(c.passed for c in self.checks):
            raise FinalQcError("sealed Final QC requires every frozen check to pass")
        if self.contract_version != FINAL_QC_CONTRACT_VERSION or self.overall_result != "pass":
            raise FinalQcError("Final QC snapshot identity is invalid")
        payload = self.identity_dict()
        configured = tuple(str(v) for v in secret_values if str(v))
        if redact_value(payload, configured) != payload:
            raise FinalQcError("Final QC contains credential-bearing material")

    def identity_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "checks": [c.as_dict() for c in self.checks],
            "contract_version": self.contract_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "overall_result": self.overall_result,
            "policy": {**self.policy.as_dict(), "checksum": self.policy.checksum},
            "requested_date": self.requested_date,
            "run_id": self.run_id,
            "upstream_infographic_checksum": self.upstream_infographic_checksum,
            "upstream_infographic_snapshot_id": self.upstream_infographic_snapshot_id,
            "upstream_pdf_report_checksum": self.upstream_pdf_report_checksum,
            "upstream_pdf_report_snapshot_id": self.upstream_pdf_report_snapshot_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.identity_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
