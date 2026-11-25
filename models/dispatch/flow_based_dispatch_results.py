"""Result serialization and diagnostics for the single-MTU flow-based LP.

Kept outside the optimization model so the formulation remains readable and
all reported price, balance and CNEC calculations have one clear home.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from models.dispatch.lp_common import marginal_price_eur_mwh
from models.grid.cnec_deanonymizer import deanonymize_cnec
from models.dispatch.jao_market_constraints import jao_net_position_residuals
from models.dispatch.jao_market_constraints import price_taking_zones, LOCAL_MODELLED_VIRTUAL_ZONES
from models.dispatch.cnec_constraints import cnec_filter_summary
from services.api.market_inputs import NO_AREAS


def build_single_period_result(
    *,
    problem: Any,
    solver_name: str,
    constraints_data: dict[str, Any],
    market: dict[str, Any],
    generators: list[dict[str, Any]],
    generation: Any,
    shed: Any,
    net_position: Any,
    balance_constraints: list[Any],
    cnec_constraints: list[tuple[dict[str, Any], Any]],
    zones: tuple[str, ...],
    zone_index: dict[str, int],
    demand: dict[str, float],
    costs: list[float],
    lower_bounds: list[float],
    upper_bounds: list[float],
    fixed_generation: dict[str, float],
    delta_t_h: float,
    ram_multiplier: float,
    cnec_rows: list[dict[str, Any]],
    jao_balance_descriptors: list[dict[str, Any]],
    jao_balance_constraints: list[Any],
) -> dict[str, Any]:
    """Validate the solution and assemble user-facing dispatch diagnostics."""
    solution_vectors = (generation.value, shed.value, net_position.value)
    if any(value is None or not np.all(np.isfinite(np.asarray(value, dtype=float))) for value in solution_vectors):
        raise RuntimeError("CVXPY returned missing or non-finite decision variables.")

    marginal_prices: dict[str, float] = {}
    balance_dual_diagnostics: dict[str, dict[str, float]] = {}
    for index, area in enumerate(NO_AREAS):
        # The equality is written as d - shed - generation + NP == 0;
        # with this orientation the CVXPY dual has the marginal-cost sign.
        dual_value = balance_constraints[index].dual_value
        raw_dual = float(dual_value) if dual_value is not None else None
        marginal_price = marginal_price_eur_mwh(raw_dual, delta_t_h)
        marginal_prices[area] = round(marginal_price, 4) if marginal_price is not None else None
        balance_dual_diagnostics[area] = {
            "dual_raw_eur_per_mw_interval": round(raw_dual, 6) if raw_dual is not None else None,
            "marginal_price_eur_mwh": round(marginal_price, 4) if marginal_price is not None else None,
            "delta_t_h": delta_t_h,
        }

    generation_results = []
    generation_by_area = {area: 0.0 for area in NO_AREAS}
    for index, generator in enumerate(generators):
        dispatch_mw = float(generation.value[index])
        generation_by_area[generator["node"]] += dispatch_mw
        generation_results.append({
            "generator_id": generator["id"],
            "name": generator["name"],
            "zone": generator["node"],
            "fuel_type": generator["fuel_type"],
            "dispatch_mw": round(dispatch_mw, 3),
            "available_capacity_mw": round(upper_bounds[index], 3),
            "minimum_mw": round(lower_bounds[index], 3),
            "marginal_cost_eur_mwh": round(costs[index], 3),
            "fixed_observed_output": generator["id"] in fixed_generation,
        })

    binding_cnecs = []
    cnec_diagnostics = []
    raw_cnec_violation = 0.0
    for row, constraint in cnec_constraints:
        ptdf_flow = sum(float(row["ptdf"][zone]) * float(net_position.value[zone_index[zone]]) for zone in zones)
        ram = float(row["ram_mw"]) * float(ram_multiplier)
        dual_value = constraint.dual_value
        raw_dual = float(dual_value) if dual_value is not None else 0.0
        shadow_price = raw_dual / delta_t_h
        slack = ram - ptdf_flow
        raw_cnec_violation = max(raw_cnec_violation, -slack)
        cnec_diagnostic = {
            "constraint_id": row.get("constraint_id"),
            "cnec_name": row.get("cnec_name"),
            "cnec_type": row.get("cnec_type"),
            "significant": row.get("significant"),
            "non_redundant": row.get("non_redundant"),
            "tso": row.get("tso"),
            "ptdf_flow_mw": round(ptdf_flow, 3),
            "ram_mw": round(ram, 3),
            "remaining_margin_mw": round(slack, 3),
            "dual_raw_eur_per_mw_interval": round(raw_dual, 8),
            "shadow_price_eur_per_mw": round(shadow_price, 6),
            "positive_dual": raw_dual > 1e-7,
            "near_limit": slack <= max(1.0, abs(ram) * 1e-4),
        }
        cnec_diagnostics.append(cnec_diagnostic)
        if abs(shadow_price) > 1e-5 or slack <= max(1.0, abs(ram) * 1e-4):
            binding_cnecs.append(deanonymize_cnec({
                "constraint_id": row.get("constraint_id"),
                "cnec_name": row.get("cnec_name"),
                "cne_name": row.get("cne_name"),
                "cnec_type": row.get("cnec_type"),
                "significant": row.get("significant"),
                "non_redundant": row.get("non_redundant"),
                "tso": row.get("tso"),
                "flow_contribution_mw": round(ptdf_flow, 3),
                "ram_mw": round(ram, 3),
                "remaining_margin_mw": round(slack, 3),
                "shadow_price_eur_per_mw": round(shadow_price, 6),
            }))

    positive_dual_cnecs = [row for row in cnec_diagnostics if row["positive_dual"]]
    closest_cnecs = sorted(
        cnec_diagnostics,
        key=lambda row: row["remaining_margin_mw"],
    )[:10]

    raw_net_positions = {
        zone: float(net_position.value[index]) for index, zone in enumerate(zones)
    }
    jao_balance_diagnostics = jao_net_position_residuals(raw_net_positions)
    jao_balance_duals = []
    for descriptor, constraint in zip(jao_balance_descriptors, jao_balance_constraints):
        dual = constraint.dual_value
        raw_dual = float(dual) if dual is not None else None
        jao_balance_duals.append({
            **descriptor,
            "dual_raw_eur_per_mw_interval": round(raw_dual, 8) if raw_dual is not None else None,
            "hourly_equivalent_shadow_value_eur_per_mw": (
                round(raw_dual / delta_t_h, 6) if raw_dual is not None else None
            ),
        })
    net_positions = {zone: round(value, 3) for zone, value in raw_net_positions.items()}
    external_zones = list(price_taking_zones(zones))
    net_position_diagnostics = {
        "all_zones_mw": net_positions,
        "norwegian_zones_mw": {zone: net_positions[zone] for zone in NO_AREAS},
        "price_taking_zones_mw": {zone: net_positions[zone] for zone in external_zones},
        "local_modelled_virtual_zones_mw": {
            zone: net_positions[zone] for zone in LOCAL_MODELLED_VIRTUAL_ZONES if zone in net_positions
        },
        "norwegian_net_position_sum_mw": round(sum(net_positions[zone] for zone in NO_AREAS), 6),
        "external_net_position_sum_mw": round(sum(net_positions[zone] for zone in external_zones), 6),
        "system_net_position_sum_mw": round(sum(raw_net_positions.values()), 8),
        **jao_balance_diagnostics,
        "zone_count": len(zones),
        "zone_set": list(zones),
    }
    load_shedding = {area: round(float(shed.value[index]), 3) for index, area in enumerate(NO_AREAS)}
    # Validate using unrounded CVXPY values. Rounding MW values before the
    # residual check can manufacture a false kW-level infeasibility.
    area_balance_residuals = {
        area: round(
            generation_by_area[area]
            + float(shed.value[index])
            - demand[area]
            - raw_net_positions[area],
            8,
        )
        for index, area in enumerate(NO_AREAS)
    }
    market_observations = market.get("price_observations", {})
    observed_no_prices = {
        area: float(market_observations[area]["price_eur_mwh"])
        for area in NO_AREAS if area in market_observations
    }
    price_residuals = {
        area: {
            "observed_eur_mwh": round(observed_no_prices[area], 4),
            "modeled_eur_mwh": marginal_prices[area],
            "modeled_minus_observed_eur_mwh": (
                round(marginal_prices[area] - observed_no_prices[area], 4)
                if marginal_prices[area] is not None else None
            ),
        }
        for area in observed_no_prices
    }
    marginal_generators_by_area: dict[str, list[dict[str, Any]]] = {area: [] for area in NO_AREAS}
    for index, generator in enumerate(generators):
        dispatch_mw = float(generation.value[index])
        lower_mw = lower_bounds[index]
        upper_mw = upper_bounds[index]
        tolerance_mw = max(0.1, 1e-4 * max(upper_mw, 1.0))
        is_at_min = dispatch_mw <= lower_mw + tolerance_mw
        is_at_max = dispatch_mw >= upper_mw - tolerance_mw
        if not is_at_min and not is_at_max:
            marginal_generators_by_area[generator["node"]].append({
                "generator_id": generator["id"],
                "fuel_type": generator["fuel_type"],
                "dispatch_mw": round(dispatch_mw, 3),
                "minimum_mw": round(lower_mw, 3),
                "available_capacity_mw": round(upper_mw, 3),
                "marginal_cost_eur_mwh": round(costs[index], 3),
                "fixed_observed_output": generator["id"] in fixed_generation,
                "headroom_mw": round(upper_mw - dispatch_mw, 3),
            })

    diagnostics = {
        "balance_duals": balance_dual_diagnostics,
        "positive_dual_cnecs": positive_dual_cnecs,
        "closest_cnecs": closest_cnecs,
        "net_positions": net_position_diagnostics,
        "interior_generators_by_area": marginal_generators_by_area,
        "price_residuals": price_residuals,
        "interpretation_note": (
            "Interior dispatch identifies candidate marginal resources only; "
            "ties, degeneracy, boundary prices, and system constraints can also set dual prices."
        ),
    }
    missing_wind_forecast_areas = market.get("missing_wind_availability_areas") or market.get(
        "missing_renewable_forecast_areas", []
    )
    input_warnings = list(market.get("forecast_origin_warnings", []))
    if missing_wind_forecast_areas:
        input_warnings.append(
            "Vindinput mangler for " + ", ".join(missing_wind_forecast_areas)
            + "; solver skal blokkeres i readiness-gaten, aldri bruke installert kapasitet som forecast."
        )
    proxy_boundary_zones = [
        zone for zone, item in market.get("boundary_price_inputs", {}).items()
        if item.get("used_declared_proxy")
    ]
    if proxy_boundary_zones:
        input_warnings.append(
            "A44-primary manglet; eksplisitt proxypris brukt for JAO-soner: "
            + ", ".join(proxy_boundary_zones)
            + ". Se boundary_price_inputs for begge prisområdene."
        )

    max_balance_residual = max((abs(value) for value in area_balance_residuals.values()), default=0.0)
    max_system_balance_residual = abs(sum(raw_net_positions.values()))
    max_jao_balance_residual = jao_balance_diagnostics["max_jao_net_position_residual_mw"]
    max_cnec_violation = max(0.0, raw_cnec_violation)
    # HiGHS/CVXPY can leave a few kW of residual on a multi-GW LP. Use an
    # explicit 10 kW MW tolerance; the old 1 kW threshold mislabelled valid
    # optimal solutions as infeasible. JAO group/pair balances are checked too.
    feasibility_tolerance = 0.01
    finite_prices = all(
        value is not None and math.isfinite(value)
        for value in marginal_prices.values()
    )
    residuals_feasible = finite_prices and max(
        max_balance_residual,
        max_system_balance_residual,
        max_jao_balance_residual,
        max_cnec_violation,
    ) <= feasibility_tolerance
    return {
        "status": problem.status,
        "feasible": residuals_feasible,
        "feasibility_status": "VALID" if residuals_feasible else "NUMERICAL_VIOLATION",
        "result_kind": "MODELLED_SINGLE_PERIOD_NORDIC_FLOW_BASED_DISPATCH",
        "model_scope": "NORWAY_DISPATCH_WITH_PRICE_TAKING_EXTERNAL_BOUNDARIES",
        "solver": solver_name,
        "objective_eur": round(float(problem.value), 4),
        "currency_unit": "EUR/MWh",
        "power_unit": "MW",
        "delta_t_h": delta_t_h,
        "delivery_start_utc": constraints_data.get("delivery_start_utc"),
        "delivery_end_utc": constraints_data.get("delivery_end_utc"),
        "input_status": market.get("status"),
        "forecast_origin_usable_at_mtu_start": market.get("forecast_origin_usable_at_mtu_start", False),
        "forecast_origin_warnings": market.get("forecast_origin_warnings", []),
        "load_policy": market.get("load_policy"),
        "load_inputs": market.get("load_inputs", {}),
        "renewable_forecast_mw": market.get("renewable_forecast_mw", {}),
        "renewable_forecast_inputs": market.get("renewable_forecast_inputs", {}),
        "missing_renewable_forecast_areas": missing_wind_forecast_areas,
        "wind_availability_policy": "A75_A16_REALIZED_FIXED_IN_REPLAY_OR_A69_A01_MW_AVAILABILITY_CEILING; OTHERWISE_BLOCK",
        "observed_generation_by_type": market.get("observed_generation_by_type", {}),
        "generation_diagnostics_included": market.get("generation_diagnostics_included", False),
        "boundary_price_inputs": market.get("boundary_price_inputs", {}),
        "boundary_price_area": market.get("boundary_price_area", {}),
        "observed_prices_eur_mwh": observed_no_prices,
        "demand_mw": {area: round(value, 3) for area, value in demand.items()},
        "generation_by_area_mw": {area: round(value, 3) for area, value in generation_by_area.items()},
        "generation": generation_results,
        "net_positions_mw": net_positions,
        "load_shedding_mw": load_shedding,
        "area_balance_residuals_mw": area_balance_residuals,
        "nodal_marginal_prices": marginal_prices,
        "diagnostics": {
            **diagnostics,
            "max_area_balance_residual_mw": round(max_balance_residual, 8),
            "max_system_balance_residual_mw": round(max_system_balance_residual, 8),
            "max_jao_net_position_residual_mw": round(max_jao_balance_residual, 8),
            "jao_balance_constraints": jao_balance_duals,
            "max_cnec_violation_mw": round(max_cnec_violation, 8),
            "feasibility_tolerance_mw": feasibility_tolerance,
        },
        "binding_cnecs": binding_cnecs,
        "constraint_count": len(cnec_rows),
        "cnec_filter_summary": cnec_filter_summary(constraints_data.get("constraints", [])),
        "raw_jao_row_count": len(constraints_data.get("constraints", [])),
        "non_market_or_redundant_rows_excluded": max(0, len(constraints_data.get("constraints", [])) - len(cnec_rows)),
        "cnec_domain_zones": list(zones),
        "sum_net_positions_mw": round(sum(net_positions.values()), 6),
        "total_load_shedding_mw": round(sum(load_shedding.values()), 3),
        "demand_provenance": "ENTSOE_A65_A16_PREFERRED_THEN_PUBLISHED_A01; NO_FIXED_LOAD_FALLBACK",
        "boundary_policy": "ENTSOE_A44_PRICE_TAKER_WITH_JAO_VIRTUAL_ZONE_CNEC_LIMITS",
        "warnings": [
            "Eksterne områder er prisbaserte randsoner, ikke full nordisk budkurve/dispatch.",
            "Generatorpark og marginalkostnader er konfigurerte/estimerte aggregeringer, ikke komplette publiserte budkurver.",
            "A75 A16 realisert vind brukes som fast injeksjon i replay; A69 A01 brukes som tilgjengelighetsgrense i forecast. Ingen nameplate-fallback.",
            *input_warnings,
        ],
        "source_errors": market.get("source_errors", {}),
    }
