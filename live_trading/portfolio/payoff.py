"""
SPX option strategy payoff calculations.

Conventions:
- Long option:  quantity > 0
- Short option: quantity < 0
- Premium: always stored as a positive dollar-per-index-point number
- SPX multiplier: normally 100

Expiry P&L:
    realised P&L
    + sum((intrinsic - entry_price) * signed_quantity * multiplier)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

import numpy as np

from live_trading.portfolio.models import Lot, Right


@dataclass
class PayoffCurves:
    spot_grid: List[float]
    expiry_pnl: List[float]
    target_pnl: List[float]
    breakevens_expiry: List[float]
    max_profit: Optional[float]
    max_loss: Optional[float]
    current_spot_pnl_expiry: float
    current_spot_pnl_target: float
    grid_min: float
    grid_max: float


def intrinsic_value(
    right: Right,
    spot: float,
    strike: float,
) -> float:
    if right == Right.CALL:
        return max(spot - strike, 0.0)

    return max(strike - spot, 0.0)


def expiry_leg_pnl(
    lot: Lot,
    spot: float,
) -> float:
    """
    One leg's final P&L at expiry.

    Examples:
    - Long 6550P at 10.00:
        at 6500 -> (50 - 10) * +1 * 100 = +4000
    - Short 6550P at 10.00:
        at 6500 -> (50 - 10) * -1 * 100 = -4000
    """
    intrinsic = intrinsic_value(
        right=lot.right,
        spot=spot,
        strike=lot.strike,
    )

    return (
        (intrinsic - lot.entry_price)
        * lot.open_quantity
        * lot.multiplier
    )


def _valid_price(
    value: Optional[float],
) -> Optional[float]:
    if value is None:
        return None

    try:
        value = float(value)
    except (TypeError, ValueError):
        return None

    if not np.isfinite(value) or value <= 0:
        return None

    return value


def theoretical_price(
    right: Right,
    spot: float,
    strike: float,
    years_to_expiry: float,
    iv: Optional[float],
    greeks_engine=None,
    risk_free_rate: float = 0.0,
) -> Optional[float]:
    """
    Return a target-date theoretical price.

    This function intentionally returns None if it cannot price the option.
    It does NOT silently substitute intrinsic value for a non-expiry date,
    because that creates a misleading 'today' curve.
    """
    if years_to_expiry <= 0:
        return intrinsic_value(
            right=right,
            spot=spot,
            strike=strike,
        )

    valid_iv = _valid_price(iv)

    if greeks_engine is None or valid_iv is None:
        return None

    try:
        price = greeks_engine.price(
            right.value,
            spot,
            strike,
            years_to_expiry,
            valid_iv,
            risk_free_rate,
        )
    except (AttributeError, TypeError, ValueError):
        return None

    return _valid_price(price)


def determine_price_grid(
    open_lots: List[Lot],
    current_spot: float,
    padding_points: float = 250.0,
    step_points: float = 5.0,
) -> Tuple[List[float], float, float]:
    """
    Always include every selected strike plus a safety band.

    A fixed +/-250 point range is insufficient when a leg is outside that
    window, and causes misleading max-profit/max-loss values.
    """
    strikes = [
        float(lot.strike)
        for lot in open_lots
    ]

    lowest_anchor = min(
        [float(current_spot)] + strikes,
    )

    highest_anchor = max(
        [float(current_spot)] + strikes,
    )

    grid_min = max(
        step_points,
        lowest_anchor - padding_points,
    )

    grid_max = highest_anchor + padding_points

    grid = list(
        np.arange(
            grid_min,
            grid_max + step_points,
            step_points,
        )
    )

    return grid, grid_min, grid_max


def build_payoff_curves(
    open_lots: List[Lot],
    realized_pnl: float,
    current_spot: float,
    target_date: Optional[date] = None,
    iv_by_lot: Optional[Dict[str, float]] = None,
    greeks_engine=None,
    commissions: float = 0.0,
    padding_points: float = 250.0,
    step_points: float = 5.0,
) -> PayoffCurves:
    """
    Build final-expiry P&L and target-date P&L curves.

    Target P&L is only meaningful when every open leg can be priced using
    a real IV and the Greeks engine. If not, it is set equal to the expiry
    curve and the UI should display a warning.
    """
    realised = float(realized_pnl or 0.0)
    total_commissions = float(commissions or 0.0)

    if not open_lots:
        pnl = realised - total_commissions

        return PayoffCurves(
            spot_grid=[current_spot],
            expiry_pnl=[pnl],
            target_pnl=[pnl],
            breakevens_expiry=[],
            max_profit=pnl,
            max_loss=pnl,
            current_spot_pnl_expiry=pnl,
            current_spot_pnl_target=pnl,
            grid_min=current_spot,
            grid_max=current_spot,
        )

    grid, grid_min, grid_max = determine_price_grid(
        open_lots=open_lots,
        current_spot=current_spot,
        padding_points=padding_points,
        step_points=step_points,
    )

    expiry_curve: List[float] = []
    target_curve: List[float] = []

    target = target_date or date.today()

    all_target_prices_available = (
        greeks_engine is not None
        and iv_by_lot is not None
    )

    for spot in grid:
        expiry_total = realised - total_commissions
        target_total = realised - total_commissions

        for lot in open_lots:
            expiry_total += expiry_leg_pnl(
                lot=lot,
                spot=spot,
            )

            years_to_expiry = max(
                (lot.expiry - target).days,
                0,
            ) / 365.0

            iv = (
                iv_by_lot.get(lot.lot_id)
                if iv_by_lot
                else None
            )

            target_option_price = theoretical_price(
                right=lot.right,
                spot=spot,
                strike=lot.strike,
                years_to_expiry=years_to_expiry,
                iv=iv,
                greeks_engine=greeks_engine,
            )

            if target_option_price is None:
                all_target_prices_available = False
                continue

            target_total += (
                (target_option_price - lot.entry_price)
                * lot.open_quantity
                * lot.multiplier
            )

        expiry_curve.append(round(expiry_total, 2))

        if all_target_prices_available:
            target_curve.append(round(target_total, 2))
        else:
            # Do not show a false “today” curve.
            target_curve.append(round(expiry_total, 2))

    breakevens = find_zero_crossings(
        grid,
        expiry_curve,
    )

    return PayoffCurves(
        spot_grid=[round(float(value), 2) for value in grid],
        expiry_pnl=expiry_curve,
        target_pnl=target_curve,
        breakevens_expiry=breakevens,
        max_profit=round(max(expiry_curve), 2),
        max_loss=round(min(expiry_curve), 2),
        current_spot_pnl_expiry=round(
            float(
                np.interp(
                    current_spot,
                    grid,
                    expiry_curve,
                )
            ),
            2,
        ),
        current_spot_pnl_target=round(
            float(
                np.interp(
                    current_spot,
                    grid,
                    target_curve,
                )
            ),
            2,
        ),
        grid_min=grid_min,
        grid_max=grid_max,
    )


def find_zero_crossings(
    spot_grid: List[float],
    pnl_curve: List[float],
) -> List[float]:
    breakevens: List[float] = []

    for index in range(1, len(pnl_curve)):
        x_0 = spot_grid[index - 1]
        x_1 = spot_grid[index]

        y_0 = pnl_curve[index - 1]
        y_1 = pnl_curve[index]

        if y_0 == 0:
            breakevens.append(round(x_0, 2))
            continue

        if (y_0 < 0 < y_1) or (y_0 > 0 > y_1):
            x_cross = x_0 + (
                (0 - y_0)
                * (x_1 - x_0)
                / (y_1 - y_0)
            )

            breakevens.append(
                round(x_cross, 2)
            )

    return sorted(
        set(breakevens)
    )