from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from app.stats.acquisition import AcquisitionOutcome, AcquisitionResult


AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION = (
    "DSE_AUTO_DAILY_STATS_GAP_REPAIR_V1"
)
_CURRENT_PROVIDER = "current_mlb_stats"
_DATASET_KEY = "regular_season_games"


class AutoDailyStatsGapError(RuntimeError):
    pass


class CompletenessWatermarkReader(Protocol):
    def get_completeness_watermark(
        self,
        *,
        provider: str,
        dataset_key: str,
        season: int,
    ) -> dict[str, object] | None: ...


def _calendar_date(value: object, field: str) -> date:
    if isinstance(value, datetime):
        raise TypeError(f"{field} must be a calendar date")
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise AutoDailyStatsGapError(f"{field} is not a valid YYYY-MM-DD date") from exc
    return parsed


def _optional_date(value: object, field: str) -> date | None:
    if value is None:
        return None
    return _calendar_date(value, field)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def _checksum(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _date_range(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        return ()
    return tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )


@dataclass(frozen=True, slots=True)
class AutoDailyStatsGapPlanV1:
    target_date: date
    repair_through_date: date
    baseline_required: bool
    prior_requested_through_date: date | None
    prior_partial_date: date | None
    repair_dates: tuple[date, ...]
    reason: str
    contract_version: str = AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION

    @property
    def repair_required(self) -> bool:
        return self.baseline_required or bool(self.repair_dates)

    @property
    def checksum(self) -> str:
        return _checksum(self.as_dict())

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "target_date": self.target_date.isoformat(),
            "repair_through_date": self.repair_through_date.isoformat(),
            "baseline_required": self.baseline_required,
            "prior_requested_through_date": (
                self.prior_requested_through_date.isoformat()
                if self.prior_requested_through_date is not None
                else None
            ),
            "prior_partial_date": (
                self.prior_partial_date.isoformat()
                if self.prior_partial_date is not None
                else None
            ),
            "repair_dates": [value.isoformat() for value in self.repair_dates],
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AutoDailyStatsExecutionV1:
    plan: AutoDailyStatsGapPlanV1
    baseline_result: AcquisitionResult | None
    repair_results: tuple[AcquisitionResult, ...]
    daily_result: AcquisitionResult

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION,
            "plan": self.plan.as_dict(),
            "plan_checksum": self.plan.checksum,
            "baseline_result": (
                self.baseline_result.as_dict()
                if self.baseline_result is not None
                else None
            ),
            "repair_results": [result.as_dict() for result in self.repair_results],
            "daily_result": self.daily_result.as_dict(),
        }


def plan_auto_daily_stats_gap(
    repository: CompletenessWatermarkReader,
    target_date: date,
) -> AutoDailyStatsGapPlanV1:
    target = _calendar_date(target_date, "target_date")
    repair_through = target - timedelta(days=1)
    watermark = repository.get_completeness_watermark(
        provider=_CURRENT_PROVIDER,
        dataset_key=_DATASET_KEY,
        season=target.year,
    )

    if watermark is None:
        return AutoDailyStatsGapPlanV1(
            target_date=target,
            repair_through_date=repair_through,
            baseline_required=(repair_through.year == target.year),
            prior_requested_through_date=None,
            prior_partial_date=None,
            repair_dates=(),
            reason="current_season_watermark_missing",
        )

    requested = _calendar_date(
        watermark.get("requested_through_date"),
        "watermark.requested_through_date",
    )
    partial = _optional_date(
        watermark.get("partial_date"),
        "watermark.partial_date",
    )
    partial_reason = watermark.get("partial_date_reason")
    if (partial is None) != (partial_reason is None):
        raise AutoDailyStatsGapError(
            "current-season watermark has inconsistent partial-date evidence"
        )
    if requested.year != target.year:
        raise AutoDailyStatsGapError(
            "current-season watermark belongs to a different season"
        )
    if requested > target:
        raise AutoDailyStatsGapError(
            "current-season watermark is later than the requested daily target"
        )
    if partial is not None and partial > requested:
        raise AutoDailyStatsGapError(
            "current-season watermark partial date exceeds requested-through date"
        )

    if repair_through.year != target.year:
        repair_dates: tuple[date, ...] = ()
        reason = "no_prior_current_season_date"
    else:
        retry_start = (
            partial
            if partial is not None and partial <= repair_through
            else requested + timedelta(days=1)
        )
        retry_start = max(retry_start, date(target.year, 1, 1))
        repair_dates = _date_range(retry_start, repair_through)
        if partial is not None and partial <= repair_through:
            reason = "retry_partial_then_fill_gap"
        elif repair_dates:
            reason = "fill_dates_since_last_checked_through"
        else:
            reason = "no_gap"

    return AutoDailyStatsGapPlanV1(
        target_date=target,
        repair_through_date=repair_through,
        baseline_required=False,
        prior_requested_through_date=requested,
        prior_partial_date=partial,
        repair_dates=repair_dates,
        reason=reason,
    )


def verify_watermark_checked_through(
    repository: CompletenessWatermarkReader,
    through_date: date,
) -> Mapping[str, object]:
    through = _calendar_date(through_date, "through_date")
    watermark = repository.get_completeness_watermark(
        provider=_CURRENT_PROVIDER,
        dataset_key=_DATASET_KEY,
        season=through.year,
    )
    if watermark is None:
        raise AutoDailyStatsGapError(
            "automatic repair completed without a current-season watermark"
        )
    requested = _calendar_date(
        watermark.get("requested_through_date"),
        "watermark.requested_through_date",
    )
    partial = _optional_date(
        watermark.get("partial_date"),
        "watermark.partial_date",
    )
    if requested < through:
        raise AutoDailyStatsGapError(
            "automatic repair did not advance the watermark through the required date"
        )
    if partial is not None and partial <= through:
        raise AutoDailyStatsGapError(
            "automatic repair left an unresolved partial date in the required interval"
        )
    return watermark


class AutoDailyStatsCoordinator:
    """Repair missed current-season stats dates before today's normal DAILY run."""

    def __init__(
        self,
        *,
        repository: CompletenessWatermarkReader,
        run_daily: Callable[[date], AcquisitionResult],
        run_baseline_backfill: Callable[[date], AcquisitionResult],
    ) -> None:
        self.repository = repository
        self.run_daily = run_daily
        self.run_baseline_backfill = run_baseline_backfill

    @staticmethod
    def _require_success(result: AcquisitionResult, label: str) -> None:
        if result.outcome is not AcquisitionOutcome.SUCCESS:
            raise AutoDailyStatsGapError(
                f"{label} did not finish with a successful completeness outcome"
            )

    def execute(self, target_date: date) -> AutoDailyStatsExecutionV1:
        plan = plan_auto_daily_stats_gap(self.repository, target_date)
        baseline_result: AcquisitionResult | None = None
        repair_results: list[AcquisitionResult] = []

        if plan.baseline_required:
            baseline_result = self.run_baseline_backfill(plan.repair_through_date)
            self._require_success(baseline_result, "baseline current-season backfill")
            verify_watermark_checked_through(
                self.repository,
                plan.repair_through_date,
            )
        else:
            for repair_date in plan.repair_dates:
                result = self.run_daily(repair_date)
                self._require_success(
                    result,
                    f"automatic daily gap repair for {repair_date.isoformat()}",
                )
                verify_watermark_checked_through(self.repository, repair_date)
                repair_results.append(result)

            if plan.repair_dates:
                verify_watermark_checked_through(
                    self.repository,
                    plan.repair_through_date,
                )

        daily_result = self.run_daily(plan.target_date)
        return AutoDailyStatsExecutionV1(
            plan=plan,
            baseline_result=baseline_result,
            repair_results=tuple(repair_results),
            daily_result=daily_result,
        )
