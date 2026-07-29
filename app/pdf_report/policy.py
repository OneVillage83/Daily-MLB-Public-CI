from __future__ import annotations

from dataclasses import dataclass

from app.daily_slate.contracts import canonical_sha256

PDF_REPORT_POLICY_ID = "DSE_MLB_PDF_REPORT_POLICY_V1"
PDF_REPORT_POLICY_VERSION = "1.0.0"


class PdfReportPolicyError(ValueError):
    """Raised when PDF Report V1 policy is weakened or malformed."""


@dataclass(frozen=True, slots=True)
class PdfReportPolicyV1:
    rankings_first: bool = True
    include_all_ranked_candidates: bool = True
    include_all_slate_decisions: bool = True
    include_every_game_dossier: bool = True
    preserve_upstream_decisions: bool = True
    preserve_upstream_ranks: bool = True
    label_missing_evidence: bool = True
    include_data_freshness_section: bool = True
    include_methodology_appendix: bool = True
    include_responsible_use_notice: bool = True
    allow_decision_promotion: bool = False
    allow_rank_rewrite: bool = False
    allow_stake_sizing: bool = False
    allow_publication_approval: bool = False
    allow_missing_data_fabrication: bool = False
    policy_id: str = PDF_REPORT_POLICY_ID
    policy_version: str = PDF_REPORT_POLICY_VERSION

    def __post_init__(self) -> None:
        required_true = (
            "rankings_first",
            "include_all_ranked_candidates",
            "include_all_slate_decisions",
            "include_every_game_dossier",
            "preserve_upstream_decisions",
            "preserve_upstream_ranks",
            "label_missing_evidence",
            "include_data_freshness_section",
            "include_methodology_appendix",
            "include_responsible_use_notice",
        )
        required_false = (
            "allow_decision_promotion",
            "allow_rank_rewrite",
            "allow_stake_sizing",
            "allow_publication_approval",
            "allow_missing_data_fabrication",
        )
        for name in required_true:
            if getattr(self, name) is not True:
                raise PdfReportPolicyError(f"{name} cannot be disabled in V1")
        for name in required_false:
            if getattr(self, name) is not False:
                raise PdfReportPolicyError(f"{name} is prohibited in V1")
        if self.policy_id != PDF_REPORT_POLICY_ID:
            raise PdfReportPolicyError("unsupported PDF Report policy_id")
        if self.policy_version != PDF_REPORT_POLICY_VERSION:
            raise PdfReportPolicyError("unsupported PDF Report policy_version")

    def as_dict(self) -> dict[str, object]:
        return {
            "allow_decision_promotion": self.allow_decision_promotion,
            "allow_missing_data_fabrication": self.allow_missing_data_fabrication,
            "allow_publication_approval": self.allow_publication_approval,
            "allow_rank_rewrite": self.allow_rank_rewrite,
            "allow_stake_sizing": self.allow_stake_sizing,
            "include_all_ranked_candidates": self.include_all_ranked_candidates,
            "include_all_slate_decisions": self.include_all_slate_decisions,
            "include_data_freshness_section": self.include_data_freshness_section,
            "include_every_game_dossier": self.include_every_game_dossier,
            "include_methodology_appendix": self.include_methodology_appendix,
            "include_responsible_use_notice": self.include_responsible_use_notice,
            "label_missing_evidence": self.label_missing_evidence,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "preserve_upstream_decisions": self.preserve_upstream_decisions,
            "preserve_upstream_ranks": self.preserve_upstream_ranks,
            "rankings_first": self.rankings_first,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())
