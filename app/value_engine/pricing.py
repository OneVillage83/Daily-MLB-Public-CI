from __future__ import annotations

import math
from dataclasses import dataclass


class ValueMathError(ValueError):
    """Raised when price/probability value math receives invalid inputs."""


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueMathError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueMathError(f"{name} must be finite")
    return result


def probability(value: object, name: str = "probability") -> float:
    result = _finite(value, name)
    if result < 0.0 or result > 1.0:
        raise ValueMathError(f"{name} must be between 0 and 1")
    return result


def american_price(value: object) -> float:
    result = _finite(value, "american_price")
    if abs(result) < 100.0:
        raise ValueMathError("american_price absolute value must be at least 100")
    return result


def american_to_implied_probability(price: object) -> float:
    normalized = american_price(price)
    if normalized > 0.0:
        return 100.0 / (normalized + 100.0)
    return abs(normalized) / (abs(normalized) + 100.0)


def american_net_profit_per_unit(price: object) -> float:
    normalized = american_price(price)
    if normalized > 0.0:
        return normalized / 100.0
    return 100.0 / abs(normalized)


def conditional_win_probability(
    win_probability: object,
    loss_probability: object,
) -> float:
    win = probability(win_probability, "win_probability")
    loss = probability(loss_probability, "loss_probability")
    decisive = win + loss
    if decisive <= 0.0:
        raise ValueMathError("win and loss probability contain no decisive mass")
    return win / decisive


def expected_value_per_unit(
    *,
    win_probability: object,
    loss_probability: object,
    american_odds: object,
) -> float:
    win = probability(win_probability, "win_probability")
    loss = probability(loss_probability, "loss_probability")
    return win * american_net_profit_per_unit(american_odds) - loss


def fair_decimal_odds(model_probability: object) -> float:
    model = probability(model_probability, "model_probability")
    if model <= 0.0:
        raise ValueMathError("model_probability must be positive for fair odds")
    return 1.0 / model


def fair_american_odds(model_probability: object) -> float:
    model = probability(model_probability, "model_probability")
    if model <= 0.0 or model >= 1.0:
        raise ValueMathError(
            "model_probability must be strictly between 0 and 1 for fair odds"
        )
    if model >= 0.5:
        return -100.0 * model / (1.0 - model)
    return 100.0 * (1.0 - model) / model


@dataclass(frozen=True, slots=True)
class ValueMathV1:
    model_win_probability: float
    model_loss_probability: float
    model_push_probability: float
    conditional_model_probability: float
    american_price: float
    raw_implied_probability: float
    no_vig_probability: float | None
    net_profit_per_unit: float
    expected_value_per_unit: float
    expected_roi_percent: float
    raw_probability_edge: float
    no_vig_probability_edge: float | None
    fair_decimal_odds: float
    fair_american_odds: float


def calculate_value_math(
    *,
    win_probability: object,
    loss_probability: object,
    push_probability: object,
    price: object,
    retained_raw_implied_probability: object | None,
    no_vig_probability: object | None,
) -> ValueMathV1:
    win = probability(win_probability, "win_probability")
    loss = probability(loss_probability, "loss_probability")
    push = probability(push_probability, "push_probability")
    if not math.isclose(win + loss + push, 1.0, abs_tol=1e-9):
        raise ValueMathError("win/loss/push probabilities must sum to one")
    normalized_price = american_price(price)
    calculated_implied = american_to_implied_probability(normalized_price)
    raw_implied = (
        calculated_implied
        if retained_raw_implied_probability is None
        else probability(
            retained_raw_implied_probability,
            "retained_raw_implied_probability",
        )
    )
    if not math.isclose(raw_implied, calculated_implied, abs_tol=1e-12):
        raise ValueMathError(
            "retained raw implied probability disagrees with American price"
        )
    no_vig = (
        None
        if no_vig_probability is None
        else probability(no_vig_probability, "no_vig_probability")
    )
    conditional = conditional_win_probability(win, loss)
    net_profit = american_net_profit_per_unit(normalized_price)
    ev = expected_value_per_unit(
        win_probability=win,
        loss_probability=loss,
        american_odds=normalized_price,
    )
    return ValueMathV1(
        model_win_probability=win,
        model_loss_probability=loss,
        model_push_probability=push,
        conditional_model_probability=conditional,
        american_price=normalized_price,
        raw_implied_probability=raw_implied,
        no_vig_probability=no_vig,
        net_profit_per_unit=net_profit,
        expected_value_per_unit=ev,
        expected_roi_percent=ev * 100.0,
        raw_probability_edge=conditional - raw_implied,
        no_vig_probability_edge=(None if no_vig is None else conditional - no_vig),
        fair_decimal_odds=fair_decimal_odds(conditional),
        fair_american_odds=fair_american_odds(conditional),
    )
