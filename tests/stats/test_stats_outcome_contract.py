from __future__ import annotations

from datetime import date

import pytest

from app.stats.acquisition import (
    AcquisitionOutcome,
    build_acquisition_outcome_evidence,
    classify_acquisition_outcome,
)
from app.stats.completeness import (
    COMPLETENESS_REASON_CONTRACT_VERSION,
    CompletenessDisposition,
    CompletenessResult,
    classify_completeness_reason,
)


@pytest.mark.parametrize(
    ("reason", "expected_disposition", "expected_outcome"),
    (
        (
            "scheduled_regular_season_game_not_final",
            CompletenessDisposition.RETRYABLE_PARTIAL,
            AcquisitionOutcome.PARTIAL,
        ),
        (
            "regular_season_game_in_progress",
            CompletenessDisposition.RETRYABLE_PARTIAL,
            AcquisitionOutcome.PARTIAL,
        ),
        (
            "postponed_regular_season_game_unresolved",
            CompletenessDisposition.RETRYABLE_PARTIAL,
            AcquisitionOutcome.PARTIAL,
        ),
        (
            "suspended_regular_season_game_pending",
            CompletenessDisposition.RETRYABLE_PARTIAL,
            AcquisitionOutcome.PARTIAL,
        ),
        (
            "abandoned_regular_season_game_unresolved",
            CompletenessDisposition.REVIEW_PARTIAL,
            AcquisitionOutcome.PARTIAL,
        ),
        (
            "regular_season_game_validation_failed",
            CompletenessDisposition.HARD_FAILURE,
            AcquisitionOutcome.FAILED,
        ),
        (
            "regular_season_game_source_incomplete",
            CompletenessDisposition.HARD_FAILURE,
            AcquisitionOutcome.FAILED,
        ),
        (
            "final_regular_season_game_not_fully_validated",
            CompletenessDisposition.HARD_FAILURE,
            AcquisitionOutcome.FAILED,
        ),
    ),
)
def test_completeness_reasons_share_one_outcome_contract(
    reason: str,
    expected_disposition: CompletenessDisposition,
    expected_outcome: AcquisitionOutcome,
) -> None:
    requested = date(2026, 7, 24)
    completeness = CompletenessResult(
        requested_through_date=requested,
        contiguous_regular_season_complete_through_date=date(2026, 7, 22),
        latest_ingested_completed_game_date=date(2026, 7, 22),
        partial_date=date(2026, 7, 23),
        partial_date_reason=reason,
    )

    assert COMPLETENESS_REASON_CONTRACT_VERSION == (
        "DSE_STATS_COMPLETENESS_REASON_V1"
    )
    assert classify_completeness_reason(reason) is expected_disposition

    evidence = build_acquisition_outcome_evidence(
        completeness,
        completeness_watermark_eligible=True,
    )
    assert classify_acquisition_outcome(evidence) is expected_outcome


def test_unknown_completeness_reason_fails_closed() -> None:
    requested = date(2026, 7, 24)
    reason = "future_provider_condition_not_yet_classified"

    completeness = CompletenessResult(
        requested_through_date=requested,
        contiguous_regular_season_complete_through_date=date(2026, 7, 22),
        latest_ingested_completed_game_date=date(2026, 7, 22),
        partial_date=date(2026, 7, 23),
        partial_date_reason=reason,
    )

    assert (
        classify_completeness_reason(reason)
        is CompletenessDisposition.HARD_FAILURE
    )

    evidence = build_acquisition_outcome_evidence(
        completeness,
        completeness_watermark_eligible=True,
    )

    assert classify_acquisition_outcome(evidence) is AcquisitionOutcome.FAILED


def test_inconsistent_completeness_evidence_fails_closed() -> None:
    completeness = CompletenessResult(
        requested_through_date=date(2026, 7, 24),
        contiguous_regular_season_complete_through_date=date(2026, 7, 22),
        latest_ingested_completed_game_date=date(2026, 7, 22),
        partial_date=date(2026, 7, 23),
        partial_date_reason=None,
    )

    evidence = build_acquisition_outcome_evidence(
        completeness,
        completeness_watermark_eligible=True,
    )

    assert "invalid_completeness_reason_contract" in evidence.hard_failures
    assert classify_acquisition_outcome(evidence) is AcquisitionOutcome.FAILED


def test_cross_source_reconciliation_failure_still_fails() -> None:
    completeness = CompletenessResult(
        requested_through_date=date(2026, 7, 24),
        contiguous_regular_season_complete_through_date=date(2026, 7, 24),
        latest_ingested_completed_game_date=date(2026, 7, 24),
        partial_date=None,
        partial_date_reason=None,
    )

    evidence = build_acquisition_outcome_evidence(
        completeness,
        completeness_watermark_eligible=False,
    )

    assert (
        "unexplained_cross_source_reconciliation_difference"
        in evidence.hard_failures
    )
    assert classify_acquisition_outcome(evidence) is AcquisitionOutcome.FAILED
