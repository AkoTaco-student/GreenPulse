"""One-MTU zonal dispatch constrained by the published Nordic JAO domain.

This reduced model uses ENTSO-E load observations/forecasts and price-taking
external boundary zones. It is not a full Nordic market-coupling recreation.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import cvxpy as cp
import numpy as np
import yaml

from models.dispatch.lp_common import marginal_price_eur_mwh
from models.dispatch.cnec_constraints import build_cnec_matrix, market_cnec_rows, cnec_filter_summary
from models.dispatch.jao_market_constraints import (
    add_jao_net_position_constraints, validate_jao_zone_set, price_taking_zones,
)
from models.dispatch.flow_based_dispatch_results import build_single_period_result
from services.api.market_inputs import NO_AREAS


class FlowBasedDispatchLP:
    """Solve a single 15-minute dispatch with JAO PTDF/RAM constraints."""

    def __init__(self, assets_config_path: Path | None = None):
        project_root = Path(__file__).resolve().parents[2]
        config_path = assets_config_path or project_root / "config" / "assets.yaml"
        with config_path.open("r", encoding="utf-8") as source:
            self.assets = yaml.safe_load(source)
        self.generators = self.assets["generators"]
        self.voll_eur_mwh = float(self.assets.get("parameters", {}).get("load_shed_penalty_eur", 3000.0))

    @staticmethod
    def _nodal_marginal_price(raw_dual: Any, delta_t_h: float) -> float | None:
        """Compatibility wrapper around the shared dual-to-price conversion."""
        return marginal_price_eur_mwh(raw_dual, delta_t_h)

    def solve(
        self,
        constraints_data: dict[str, Any],
        market_inputs: dict[str, Any],
        demand_multipliers: dict[str, float] | None = None,
        hydro_cost_shifts: dict[str, float] | None = None,
        generator_availability: dict[str, float] | None = None,
        generator_available_capacity_mw: dict[str, float] | None = None,
        wind_multiplier: float = 1.0,
        ram_multiplier: float = 1.0,
        solver: str = "HIGHS",
    ) -> dict[str, Any]:
        """Solve one MTU using live time-aligned market inputs; never nominal loads."""
        zones = validate_jao_zone_set(
            tuple(dict.fromkeys([*NO_AREAS, *constraints_data.get("zones", [])]))
        )
        cnec_rows = market_cnec_rows(constraints_data.get("constraints", []))
        if not cnec_rows:
            raise ValueError("JAO-domenet inneholder ingen aktive market-coupling CNEC-rader.")

        market = market_inputs
        if market.get("status") != "READY":
            raise ValueError(
                "Flow-based dispatch readiness gate blocked solve: "
                f"missing_load={market.get('missing_load_areas')}, "
                f"missing_wind={market.get('missing_wind_availability_areas') or market.get('missing_renewable_forecast_areas')}, "
                f"missing_boundary_prices={market.get('missing_boundary_price_zones')}"
            )

        demand_mult = demand_multipliers or {}
        hydro_shift = hydro_cost_shifts or {}
        availability = generator_availability or {}
        available_capacity = generator_available_capacity_mw or {}
        fixed_generation = market.get("fixed_generation_mw_by_generator", {})
        if not 0.0 <= float(wind_multiplier) <= 1.0:
            raise ValueError("wind_multiplier må være mellom 0 og 1.")
        if not math.isfinite(float(ram_multiplier)) or float(ram_multiplier) <= 0.0:
            raise ValueError("ram_multiplier må være et positivt endelig tall.")

        demand: dict[str, float] = {}
        for area in NO_AREAS:
            if area not in market.get("demand_mw", {}):
                raise ValueError(f"Mangler validert ENTSO-E-last for {area}; fast MW-fallback er deaktivert.")
            multiplier = float(demand_mult.get(area, 1.0))
            if not math.isfinite(multiplier) or multiplier < 0.0:
                raise ValueError(f"Ugyldig lastscenario for {area}.")
            demand[area] = float(market["demand_mw"][area]) * multiplier

        boundary_prices = market.get("boundary_prices_eur_mwh", {})
        if not boundary_prices:
            raise ValueError("Mangler ENTSO-E A44-priser for JAO-eksterne/virtuelle randsoner.")
        external_zones = set(price_taking_zones(zones))
        missing_boundary_prices = sorted(external_zones - set(boundary_prices))
        if missing_boundary_prices:
            raise ValueError(
                "Missing external A44 boundary prices for JAO zones: "
                + ", ".join(missing_boundary_prices)
                + ". Unpriced external net positions are not allowed."
            )
        for zone, price in boundary_prices.items():
            if zone not in external_zones or not math.isfinite(float(price)):
                raise ValueError(f"Ugyldig prisbasert randbetingelse for {zone}.")

        generator_ids = {str(generator["id"]) for generator in self.generators}
        for generator_id, value in availability.items():
            if generator_id not in generator_ids or not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"Ugyldig tilgjengelighetsfaktor for generator {generator_id}.")
        for generator_id, value in available_capacity.items():
            if generator_id not in generator_ids or not math.isfinite(float(value)) or float(value) < 0.0:
                raise ValueError(f"Ugyldig MW-kapasitet for generator {generator_id}.")

        generators = [generator for generator in self.generators if generator["node"] in NO_AREAS]
        n_generators = len(generators)
        n_zones = len(zones)
        zone_index = {zone: index for index, zone in enumerate(zones)}
        generation = cp.Variable(n_generators, nonneg=True, name="generation_mw")
        shed = cp.Variable(len(NO_AREAS), nonneg=True, name="load_shed_mw")
        net_position = cp.Variable(n_zones, name="net_position_mw")
        constraints: list[cp.Constraint] = []
        # Load shedding cannot exceed the demand in the corresponding area.
        constraints.append(shed <= np.asarray([demand[area] for area in NO_AREAS], dtype=float))

        costs: list[float] = []
        upper_bounds: list[float] = []
        lower_bounds: list[float] = []
        for index, generator in enumerate(generators):
            generator_id = str(generator["id"])
            area = str(generator["node"])
            cost = float(generator["marginal_cost_eur"])
            if "hydro" in str(generator["fuel_type"]).lower():
                cost += float(hydro_shift.get(area, hydro_shift.get("ALL", 0.0)))
            factor = float(availability.get(generator_id, 1.0))
            if not math.isfinite(cost) or not math.isfinite(factor) or not 0.0 <= factor <= 1.0:
                raise ValueError(f"Ugyldig kostnad eller tilgjengelighet for {generator_id}.")
            is_wind = str(generator["fuel_type"]).lower() == "wind"
            if is_wind:
                factor *= float(wind_multiplier)
                if generator_id not in available_capacity and generator_id not in fixed_generation:
                    raise ValueError(
                        f"Missing A69 availability or A75 realized output for {generator_id}; "
                        "configured nameplate capacity is not a valid wind forecast."
                    )
            upper = float(generator["capacity_mw"]) * factor
            if generator_id in available_capacity:
                # The MTU-aligned A69 forecast is an MW ceiling. Do not divide
                # by an unverified installed-capacity estimate to invent a
                # percentage availability factor.
                upper = float(available_capacity[generator_id]) * factor
            lower = float(generator["min_mw"]) * factor
            if is_wind and generator_id in fixed_generation:
                observed_mw = float(fixed_generation[generator_id]) * float(wind_multiplier)
                if not math.isfinite(observed_mw) or observed_mw < 0.0:
                    raise ValueError(f"Invalid observed wind output for {generator_id}.")
                upper = lower = observed_mw
            if not all(math.isfinite(value) and value >= 0.0 for value in (upper, lower)):
                raise ValueError(f"Ugyldig kapasitetsgrense for {generator_id}.")
            if lower > upper + 1e-8:
                raise ValueError(f"Minsteproduksjon overstiger tilgjengelig kapasitet for {generator_id}.")
            costs.append(cost)
            lower_bounds.append(lower)
            upper_bounds.append(upper)

        gen_min_constraint = generation >= np.asarray(lower_bounds)
        gen_max_constraint = generation <= np.asarray(upper_bounds)
        constraints.extend([gen_min_constraint, gen_max_constraint])

        balance_constraints: list[cp.Constraint] = []
        for index, area in enumerate(NO_AREAS):
            local_gen_indices = [i for i, generator in enumerate(generators) if generator["node"] == area]
            local_generation = cp.sum(generation[local_gen_indices]) if local_gen_indices else 0.0
            # NP is export-positive. d - shed - generation + NP == 0.
            balance = demand[area] - shed[index] - local_generation + net_position[zone_index[area]] == 0
            balance_constraints.append(balance)
            constraints.append(balance)

        # JAO balances each synchronous area independently and links the two
        # virtual bidding zones on each internal HVDC interconnector. A single
        # global sum(net_position) == 0 incorrectly lets separate synchronous
        # areas offset one another.
        jao_balance_constraints, jao_balance_descriptors = add_jao_net_position_constraints(
            net_position, zones
        )
        constraints.extend(jao_balance_constraints)

        cnec_constraints: list[tuple[dict[str, Any], cp.Constraint]] = []
        coefficient_matrix, ram_vector = build_cnec_matrix(cnec_rows, zones)
        for row, coefficients, ram in zip(cnec_rows, coefficient_matrix, ram_vector):
            cnec_constraint = coefficients @ net_position <= float(ram) * float(ram_multiplier)
            constraints.append(cnec_constraint)
            cnec_constraints.append((row, cnec_constraint))

        # Every external/virtual zone has a validated A44 price. Price its
        # existing net-position variable directly; separate alias variables
        # and equality constraints only enlarged the LP without adding meaning.
        boundary_cost_terms = [
            float(boundary_prices[zone]) * net_position[zone_index[zone]]
            for zone in sorted(external_zones)
        ]

        delta_t_h = 0.25
        operating_cost = delta_t_h * cp.sum(cp.multiply(np.asarray(costs), generation))
        shedding_cost = delta_t_h * self.voll_eur_mwh * cp.sum(shed)
        boundary_cost = delta_t_h * cp.sum(cp.hstack(boundary_cost_terms)) if boundary_cost_terms else 0.0
        problem = cp.Problem(cp.Minimize(operating_cost + shedding_cost + boundary_cost), constraints)
        solver_name = str(solver).upper()
        installed = cp.installed_solvers()
        solver_enum = getattr(cp, solver_name, None)
        if solver_enum is None or solver_name not in installed:
            if "HIGHS" in installed:
                solver_name, solver_enum = "HIGHS", cp.HIGHS
            else:
                raise RuntimeError(f"Valgt solver {solver} er utilgjengelig; installert={installed}.")
        try:
            problem.solve(solver=solver_enum)
        except Exception as error:
            raise RuntimeError(f"CVXPY {solver_name}-solve feilet: {error}") from error

        if problem.status not in (cp.OPTIMAL, cp.OPTIMAL_INACCURATE):
            infeasible_statuses = {cp.INFEASIBLE, cp.INFEASIBLE_INACCURATE}
            unbounded_statuses = {cp.UNBOUNDED, cp.UNBOUNDED_INACCURATE}
            if problem.status in infeasible_statuses:
                feasibility_status, feasible = "SOLVER_INFEASIBLE", False
            elif problem.status in unbounded_statuses:
                feasibility_status, feasible = "SOLVER_UNBOUNDED", None
            else:
                feasibility_status, feasible = "SOLVER_NOT_OPTIMAL", None
            return {
                "status": str(problem.status),
                "feasible": feasible,
                "feasibility_status": feasibility_status,
                "result_kind": "MODELLED_SINGLE_PERIOD_NORDIC_FLOW_BASED_DISPATCH",
                "solver": solver_name,
                "delivery_start_utc": constraints_data.get("delivery_start_utc"),
                "input_status": market.get("status"),
                "message": f"CVXPY status: {problem.status}",
            }
        return build_single_period_result(
            problem=problem,
            solver_name=solver_name,
            constraints_data=constraints_data,
            market=market,
            generators=generators,
            generation=generation,
            shed=shed,
            net_position=net_position,
            balance_constraints=balance_constraints,
            cnec_constraints=cnec_constraints,
            zones=zones,
            zone_index=zone_index,
            demand=demand,
            costs=costs,
            lower_bounds=lower_bounds,
            upper_bounds=upper_bounds,
            fixed_generation=fixed_generation,
            delta_t_h=delta_t_h,
            ram_multiplier=ram_multiplier,
            cnec_rows=cnec_rows,
            jao_balance_descriptors=jao_balance_descriptors,
            jao_balance_constraints=jao_balance_constraints,
        )
