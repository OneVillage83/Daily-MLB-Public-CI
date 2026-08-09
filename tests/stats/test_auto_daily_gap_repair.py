from __future__ import annotations

from datetime import date

import pytest

from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionOutcome,
    AcquisitionResult,
)
from app.stats.auto_daily import (
    AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION,
    AutoDailyStatsCoordinator,
    AutoDailyStatsGapError,
    plan_auto_daily_stats_gap,
    verify_watermark_checked_through,
)


class FakeRepository:
    def __init__(self, watermark: dict[str, object] | None) -> None:
        self.watermark = watermark

    def get_completeness_watermark(
        self,
        *,
        provider: str,
        dataset_key: str,
        season: int,
    ) -> dict[str, object] | None:
        assert provider == "current_mlb_stats"
        assert dataset_key == "regular_season_games"
        if self.watermark is None:
            return None
        assert str(self.watermark["requested_through_date"]).startswith(str(season))
        return dict(self.watermark)

    def advance(self, requested_date: date, *, partial: date | None = None) -> None:
        self.watermark = {
            "requested_through_date": requested_date.isoformat(),
            "partial_date": partial.isoformat() if partial is not None else None,
            "partial_date_reason": (
                "scheduled_regular_season_game_not_final"
                if partial is not None
                else None
            ),
        }


def result(
    command: AcquisitionCommand,
    requested_date: date,
    *,
    outcome: AcquisitionOutcome = AcquisitionOutcome.SUCCESS,
) -> AcquisitionResult:
    return AcquisitionResult(
        command=command,
        status=("completed" if outcome is AcquisitionOutcome.SUCCESS else "completed_with_warnings"),
        requested_through_date=requested_date,
        run_id=f"run_{requested_date.isoformat()}",
        stats_run_id=f"stats_{requested_date.isoformat()}",
        counts={},
        completeness=None,
        warnings=(),
        report_path=None,
        dry_run=False,
        outcome=outcome,
    )


def test_plan_uses_one_baseline_backfill_when_watermark_is_missing() -> None:
    plan = plan_auto_daily_stats_gap(FakeRepository(None), date(2026, 8, 9))

    assert plan.contract_version == AUTO_DAILY_STATS_GAP_REPAIR_CONTRACT_VERSION
    assert plan.baseline_required is True
    assert plan.repair_through_date == date(2026, 8, 8)
    assert plan.repair_dates == ()
    assert plan.reason == "current_season_watermark_missing"
    assert len(plan.checksum) == 64


def test_plan_repairs_only_dates_after_last_checked_through() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-03",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )

    plan = plan_auto_daily_stats_gap(repository, date(2026, 8, 9))

    assert plan.baseline_required is False
    assert plan.repair_dates == tuple(
        date(2026, 8, day) for day in range(4, 9)
    )
    assert plan.reason == "fill_dates_since_last_checked_through"


def test_plan_retries_unresolved_partial_date_before_later_missing_dates() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-06",
            "partial_date": "2026-08-04",
            "partial_date_reason": "scheduled_regular_season_game_not_final",
        }
    )

    plan = plan_auto_daily_stats_gap(repository, date(2026, 8, 9))

    assert plan.repair_dates == tuple(
        date(2026, 8, day) for day in range(4, 9)
    )
    assert plan.reason == "retry_partial_then_fill_gap"


def test_plan_has_no_gap_when_yesterday_was_already_checked() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-08",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )

    plan = plan_auto_daily_stats_gap(repository, date(2026, 8, 9))

    assert plan.repair_required is False
    assert plan.repair_dates == ()
    assert plan.reason == "no_gap"


def test_coordinator_repairs_gap_chronologically_then_runs_today() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-05",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )
    calls: list[tuple[str, date]] = []

    def run_daily(requested_date: date) -> AcquisitionResult:
        calls.append(("daily", requested_date))
        repository.advance(requested_date)
        return result(AcquisitionCommand.DAILY, requested_date)

    def run_baseline(requested_date: date) -> AcquisitionResult:
        raise AssertionError("baseline backfill should not run")

    execution = AutoDailyStatsCoordinator(
        repository=repository,
        run_daily=run_daily,
        run_baseline_backfill=run_baseline,
    ).execute(date(2026, 8, 9))

    assert calls == [
        ("daily", date(2026, 8, 6)),
        ("daily", date(2026, 8, 7)),
        ("daily", date(2026, 8, 8)),
        ("daily", date(2026, 8, 9)),
    ]
    assert [item.requested_through_date for item in execution.repair_results] == [
        date(2026, 8, 6),
        date(2026, 8, 7),
        date(2026, 8, 8),
    ]
    assert execution.daily_result.requested_through_date == date(2026, 8, 9)


def test_coordinator_runs_baseline_once_on_first_current_season_run() -> None:
    repository = FakeRepository(None)
    calls: list[tuple[str, date]] = []

    def run_baseline(requested_date: date) -> AcquisitionResult:
        calls.append(("backfill", requested_date))
        repository.advance(requested_date)
        return result(AcquisitionCommand.BACKFILL_CURRENT, requested_date)

    def run_daily(requested_date: date) -> AcquisitionResult:
        calls.append(("daily", requested_date))
        repository.advance(requested_date)
        return result(AcquisitionCommand.DAILY, requested_date)

    execution = AutoDailyStatsCoordinator(
        repository=repository,
        run_daily=run_daily,
        run_baseline_backfill=run_baseline,
    ).execute(date(2026, 8, 9))

    assert calls == [
        ("backfill", date(2026, 8, 8)),
        ("daily", date(2026, 8, 9)),
    ]
    assert execution.baseline_result is not None
    assert execution.repair_results == ()


def test_repair_stops_before_today_when_a_missing_day_remains_partial() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-06",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )
    calls: list[date] = []

    def run_daily(requested_date: date) -> AcquisitionResult:
        calls.append(requested_date)
        repository.advance(requested_date, partial=requested_date)
        return result(
            AcquisitionCommand.DAILY,
            requested_date,
            outcome=AcquisitionOutcome.PARTIAL,
        )

    with pytest.raises(
        AutoDailyStatsGapError,
        match="did not finish with a successful completeness outcome",
    ):
        AutoDailyStatsCoordinator(
            repository=repository,
            run_daily=run_daily,
            run_baseline_backfill=lambda _: pytest.fail("unexpected baseline"),
        ).execute(date(2026, 8, 9))

    assert calls == [date(2026, 8, 7)]


def test_repair_stops_if_success_result_does_not_advance_watermark() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-06",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )

    def run_daily(requested_date: date) -> AcquisitionResult:
        return result(AcquisitionCommand.DAILY, requested_date)

    with pytest.raises(
        AutoDailyStatsGapError,
        match="did not advance the watermark",
    ):
        AutoDailyStatsCoordinator(
            repository=repository,
            run_daily=run_daily,
            run_baseline_backfill=lambda _: pytest.fail("unexpected baseline"),
        ).execute(date(2026, 8, 9))


def test_today_may_preserve_existing_partial_outcome_after_successful_repairs() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-08",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )

    def run_daily(requested_date: date) -> AcquisitionResult:
        repository.advance(requested_date, partial=requested_date)
        return result(
            AcquisitionCommand.DAILY,
            requested_date,
            outcome=AcquisitionOutcome.PARTIAL,
        )

    execution = AutoDailyStatsCoordinator(
        repository=repository,
        run_daily=run_daily,
        run_baseline_backfill=lambda _: pytest.fail("unexpected baseline"),
    ).execute(date(2026, 8, 9))

    assert execution.repair_results == ()
    assert execution.daily_result.outcome is AcquisitionOutcome.PARTIAL


def test_future_watermark_fails_closed() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-10",
            "partial_date": None,
            "partial_date_reason": None,
        }
    )

    with pytest.raises(AutoDailyStatsGapError, match="later than"):
        plan_auto_daily_stats_gap(repository, date(2026, 8, 9))


def test_watermark_verification_rejects_unresolved_partial_date() -> None:
    repository = FakeRepository(
        {
            "requested_through_date": "2026-08-08",
            "partial_date": "2026-08-07",
            "partial_date_reason": "scheduled_regular_season_game_not_final",
        }
    )

    with pytest.raises(AutoDailyStatsGapError, match="unresolved partial"):
        verify_watermark_checked_through(repository, date(2026, 8, 8))
