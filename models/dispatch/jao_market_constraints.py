"""JAO Nordic net-position balance rules for the flow-based market LP.

The JAO Nordic flow-based domain contains zones in the Nordic and Central
European synchronous areas, plus virtual bidding zones for HVDC endpoints.
JAO requires (1) each synchronous area to have a zero-sum net position and
(2) the two virtual zones at the ends of each internal Nordic-CCR HVDC link
to be coupled. A single global sum(NP) == 0 does not express these rules.

The internal-link pair equations below use a lossless approximation. JAO
publishes implicit loss factors for some links; applying those correctly is
direction-dependent and requires a piecewise/MILP formulation. We deliberately
do not pretend that a fixed signed equality captures those losses.
"""
from __future__ import annotations

from typing import Any

# JAO Nordic Publication Tool Handbook v1.8.1, Table 1.
NORDIC_SYNCHRONOUS_AREA_ZONES = (
    "DK2", "FI", "NO1", "NO2", "NO3", "NO4", "NO5",
    "SE1", "SE2", "SE3", "SE4",
    "NO2_SK", "DK2_SB", "SE3_FS", "SE3_KS", "FI_FS",
    "SE3_SWL", "SE4_SWL",
    "SE4_SP", "SE4_NB", "SE4_BC", "FI_EL", "DK2_KO",
    "NO2_ND", "NO2_NK",
)

CENTRAL_EUROPE_SYNCHRONOUS_AREA_ZONES = (
    "DK1", "DK1_SK", "DK1_SB", "DK1_KS", "DK1_DE", "DK1_CO",
)

# The opposite virtual bidding zones at both ends of internal HVDC links.
# Each pair sum is zero in the lossless LP approximation.
INTERNAL_HVDC_VIRTUAL_ZONE_PAIRS = (
    ("NO2_SK", "DK1_SK"),    # Skagerrak
    ("DK1_SB", "DK2_SB"),    # Storebælt
    ("DK1_KS", "SE3_KS"),    # Konti-Skan
    ("SE3_FS", "FI_FS"),    # Fenno-Skan
    ("SE3_SWL", "SE4_SWL"),  # SouthWestLink
)

# Published implicit loss factors (JAO Handbook v1.8.1, Appendix, p. 25).
# These are reported for transparency, not applied by this linear lossless model.
INTERNAL_HVDC_PUBLISHED_ILF = {
    "SKAGERRAK": 0.029,
    "KONTI_SKAN": 0.025,
    "STOREBAELT": 0.018,
}

# In the reduced dispatch, NO2 is modelled endogenously. Therefore the NO2 end
# of Skagerrak must not be valued using the observed NO2 A44 price. Other
# external real/virtual endpoints are valued using their own market-area A44.
LOCAL_MODELLED_VIRTUAL_ZONES = frozenset({"NO2_SK"})

# These two virtual zones are non-market borders and are not part of the
# day-ahead flow-based domain used by market coupling.
NON_MARKET_VIRTUAL_ZONES = frozenset({"NO2_NSL", "DK1_VL"})

SYNCHRONOUS_AREA_GROUPS = {
    "NORDIC": NORDIC_SYNCHRONOUS_AREA_ZONES,
    "CENTRAL_EUROPE": CENTRAL_EUROPE_SYNCHRONOUS_AREA_ZONES,
}
SUPPORTED_MARKET_ZONES = frozenset(
    NORDIC_SYNCHRONOUS_AREA_ZONES + CENTRAL_EUROPE_SYNCHRONOUS_AREA_ZONES
)


def price_taking_zones(zones: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return external positions valued from A44, excluding endogenous NO2_SK."""
    norwegian = {"NO1", "NO2", "NO3", "NO4", "NO5"}
    return tuple(
        zone for zone in zones
        if zone not in norwegian and zone not in LOCAL_MODELLED_VIRTUAL_ZONES
    )


def validate_jao_zone_set(zones: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate the complete JAO market topology; never silently zero-fill zones."""
    ordered = tuple(dict.fromkeys(str(zone).strip() for zone in zones if str(zone).strip()))
    actual = set(ordered)
    unexpected = sorted(actual - SUPPORTED_MARKET_ZONES)
    missing = sorted(SUPPORTED_MARKET_ZONES - actual)
    if unexpected or missing:
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unexpected:
            details.append(f"unsupported={unexpected}")
        raise ValueError(
            "JAO FB zone topology differs from the configured Nordic handbook map ("
            + "; ".join(details)
            + "). Review the current JAO publication handbook before changing the map."
        )
    return ordered


def add_jao_net_position_constraints(
    net_position: Any,
    zones: tuple[str, ...] | list[str],
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Build JAO synchronous-area and internal-HVDC pair equalities for one MTU."""
    ordered = validate_jao_zone_set(zones)
    zone_index = {zone: index for index, zone in enumerate(ordered)}
    constraints: list[Any] = []
    descriptors: list[dict[str, Any]] = []

    for group_name, group_zones in SYNCHRONOUS_AREA_GROUPS.items():
        indices = [zone_index[zone] for zone in group_zones]
        constraints.append(sum(net_position[index] for index in indices) == 0.0)
        descriptors.append({
            "name": f"SYNC_{group_name}",
            "kind": "SYNCHRONOUS_AREA_NET_POSITION",
            "zones": list(group_zones),
        })

    for endpoint_a, endpoint_b in INTERNAL_HVDC_VIRTUAL_ZONE_PAIRS:
        constraints.append(
            net_position[zone_index[endpoint_a]] + net_position[zone_index[endpoint_b]] == 0.0
        )
        link_name = next(
            name for name, pair in {
                "SKAGERRAK": ("NO2_SK", "DK1_SK"),
                "STOREBAELT": ("DK1_SB", "DK2_SB"),
                "KONTI_SKAN": ("DK1_KS", "SE3_KS"),
                "FENNO_SKAN": ("SE3_FS", "FI_FS"),
                "SOUTHWESTLINK": ("SE3_SWL", "SE4_SWL"),
            }.items() if pair == (endpoint_a, endpoint_b)
        )
        descriptors.append({
            "name": f"HVDC_{endpoint_a}_{endpoint_b}",
            "kind": "INTERNAL_HVDC_VIRTUAL_ZONE_PAIR",
            "link": link_name,
            "zones": [endpoint_a, endpoint_b],
            "loss_model": "LOSSLESS_APPROXIMATION",
            "published_ilf_not_applied": INTERNAL_HVDC_PUBLISHED_ILF.get(link_name),
        })

    return constraints, descriptors


def jao_net_position_residuals(net_positions_mw: dict[str, float]) -> dict[str, Any]:
    """Calculate absolute synchronous-area and virtual-pair equality residuals."""
    group_residuals = {
        group_name: float(sum(net_positions_mw[zone] for zone in group_zones))
        for group_name, group_zones in SYNCHRONOUS_AREA_GROUPS.items()
    }
    pair_residuals = {
        f"{endpoint_a}<->{endpoint_b}": float(
            net_positions_mw[endpoint_a] + net_positions_mw[endpoint_b]
        )
        for endpoint_a, endpoint_b in INTERNAL_HVDC_VIRTUAL_ZONE_PAIRS
    }
    all_residuals = [*group_residuals.values(), *pair_residuals.values()]
    return {
        "synchronous_area_residuals_mw": {
            key: round(value, 8) for key, value in group_residuals.items()
        },
        "internal_hvdc_pair_residuals_mw": {
            key: round(value, 8) for key, value in pair_residuals.items()
        },
        "max_jao_net_position_residual_mw": round(
            max((abs(value) for value in all_residuals), default=0.0), 8
        ),
        "loss_model": "LOSSLESS_APPROXIMATION; JAO_DIRECTIONAL_ILF_NOT_APPLIED",
    }
