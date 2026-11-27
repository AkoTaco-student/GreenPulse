"""Approach B: contiguous multi-period flow-based dispatch LP.

The model is a linear program (not an SOCP). An optional, explicitly labelled
daily usable-energy scenario can couple reservoir generation across a complete
Oslo delivery day. Without that input, the time-indexed LP has no intertemporal
hydro constraint. FORECAST inputs are vintage-checked; REPLAY is explicitly
ex-post and may use realized inputs.
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import cvxpy as cp
import numpy as np
import yaml

from models.dispatch.lp_common import marginal_price_eur_mwh
from models.dispatch.cnec_constraints import build_cnec_matrix, market_cnec_rows, cnec_filter_summary
from models.dispatch.jao_market_constraints import (
    add_jao_net_position_constraints,
    jao_net_position_residuals,
    validate_jao_zone_set,
    price_taking_zones,
)
from models.dispatch.market_ramping_constraints import (
    add_market_ramping_constraints,
    market_ramping_diagnostics,
)
from services.api.market_inputs import NO_AREAS
from services.api.forecast_calendar import delivery_periods


class MultiPeriodFlowBasedDispatchLP:
    """Solve one contiguous MTU block as a linear program."""

    def __init__(self, assets_config_path: Path | None = None):
        project_root = Path(__file__).resolve().parents[2]
        config_path = assets_config_path or project_root / "config" / "assets.yaml"
        with config_path.open("r", encoding="utf-8") as source:
            assets = yaml.safe_load(source)
        self.generators = assets["generators"]
        self.voll_eur_mwh = float(
            assets.get("parameters", {}).get("load_shed_penalty_eur", 3000.0)
        )

    @staticmethod
    def _finite_number(value: Any, label: str, *, minimum: float | None = None) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{label} must be numeric.") from error
        if not math.isfinite(number) or (minimum is not None and number < minimum):
            raise ValueError(f"{label} must be finite and >= {minimum}.")
        return number

    def solve(
        self,
        constraints_by_mtu: list[dict[str, Any]],
        market_inputs_by_mtu: list[dict[str, Any]],
        *,
        daily_hydro_energy_budget_mwh: dict[str, float] | None = None,
        generator_availability_by_mtu: list[dict[str, float]] | None = None,
        solver: str = "MOSEK",
        delta_t_h: float = 0.25,
        feasibility_tolerance: float = 0.01,
        hydro_budget_tolerance_mwh: float = 0.01,
    ) -> dict[str, Any]:
        """Solve the whole horizon.

        An optional labelled scenario budget limits total reservoir-hydro
        generation per zone over this complete Oslo delivery day. It is an
        upper bound in MWh, not an inferred reservoir state or measured volume.
        """
        count = len(constraints_by_mtu)
        if count < 2 or len(market_inputs_by_mtu) != count:
            raise ValueError("Approach B needs matching inputs for at least two MTUs.")
        if not math.isclose(delta_t_h, 0.25, abs_tol=1e-12):
            raise ValueError("This implementation is configured for 15-minute MTUs (0.25 h).")
        availability_series = generator_availability_by_mtu or [{} for _ in range(count)]
        if len(availability_series) != count:
            raise ValueError("generator_availability_by_mtu must contain one mapping per MTU.")

        zones = validate_jao_zone_set(
            tuple(dict.fromkeys([*NO_AREAS, *constraints_by_mtu[0].get("zones", [])]))
        )
        zone_index = {zone: i for i, zone in enumerate(zones)}
        n_zones = len(zones)
        if not all(area in zone_index for area in NO_AREAS):
            raise ValueError("JAO domain is missing one or more Norwegian bidding zones.")

        periods: list[str] = []
        for t, (domain, market) in enumerate(zip(constraints_by_mtu, market_inputs_by_mtu)):
            if market.get("status") != "READY":
                raise ValueError(f"MTU {t} market input status is not READY.")
            replay_mode = market.get("mode") == "REPLAY"
            if not replay_mode and market.get("forecast_origin_usable") is not True:
                raise ValueError(f"MTU {t} contains input not verified at forecast origin.")
            if replay_mode:
                if domain.get("status") != "READY":
                    raise ValueError(f"MTU {t} JAO replay domain is not READY.")
            elif domain.get("forecast_origin_usable") is not True:
                raise ValueError(f"MTU {t} JAO domain is not verified at forecast origin.")
            domain_zones = validate_jao_zone_set(
                tuple(dict.fromkeys([*NO_AREAS, *domain.get("zones", [])]))
            )
            if set(domain_zones) != set(zones):
                raise ValueError("The JAO zone set changes within the day; split the horizon into compatible blocks.")
            start = str(domain.get("delivery_start_utc", market.get("delivery_start_utc", "")))
            if not start:
                raise ValueError(f"MTU {t} has no delivery timestamp.")
            periods.append(start)

        # Verify quarter-hour continuity and chronological order.
        parsed_periods = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in periods]
        for previous, current in zip(parsed_periods, parsed_periods[1:]):
            if current - previous != timedelta(minutes=15):
                raise ValueError("MTUs must be ordered, contiguous 15-minute intervals.")

        generators = [g for g in self.generators if g["node"] in NO_AREAS]
        n_generators = len(generators)
        reservoir_areas = sorted({
            str(g["node"]) for g in generators
            if "hydro" in str(g["fuel_type"]).lower()
            and "run_of_river" not in str(g["fuel_type"]).lower()
        })
        daily_hydro_energy_budget_mwh = daily_hydro_energy_budget_mwh or {}
        unknown_budget_areas = sorted(set(daily_hydro_energy_budget_mwh) - set(reservoir_areas))
        if unknown_budget_areas:
            raise ValueError(f"Daily hydro budget contains non-reservoir or unknown areas: {unknown_budget_areas}.")
        if daily_hydro_energy_budget_mwh and set(daily_hydro_energy_budget_mwh) != set(reservoir_areas):
            raise ValueError("A hydro-coupled B run requires a daily energy budget for every configured reservoir zone.")
        if daily_hydro_energy_budget_mwh:
            local_days = {period.astimezone(ZoneInfo("Europe/Oslo")).date() for period in parsed_periods}
            if len(local_days) != 1:
                raise ValueError("A daily hydro budget cannot span multiple Oslo delivery days.")
            delivery_day = next(iter(local_days))
            expected_periods = delivery_periods(delivery_day)
            if len(parsed_periods) != len(expected_periods) or parsed_periods != expected_periods:
                raise ValueError("A daily hydro budget requires the complete contiguous Oslo delivery day.")
            for area, value in daily_hydro_energy_budget_mwh.items():
                self._finite_number(value, f"daily hydro energy budget {area}", minimum=0.0)
        hydro_coupling_enabled = bool(daily_hydro_energy_budget_mwh)

        model_build_started = time.perf_counter()
        generation = cp.Variable((n_generators, count), nonneg=True, name="generation_mw")
        shed = cp.Variable((len(NO_AREAS), count), nonneg=True, name="load_shed_mw")
        net_position = cp.Variable((n_zones, count), name="net_position_mw")
        constraints: list[cp.Constraint] = []
        balance_constraints: dict[tuple[str, int], cp.Constraint] = {}
        cnec_coefficients_by_mtu: list[np.ndarray] = []
        cnec_ram_by_mtu: list[np.ndarray] = []
        cnec_rows_by_mtu: list[list[dict[str, Any]]] = []
        cnec_constraints_by_mtu: list[cp.Constraint] = []
        jao_balance_descriptors: list[dict[str, Any]] = []
        jao_balance_constraints_by_mtu: list[list[cp.Constraint]] = []
        demand = np.zeros((len(NO_AREAS), count), dtype=float)
        generation_cost = np.zeros((n_generators, count), dtype=float)
        p_min = np.zeros((n_generators, count), dtype=float)
        p_max = np.zeros((n_generators, count), dtype=float)
        boundary_cost_terms = []

        for t, market in enumerate(market_inputs_by_mtu):
            for a, area in enumerate(NO_AREAS):
                if area not in market.get("demand_mw", {}):
                    raise ValueError(f"Missing day-ahead forecast load for {area} at MTU {t}.")
                demand[a, t] = self._finite_number(market["demand_mw"][area], f"load {area} MTU {t}", minimum=0.0)
            constraints.append(shed[:, t] <= demand[:, t])

            forecast_boundary_prices = market.get("boundary_price_forecasts_eur_mwh", {})
            boundary_zones = set(price_taking_zones(zones))
            missing_boundary_prices = sorted(boundary_zones - set(forecast_boundary_prices))
            if missing_boundary_prices:
                raise ValueError(
                    f"Missing forecast boundary prices at MTU {t}: {missing_boundary_prices}. "
                    "Unpriced external net positions are not allowed."
                )
            extra_boundary_prices = sorted(set(forecast_boundary_prices) - boundary_zones)
            if extra_boundary_prices:
                raise ValueError(f"Invalid boundary price zones at MTU {t}: {extra_boundary_prices}.")
            for zone in sorted(boundary_zones):
                price = self._finite_number(
                    forecast_boundary_prices[zone], f"boundary price {zone} MTU {t}"
                )
                boundary_cost_terms.append(price * net_position[zone_index[zone], t])

            wind_caps = market.get("generator_available_capacity_mw", {})
            fixed_generation = market.get("fixed_generation_mw_by_generator", {})
            for g, generator in enumerate(generators):
                generator_id = str(generator["id"])
                area = str(generator["node"])
                raw_availability = availability_series[t].get(generator_id, 1.0)
                factor = self._finite_number(raw_availability, f"availability {generator_id} MTU {t}", minimum=0.0)
                if factor > 1.0:
                    raise ValueError(f"Availability for {generator_id} must be in [0, 1].")
                upper = self._finite_number(generator["capacity_mw"], f"capacity {generator_id}", minimum=0.0) * factor
                if str(generator["fuel_type"]).lower() == "wind":
                    scenario_wind_multiplier = self._finite_number(
                        market.get("scenario_wind_multiplier", 1.0),
                        f"scenario wind multiplier MTU {t}", minimum=0.0,
                    )
                    if scenario_wind_multiplier > 1.0:
                        raise ValueError("scenario wind multiplier must be in [0, 1].")
                    factor *= scenario_wind_multiplier
                    upper = self._finite_number(generator["capacity_mw"], f"capacity {generator_id}", minimum=0.0) * factor
                    if generator_id in wind_caps:
                        upper = self._finite_number(wind_caps[generator_id], f"A69 wind capacity {generator_id} MTU {t}", minimum=0.0) * factor
                    elif generator_id not in fixed_generation:
                        raise ValueError(
                            f"Missing A69 availability or A75 realized output for {generator_id} at MTU {t}; "
                            "configured nameplate capacity is not a valid wind forecast."
                        )
                lower = self._finite_number(generator["min_mw"], f"minimum {generator_id}", minimum=0.0) * factor
                if str(generator["fuel_type"]).lower() == "wind" and generator_id in fixed_generation:
                    observed_mw = self._finite_number(
                        fixed_generation[generator_id], f"observed wind output {generator_id} MTU {t}", minimum=0.0
                    ) * self._finite_number(market.get("scenario_wind_multiplier", 1.0), "scenario wind multiplier", minimum=0.0)
                    if observed_mw > self._finite_number(generator["capacity_mw"], f"capacity {generator_id}", minimum=0.0) + 1e-6:
                        raise ValueError(f"Observed wind generation exceeds configured capacity for {generator_id} at MTU {t}.")
                    lower = upper = observed_mw
                cost = self._finite_number(generator["marginal_cost_eur"], f"cost {generator_id}")
                if "hydro" in str(generator["fuel_type"]).lower():
                    cost += self._finite_number(market.get("hydro_cost_shift_eur_mwh", {}).get(area, 0.0), f"hydro shift {area} MTU {t}")
                if lower > upper + 1e-8:
                    raise ValueError(f"Minimum output exceeds availability for {generator_id} at MTU {t}.")
                p_min[g, t], p_max[g, t], generation_cost[g, t] = lower, upper, cost

            for a, area in enumerate(NO_AREAS):
                local_g = [i for i, generator in enumerate(generators) if generator["node"] == area]
                local_generation = cp.sum(generation[local_g, t]) if local_g else 0.0
                balance = demand[a, t] - shed[a, t] - local_generation + net_position[zone_index[area], t] == 0
                balance_constraints[(area, t)] = balance
                constraints.append(balance)

            # JAO requires separate synchronous-area balances and pairwise
            # virtual-zone coupling for each internal Nordic HVDC link.
            jao_constraints, descriptors = add_jao_net_position_constraints(
                net_position[:, t], zones
            )
            constraints.extend(jao_constraints)
            jao_balance_constraints_by_mtu.append(jao_constraints)
            if not jao_balance_descriptors:
                jao_balance_descriptors = descriptors

            domain_rows = market_cnec_rows(constraints_by_mtu[t].get("constraints", []))
            try:
                coefficient_matrix, ram_vector = build_cnec_matrix(domain_rows, zones)
            except ValueError as error:
                raise ValueError(f"Invalid JAO CNEC matrix at MTU {t}: {error}") from error
            ram_multiplier = self._finite_number(
                market.get("scenario_ram_multiplier", 1.0),
                f"scenario RAM multiplier MTU {t}", minimum=0.01,
            )
            effective_ram = ram_vector * ram_multiplier
            cnec_constraint = coefficient_matrix @ net_position[:, t] <= effective_ram
            constraints.append(cnec_constraint)
            cnec_coefficients_by_mtu.append(coefficient_matrix)
            cnec_ram_by_mtu.append(effective_ram)
            cnec_rows_by_mtu.append(list(domain_rows))
            cnec_constraints_by_mtu.append(cnec_constraint)

        constraints.extend([generation >= p_min, generation <= p_max])

        # SDAC's published 15-minute interconnector ramp limits couple adjacent
        # MTUs. They are applied to the JAO virtual-zone flow positions; no
        # transition is invented before the first MTU in a contiguous block.
        market_ramp_constraints, market_ramp_descriptors = add_market_ramping_constraints(
            net_position, zones, count
        )
        constraints.extend(market_ramp_constraints)

        hydro_generator_indices = {
            area: [
                i for i, generator in enumerate(generators)
                if generator["node"] == area
                and "hydro" in str(generator["fuel_type"]).lower()
                and "run_of_river" not in str(generator["fuel_type"]).lower()
            ]
            for area in reservoir_areas
        }
        hydro_budget_constraints: dict[str, cp.Constraint] = {}
        for area in reservoir_areas if hydro_coupling_enabled else ():
            hydro_g = hydro_generator_indices[area]
            budget_constraint = delta_t_h * cp.sum(generation[hydro_g, :]) <= float(
                daily_hydro_energy_budget_mwh[area]
            )
            constraints.append(budget_constraint)
            hydro_budget_constraints[area] = budget_constraint

        objective = cp.Minimize(
            delta_t_h * cp.sum(cp.multiply(generation_cost, generation))
            + delta_t_h * self.voll_eur_mwh * cp.sum(shed)
            + delta_t_h * cp.sum(cp.hstack(boundary_cost_terms))
        )
        problem = cp.Problem(objective, constraints)
        model_build_seconds = time.perf_counter() - model_build_started
        installed = cp.installed_solvers()
        # Requested solver first; otherwise MOSEK, then HiGHS (listed in requirements.txt).
        solver_name = next(
            (name for name in (str(solver).upper(), "MOSEK", "HIGHS") if name in installed), None
        )
        if solver_name is None:
            raise RuntimeError(f"Requested solver {solver} unavailable; installed={installed}.")
        solver_enum = getattr(cp, solver_name)
        solve_started = time.perf_counter()
        try:
            problem.solve(solver=solver_enum)
        except Exception as error:
            raise RuntimeError(f"CVXPY {solver_name} solve failed: {error}") from error
        solve_wall_seconds = time.perf_counter() - solve_started
        solver_stats = problem.solver_stats
        solver_setup_seconds = getattr(solver_stats, "setup_time", None)
        solver_solve_seconds = getattr(solver_stats, "solve_time", None)
        timings = {
            "model_build_seconds": round(model_build_seconds, 6),
            "cvxpy_solve_wall_seconds": round(solve_wall_seconds, 6),
            "solver_setup_seconds": round(float(solver_setup_seconds), 6) if solver_setup_seconds is not None else None,
            "solver_solve_seconds": round(float(solver_solve_seconds), 6) if solver_solve_seconds is not None else None,
        }

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
                "model_version": "B_MULTI_PERIOD_FLOW_BASED_LP",
                "solver": solver_name,
                "interval_count": count,
                "timings": timings,
                "delivery_start_utc": periods[0],
                "delivery_end_utc": (parsed_periods[-1] + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
            }

        gen_values = np.asarray(generation.value, dtype=float)
        shed_values = np.asarray(shed.value, dtype=float)
        np_values = np.asarray(net_position.value, dtype=float)
        if not (np.all(np.isfinite(gen_values)) and np.all(np.isfinite(shed_values)) and np.all(np.isfinite(np_values))):
            raise RuntimeError("Solver returned non-finite decision variables.")

        prices: dict[str, list[float]] = {area: [] for area in NO_AREAS}
        for area in NO_AREAS:
            for t in range(count):
                dual = balance_constraints[(area, t)].dual_value
                price = marginal_price_eur_mwh(dual, delta_t_h)
                prices[area].append(price if price is not None else math.nan)

        generation_by_mtu: list[dict[str, float]] = []
        generator_dispatch_by_mtu: list[list[dict[str, Any]]] = []
        net_positions_by_mtu: list[dict[str, float]] = []
        load_shedding_by_mtu: list[dict[str, float]] = []
        area_residuals_by_mtu: list[dict[str, float]] = []
        for t in range(count):
            per_area = {area: 0.0 for area in NO_AREAS}
            per_generator: list[dict[str, Any]] = []
            for g, generator in enumerate(generators):
                dispatch_mw = float(gen_values[g, t])
                per_area[str(generator["node"])] += dispatch_mw
                lower_mw = float(p_min[g, t])
                upper_mw = float(p_max[g, t])
                tolerance_mw = max(0.1, 1e-4 * max(upper_mw, 1.0))
                per_generator.append({
                    "generator_id": str(generator["id"]),
                    "zone": str(generator["node"]),
                    "fuel_type": str(generator["fuel_type"]),
                    "dispatch_mw": round(dispatch_mw, 4),
                    "minimum_mw": round(lower_mw, 4),
                    "available_capacity_mw": round(upper_mw, 4),
                    "headroom_mw": round(max(0.0, upper_mw - dispatch_mw), 4),
                    "marginal_cost_eur_mwh": round(float(generation_cost[g, t]), 4),
                    "at_minimum": dispatch_mw <= lower_mw + tolerance_mw,
                    "at_capacity": dispatch_mw >= upper_mw - tolerance_mw,
                    "fixed_observed_output": str(generator["id"]) in market_inputs_by_mtu[t].get("fixed_generation_mw_by_generator", {}),
                })
            generator_dispatch_by_mtu.append(per_generator)
            generation_by_mtu.append({k: round(v, 4) for k, v in per_area.items()})
            net_positions_by_mtu.append({zone: round(float(np_values[z, t]), 4) for z, zone in enumerate(zones)})
            load_shedding_by_mtu.append({area: round(float(shed_values[a, t]), 4) for a, area in enumerate(NO_AREAS)})
            area_residuals_by_mtu.append({
                area: round(per_area[area] + float(shed_values[a, t]) - demand[a, t] - float(np_values[zone_index[area], t]), 7)
                for a, area in enumerate(NO_AREAS)
            })

        hydro_usage = {}
        hydro_residual = 0.0
        for area in hydro_budget_constraints:
            hydro_indices = hydro_generator_indices[area]
            used_mwh = delta_t_h * float(np.sum(gen_values[hydro_indices, :]))
            budget_mwh = float(daily_hydro_energy_budget_mwh[area])
            violation = max(0.0, used_mwh - budget_mwh)
            hydro_residual = max(hydro_residual, violation)
            hydro_usage[area] = {
                "daily_budget_mwh": round(budget_mwh, 3),
                "used_energy_mwh": round(used_mwh, 3),
                "remaining_budget_mwh": round(max(0.0, budget_mwh - used_mwh), 3),
                "constraint_dual_eur_per_mwh": (
                    round(float(hydro_budget_constraints[area].dual_value), 6)
                    if hydro_budget_constraints[area].dual_value is not None else None
                ),
            }

        max_balance_residual = max(
            (abs(value) for row in area_residuals_by_mtu for value in row.values()), default=0.0
        )
        max_system_residual = max(
            (abs(float(np.sum(np_values[:, t]))) for t in range(count)), default=0.0
        )
        jao_balance_by_mtu: list[dict[str, Any]] = []
        jao_balance_duals_by_mtu: list[list[dict[str, Any]]] = []
        max_jao_balance_residual = 0.0
        for t in range(count):
            raw_positions = {
                zone: float(np_values[z, t]) for z, zone in enumerate(zones)
            }
            residuals = jao_net_position_residuals(raw_positions)
            jao_balance_by_mtu.append({
                "delivery_start_utc": periods[t],
                **residuals,
            })
            balance_duals = []
            for descriptor, constraint in zip(
                jao_balance_descriptors, jao_balance_constraints_by_mtu[t]
            ):
                dual = constraint.dual_value
                raw_dual = float(dual) if dual is not None else None
                balance_duals.append({
                    **descriptor,
                    "dual_raw_eur_per_mw_interval": round(raw_dual, 8) if raw_dual is not None else None,
                    "hourly_equivalent_shadow_value_eur_per_mw": (
                        round(raw_dual / delta_t_h, 6) if raw_dual is not None else None
                    ),
                })
            jao_balance_duals_by_mtu.append(balance_duals)
            max_jao_balance_residual = max(
                max_jao_balance_residual,
                float(residuals["max_jao_net_position_residual_mw"]),
            )
        cnec_violation_by_mtu: list[float] = []
        for t, (coefficient_matrix, ram_vector) in enumerate(zip(cnec_coefficients_by_mtu, cnec_ram_by_mtu)):
            violations = coefficient_matrix @ np_values[:, t] - ram_vector
            cnec_violation_by_mtu.append(max(0.0, float(np.max(violations))) if violations.size else 0.0)
        max_cnec_violation = max(cnec_violation_by_mtu, default=0.0)
        market_ramping = market_ramping_diagnostics(net_positions_by_mtu, periods)
        max_ramp_violation = float(market_ramping["max_ramp_violation_mw"])

        # Retain the per-MTU duals and remaining margins. The previous B output
        # verified CNEC feasibility but discarded the duals, which made it
        # impossible to identify which published domain constraints separated
        # the Norwegian zonal prices.
        cnec_diagnostics_by_mtu: list[dict[str, Any]] = []
        for t, (coefficient_matrix, ram_vector, rows, cnec_constraint) in enumerate(zip(
            cnec_coefficients_by_mtu, cnec_ram_by_mtu, cnec_rows_by_mtu, cnec_constraints_by_mtu
        )):
            flows = coefficient_matrix @ np_values[:, t]
            remaining = ram_vector - flows
            dual_values = cnec_constraint.dual_value
            if dual_values is None:
                raw_duals = np.zeros(len(rows), dtype=float)
                duals_available = False
            else:
                raw_duals = np.asarray(dual_values, dtype=float).reshape(-1)
                if raw_duals.size != len(rows) or not np.all(np.isfinite(raw_duals)):
                    raw_duals = np.zeros(len(rows), dtype=float)
                    duals_available = False
                else:
                    duals_available = True

            diagnostics_rows: list[dict[str, Any]] = []
            for index, row in enumerate(rows):
                raw_dual = float(raw_duals[index])
                ram = float(ram_vector[index])
                flow = float(flows[index])
                margin = float(remaining[index])
                # Match Approach A's reporting convention: raw dual is the
                # objective sensitivity to 1 MW of RAM for this MTU; dividing
                # by MTU duration gives an hourly-equivalent shadow value.
                shadow_value = marginal_price_eur_mwh(raw_dual, delta_t_h)
                diagnostics_rows.append({
                    "constraint_id": row.get("constraint_id"),
                    "cnec_name": row.get("cnec_name") or row.get("cne_name"),
                    "cnec_type": row.get("cnec_type"),
                    "significant": row.get("significant"),
                    "non_redundant": row.get("non_redundant"),
                    "tso": row.get("tso"),
                    "contingency": row.get("contingency"),
                    "ptdf_flow_mw": round(flow, 4),
                    "ram_mw": round(ram, 4),
                    "published_ram_mw": round(float(rows[index].get("ram_mw", ram)), 4),
                    "scenario_ram_multiplier": round(
                        float(market_inputs_by_mtu[t].get("scenario_ram_multiplier", 1.0)), 4
                    ),
                    "remaining_margin_mw": round(margin, 4),
                    "dual_raw_eur_per_mw_interval": round(raw_dual, 8),
                    "shadow_price_eur_per_mw": round(shadow_value, 6) if shadow_value is not None else None,
                    "positive_dual": raw_dual > 1e-7,
                    "near_limit": margin <= max(1.0, abs(ram) * 1e-4),
                })

            positive_dual = sorted(
                (item for item in diagnostics_rows if item["positive_dual"]),
                key=lambda item: float(item["dual_raw_eur_per_mw_interval"]),
                reverse=True,
            )
            closest = sorted(diagnostics_rows, key=lambda item: float(item["remaining_margin_mw"]))[:10]
            cnec_diagnostics_by_mtu.append({
                "delivery_start_utc": periods[t],
                "filter_summary": cnec_filter_summary(constraints_by_mtu[t].get("constraints", [])),
                "cnec_count": len(diagnostics_rows),
                "positive_dual_count": len(positive_dual),
                "near_limit_count": sum(1 for item in diagnostics_rows if item["near_limit"]),
                "duals_available": duals_available,
                "binding_cnecs": positive_dual[:20],
                "closest_cnecs": closest,
            })

        max_balance_by_mtu = [
            max((abs(value) for value in residuals.values()), default=0.0)
            for residuals in area_residuals_by_mtu
        ]
        max_system_by_mtu = [abs(float(np.sum(np_values[:, t]))) for t in range(count)]
        max_jao_by_mtu = [
            float(row["max_jao_net_position_residual_mw"]) for row in jao_balance_by_mtu
        ]
        prices_finite_by_mtu = [
            all(math.isfinite(prices[area][t]) for area in NO_AREAS)
            for t in range(count)
        ]
        ramp_violation_by_mtu = [
            max(
                (float(item["violation_mw"]) for item in market_ramping["by_mtu"][t]["ramp_constraints"]),
                default=0.0,
            )
            for t in range(count)
        ]
        mw_residual_by_mtu = [
            max(
                max_balance_by_mtu[t], max_system_by_mtu[t], max_jao_by_mtu[t],
                cnec_violation_by_mtu[t], ramp_violation_by_mtu[t],
            )
            for t in range(count)
        ]
        interval_feasible = [
            bool(
                prices_finite_by_mtu[t]
                and mw_residual_by_mtu[t] <= feasibility_tolerance
                and hydro_residual <= hydro_budget_tolerance_mwh
            )
            for t in range(count)
        ]
        feasible = all(interval_feasible)

        interval_results = []
        for t in range(count):
            market = market_inputs_by_mtu[t]
            interval_results.append({
                "delivery_start_utc": periods[t],
                "nodal_marginal_prices_eur_mwh": {area: round(prices[area][t], 4) for area in NO_AREAS},
                "generation_by_area_mw": generation_by_mtu[t],
                "generator_dispatch": generator_dispatch_by_mtu[t],
                "net_positions_mw": net_positions_by_mtu[t],
                "load_shedding_mw": load_shedding_by_mtu[t],
                "area_balance_residuals_mw": area_residuals_by_mtu[t],
                "boundary_prices_eur_mwh": dict(market.get("boundary_price_forecasts_eur_mwh", {})),
                "boundary_price_area": dict(market.get("boundary_price_area", {})),
                "hydro_cost_shift_eur_mwh": dict(market.get("hydro_cost_shift_eur_mwh", {})),
                "generator_available_capacity_mw": dict(market.get("generator_available_capacity_mw", {})),
                "cnec_diagnostics": cnec_diagnostics_by_mtu[t],
                "jao_net_position_balance": jao_balance_by_mtu[t],
                "jao_balance_duals": jao_balance_duals_by_mtu[t],
                "market_ramping": market_ramping["by_mtu"][t],
                "max_mw_residual_mw": round(mw_residual_by_mtu[t], 8),
                "feasible": interval_feasible[t],
            })

        return {
            "status": str(problem.status),
            "feasible": feasible,
            "feasibility_status": "VALID" if feasible else "NUMERICAL_VIOLATION",
            "model_version": "B_MULTI_PERIOD_FLOW_BASED_LP",
            "model_scope": "NORWAY_ZONAL_DISPATCH_WITH_FORECAST_BOUNDARIES_AND_OPTIONAL_HYDRO_ENERGY_COUPLING",
            "solver": solver_name,
            "timings": timings,
            "objective_eur": round(float(problem.value), 4),
            "currency_unit": "EUR/MWh",
            "power_unit": "MW",
            "energy_unit": "MWh",
            "delta_t_h": delta_t_h,
            "interval_count": count,
            "cnec_filter_summary": cnec_filter_summary(constraints_by_mtu[0].get("constraints", [])) if constraints_by_mtu else {},
            "market_cnec_count": len(cnec_rows_by_mtu[0]) if cnec_rows_by_mtu else 0,
            "monitoring_only_rows_excluded": max(
                (cnec_filter_summary(domain.get("constraints", [])).get("monitoring_only_rows", 0)
                 for domain in constraints_by_mtu), default=0
            ),
            "non_significant_rows_excluded": max(
                (cnec_filter_summary(domain.get("constraints", [])).get("non_significant_rows", 0)
                 for domain in constraints_by_mtu), default=0
            ),
            "redundant_rows_excluded": max(
                (cnec_filter_summary(domain.get("constraints", [])).get("redundant_rows", 0)
                 for domain in constraints_by_mtu), default=0
            ),
            "excluded_cnec_rows_max_per_mtu": max(
                (len(domain.get("constraints", [])) - len(cnec_rows_by_mtu[index])
                 for index, domain in enumerate(constraints_by_mtu)), default=0
            ),
            "delivery_start_utc": periods[0],
            "delivery_end_utc": (parsed_periods[-1] + timedelta(minutes=15)).isoformat().replace("+00:00", "Z"),
            "forecast_origin_utc": market_inputs_by_mtu[0].get("forecast_origin_utc"),
            "intervals": interval_results,
            "hydro_coupling_enabled": hydro_coupling_enabled,
            "hydro_coupling_mode": "DAILY_ZONAL_MWH_SCENARIO_BUDGET" if hydro_coupling_enabled else "DISABLED_NO_ENERGY_BUDGET",
            "price_estimate_quality": (
                "SCENARIO_BUDGETED_HYDRO_NOT_MEASURED_RESERVOIR_STATE"
                if hydro_coupling_enabled
                else "LIMITED_NO_DAILY_HYDRO_ENERGY_BUDGET"
            ),
            "hydro_energy_budgets": hydro_usage,
            "market_ramping_constraints": market_ramp_descriptors,
            "market_ramping_diagnostics": market_ramping,
            "diagnostics": {
                "max_area_balance_residual_mw": round(max_balance_residual, 8),
                "max_system_balance_residual_mw": round(max_system_residual, 8),
                "max_jao_net_position_residual_mw": round(max_jao_balance_residual, 8),
                "jao_balance_constraints": jao_balance_descriptors,
                "jao_balance_by_mtu": jao_balance_by_mtu,
                "jao_balance_duals_by_mtu": jao_balance_duals_by_mtu,
                "max_cnec_violation_mw": round(max_cnec_violation, 8),
                "max_cnec_violation_by_mtu_mw": {
                    periods[t]: round(cnec_violation_by_mtu[t], 8) for t in range(count)
                },
                "max_mw_residual_by_mtu": {
                    periods[t]: round(mw_residual_by_mtu[t], 8) for t in range(count)
                },
                "max_hydro_budget_violation_mwh": round(hydro_residual, 8),
                "max_market_ramp_violation_mw": round(max_ramp_violation, 8),
                "max_market_ramp_violation_by_mtu_mw": {
                    periods[t]: round(ramp_violation_by_mtu[t], 8) for t in range(count)
                },
                "market_ramp_constraint_count": len(market_ramp_constraints),
                "feasibility_tolerance_mw": feasibility_tolerance,
                "hydro_budget_tolerance_mwh": hydro_budget_tolerance_mwh,
            },
            "warnings": [
                *( ["Daily usable-hydro MWh values are labelled sensitivity budgets, not measured reservoir states."] if hydro_coupling_enabled else ["No daily hydro-energy budget was supplied; B uses configured hydro marginal costs without intertemporal hydro coupling."]),
                "Positive CNEC duals identify marginal constraints in this LP solution; active-constraint duals can be non-unique under degeneracy.",
                "External regions remain price-taking boundary conditions; this is not full Nordic market clearing.",
                "External virtual-zone endpoints are valued using their own adjacent market-area A44 reference; local NO2_SK is endogenous and is not priced from observed NO2 A44.",
                "15-minute interconnector ramp limits are applied to contiguous Approach B blocks; the first MTU has no pre-horizon flow boundary.",
                "SwePol QPRE is not applied until the date-specific QPRE activation message is available.",
                "Generator capacities and marginal costs remain configured assumptions until outage and bid data are integrated.",
            ],
        }
