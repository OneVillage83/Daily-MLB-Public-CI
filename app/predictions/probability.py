from __future__ import annotations

import math
from dataclasses import dataclass

from app.predictions.contracts import PredictionsContractError, RunDistributionV1


@dataclass(frozen=True, slots=True)
class TotalLineProjectionV1:
    line: float
    over_probability: float
    under_probability: float
    push_probability: float
    approximation_tail_bound: float


@dataclass(frozen=True, slots=True)
class HomeSpreadProjectionV1:
    home_spread: float
    home_cover_probability: float
    away_cover_probability: float
    push_probability: float
    approximation_tail_bound: float


def poisson_probabilities(
    rate: float,
    *,
    maximum_run_support: int,
) -> tuple[tuple[float, ...], float]:
    if not math.isfinite(rate) or rate <= 0.0:
        raise PredictionsContractError("Poisson rate must be positive and finite")
    if maximum_run_support < 0:
        raise PredictionsContractError("maximum_run_support must be nonnegative")
    values = [math.exp(-rate)]
    for runs in range(1, maximum_run_support + 1):
        values.append(values[-1] * rate / runs)
    retained = sum(values)
    tail = max(0.0, 1.0 - retained)
    total = retained + tail
    if total <= 0.0:
        raise PredictionsContractError("Poisson distribution has no probability mass")
    values = [value / total for value in values]
    tail /= total
    return tuple(values), tail


def build_run_distribution(
    *,
    home_run_rate: float,
    away_run_rate: float,
    maximum_run_support: int,
) -> RunDistributionV1:
    home, home_tail = poisson_probabilities(
        home_run_rate,
        maximum_run_support=maximum_run_support,
    )
    away, away_tail = poisson_probabilities(
        away_run_rate,
        maximum_run_support=maximum_run_support,
    )
    retained_joint = (1.0 - home_tail) * (1.0 - away_tail)
    return RunDistributionV1(
        home_run_rate=home_run_rate,
        away_run_rate=away_run_rate,
        maximum_run_support=maximum_run_support,
        home_run_probabilities=home,
        away_run_probabilities=away,
        home_tail_probability=home_tail,
        away_tail_probability=away_tail,
        retained_joint_probability=retained_joint,
        approximation_tail_bound=1.0 - retained_joint,
    )


def decisive_moneyline_probabilities(
    distribution: RunDistributionV1,
) -> tuple[float, float, float]:
    home_win = 0.0
    away_win = 0.0
    tie = 0.0
    for home_runs, home_probability in enumerate(
        distribution.home_run_probabilities
    ):
        for away_runs, away_probability in enumerate(
            distribution.away_run_probabilities
        ):
            joint = home_probability * away_probability
            if home_runs > away_runs:
                home_win += joint
            elif away_runs > home_runs:
                away_win += joint
            else:
                tie += joint
    retained = distribution.retained_joint_probability
    if retained <= 0.0:
        raise PredictionsContractError("run distribution retained no joint mass")
    home_win /= retained
    away_win /= retained
    tie /= retained
    decisive = home_win + away_win
    if decisive <= 0.0:
        raise PredictionsContractError("run distribution has no decisive outcome mass")
    return home_win / decisive, away_win / decisive, tie


def total_line_projection(
    distribution: RunDistributionV1,
    line: float,
) -> TotalLineProjectionV1:
    if not math.isfinite(line):
        raise PredictionsContractError("total line must be finite")
    over = 0.0
    under = 0.0
    push = 0.0
    for home_runs, home_probability in enumerate(
        distribution.home_run_probabilities
    ):
        for away_runs, away_probability in enumerate(
            distribution.away_run_probabilities
        ):
            joint = home_probability * away_probability
            total = home_runs + away_runs
            if total > line:
                over += joint
            elif total < line:
                under += joint
            else:
                push += joint
    retained = distribution.retained_joint_probability
    if retained <= 0.0:
        raise PredictionsContractError("run distribution retained no joint mass")
    return TotalLineProjectionV1(
        line=float(line),
        over_probability=over / retained,
        under_probability=under / retained,
        push_probability=push / retained,
        approximation_tail_bound=distribution.approximation_tail_bound,
    )


def home_spread_projection(
    distribution: RunDistributionV1,
    home_spread: float,
) -> HomeSpreadProjectionV1:
    if not math.isfinite(home_spread):
        raise PredictionsContractError("home spread must be finite")
    home_cover = 0.0
    away_cover = 0.0
    push = 0.0
    for home_runs, home_probability in enumerate(
        distribution.home_run_probabilities
    ):
        for away_runs, away_probability in enumerate(
            distribution.away_run_probabilities
        ):
            joint = home_probability * away_probability
            adjusted_margin = home_runs - away_runs + home_spread
            if adjusted_margin > 0.0:
                home_cover += joint
            elif adjusted_margin < 0.0:
                away_cover += joint
            else:
                push += joint
    retained = distribution.retained_joint_probability
    if retained <= 0.0:
        raise PredictionsContractError("run distribution retained no joint mass")
    return HomeSpreadProjectionV1(
        home_spread=float(home_spread),
        home_cover_probability=home_cover / retained,
        away_cover_probability=away_cover / retained,
        push_probability=push / retained,
        approximation_tail_bound=distribution.approximation_tail_bound,
    )
