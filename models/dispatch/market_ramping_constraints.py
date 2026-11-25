"""15-minute day-ahead interconnector ramping limits for Approach B.

The static values are taken from the April 2026 SDAC overview of TSO
allocation constraints. They are expressed as maximum MW change between
adjacent 15-minute MTUs. The model applies them to the JAO virtual-zone net
position representing the associated interconnector flow.

SwePol (SE4_SP) is deliberately omitted: its published 300 MW value is marked
with QPRE, whose active dates must be read from a market message. Without that
activation state, applying the QPRE schedule (or assuming it is inactive)
would be an unsupported historical assumption.
"""
from __future__ import annotations

from typing import Any

# link name -> (JAO flow zone, maximum change MW per 15-minute MTU)
# For internal Nordic links either endpoint is valid because the JAO pair
# equality makes the two endpoint positions equal in magnitude and opposite
# in sign. We consistently use the endpoint located in the Nordic area.
INDIVIDUAL_LINK_RAMP_LIMITS_MW = {
    "COBRA": ("DK1_CO", 700.0),
    "KONTI_SKAN": ("SE3_KS", 300.0),
    "NORNED": ("NO2_ND", 309.0),
    "BALTIC_CABLE": ("SE4_BC", 300.0),
    "NORDLINK": ("NO2_NK", 619.0),
    "ESTLINK": ("FI_EL", 300.0),
    "KONTEK": ("DK2_KO", 300.0),
    "FENNO_SKAN": ("SE3_FS", 600.0),
    "NORDBALT": ("SE4_NB", 300.0),
    "SKAGERRAK": ("NO2_SK", 225.0),
    "STOREBAELT": ("DK2_SB", 300.0),
}

# The SDAC overview separately lists a 450 MW/15-minute ramp limit for the
# combined NO2-NL + NO2-DE line set. Both virtual-zone positions are oriented
# from their Norwegian access nodes, so their sum is the modeled line-set flow.
LINE_SET_RAMP_LIMITS_MW = {
    "NO2A_NORNED_PLUS_NORDLINK": (("NO2_ND", "NO2_NK"), 450.0),
}

# The 2026 SDAC overview lists SE4_SP / SwePol at 300 MW with a QPRE marker.
# JAO states QPRE activation is date-specific and announced in a market
# message. Until that message is ingested, leave it out rather than guess.
QPRE_MANAGED_LINKS = {"SWEPOL": "SE4_SP"}


def add_market_ramping_constraints(
    net_position: Any,
    zones: tuple[str, ...] | list[str],
    interval_count: int,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Build adjacent-MTU ramp constraints for each modeled cable/line set.

    The first MTU in a solved block has no preceding-position boundary value;
    constraints therefore begin at t=1. Approach B only solves contiguous
    blocks, so no ramp is inferred across a missing-input gap.
    """
    if interval_count < 2:
        return [], []
    zone_index = {zone: index for index, zone in enumerate(zones)}
    constraints: list[Any] = []
    descriptors: list[dict[str, Any]] = []

    for link, (zone, limit_mw) in INDIVIDUAL_LINK_RAMP_LIMITS_MW.items():
        if zone not in zone_index:
            raise ValueError(f"JAO domain is missing ramping zone {zone} for {link}.")
        index = zone_index[zone]
        for t in range(1, interval_count):
            constraints.append(net_position[index, t] - net_position[index, t - 1] <= limit_mw)
            constraints.append(net_position[index, t - 1] - net_position[index, t] <= limit_mw)
        descriptors.append({
            "link": link,
            "zones": [zone],
            "limit_mw_per_15min": limit_mw,
            "constraint_count": 2 * (interval_count - 1),
            "source": "SDAC_ALLOCATION_CONSTRAINTS_APRIL_2026",
        })

    for line_set, (line_zones, limit_mw) in LINE_SET_RAMP_LIMITS_MW.items():
        missing = [zone for zone in line_zones if zone not in zone_index]
        if missing:
            raise ValueError(f"JAO domain is missing ramping zones {missing} for {line_set}.")
        indices = [zone_index[zone] for zone in line_zones]
        for t in range(1, interval_count):
            current_flow = sum(net_position[index, t] for index in indices)
            previous_flow = sum(net_position[index, t - 1] for index in indices)
            constraints.append(current_flow - previous_flow <= limit_mw)
            constraints.append(previous_flow - current_flow <= limit_mw)
        descriptors.append({
            "link_set": line_set,
            "zones": list(line_zones),
            "limit_mw_per_15min": limit_mw,
            "constraint_count": 2 * (interval_count - 1),
            "source": "SDAC_ALLOCATION_CONSTRAINTS_APRIL_2026",
        })

    return constraints, descriptors


def market_ramping_diagnostics(
    net_positions_by_mtu: list[dict[str, float]],
    periods_utc: list[str],
) -> dict[str, Any]:
    """Report observed adjacent-MTU ramp changes and remaining margins."""
    if len(net_positions_by_mtu) != len(periods_utc):
        raise ValueError("Net-position and period arrays must have equal length.")

    by_mtu: list[dict[str, Any]] = []
    max_violation = 0.0
    binding: list[dict[str, Any]] = []

    def append_ramp(name: str, zones: tuple[str, ...], limit: float, t: int) -> None:
        nonlocal max_violation
        previous = sum(float(net_positions_by_mtu[t - 1][zone]) for zone in zones)
        current = sum(float(net_positions_by_mtu[t][zone]) for zone in zones)
        change = current - previous
        margin = limit - abs(change)
        violation = max(0.0, -margin)
        max_violation = max(max_violation, violation)
        item = {
            "link": name,
            "zones": list(zones),
            "previous_flow_mw": round(previous, 4),
            "current_flow_mw": round(current, 4),
            "change_mw": round(change, 4),
            "limit_mw_per_15min": limit,
            "remaining_ramp_margin_mw": round(margin, 4),
            "binding": margin <= 1.0,
            "violation_mw": round(violation, 8),
        }
        by_mtu[t].append(item)
        if item["binding"]:
            binding.append({"delivery_start_utc": periods_utc[t], **item})

    for _ in periods_utc:
        by_mtu.append([])
    for t in range(1, len(periods_utc)):
        for link, (zone, limit) in INDIVIDUAL_LINK_RAMP_LIMITS_MW.items():
            append_ramp(link, (zone,), limit, t)
        for line_set, (zones, limit) in LINE_SET_RAMP_LIMITS_MW.items():
            append_ramp(line_set, zones, limit, t)

    return {
        "by_mtu": [
            {"delivery_start_utc": periods_utc[t], "ramp_constraints": by_mtu[t]}
            for t in range(len(periods_utc))
        ],
        "binding_ramps": binding,
        "binding_ramp_count": len(binding),
        "max_ramp_violation_mw": round(max_violation, 8),
        "first_mtu_has_no_pre_horizon_ramp_boundary": True,
        "qpre_managed_links_not_applied_pending_activation_data": dict(QPRE_MANAGED_LINKS),
    }
