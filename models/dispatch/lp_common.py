"""Shared unit conversion for the two flow-based dispatch LPs."""

from __future__ import annotations

import math


def marginal_price_eur_mwh(raw_dual: float | None, delta_t_h: float = 0.25) -> float | None:
    """Convert an energy-balance dual into EUR/MWh.

    The LP objective is multiplied by the MTU duration in hours, so the raw
    balance dual is divided by that duration to obtain an hourly marginal price.
    """
    if raw_dual is None:
        return None
    try:
        value = float(raw_dual)
        duration = float(delta_t_h)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not math.isfinite(duration) or duration <= 0.0:
        return None
    price = value / duration
    return price if math.isfinite(price) else None
