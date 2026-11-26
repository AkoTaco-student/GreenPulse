"""Shared orchestration and strict forecast-vintage checks for model A and B."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from models.dispatch.flow_based_dispatch import FlowBasedDispatchLP
from models.dispatch.multi_period_flow_based_dispatch import MultiPeriodFlowBasedDispatchLP
from models.validation.forecast_vintage import parse_utc as _parse_utc, validate_forecast_vintage


def _as_solver_market_inputs(market: dict[str, Any]) -> dict[str, Any]:
    """Adapt the forecast contract to the existing one-MTU LP input schema."""
    result = dict(market)
    result["status"] = "READY"
    result["boundary_prices_eur_mwh"] = dict(market["boundary_price_forecasts_eur_mwh"])
    result["forecast_origin_usable_at_mtu_start"] = True
    result["load_policy"] = "A65_A01_DAY_AHEAD_FORECAST_ONLY"
    result["boundary_policy"] = "LAGGED_A44_SAME_LOCAL_MTUD1_FORECAST_PROXY"
    return result


def solve_approach_a(
    intervals: list[dict[str, Any]],
    *,
    engine: FlowBasedDispatchLP | None = None,
    solver: str = "MOSEK", # can swap to MOSEK
) -> dict[str, Any]:
    """Approach A: independent flow-based LP for each 15-minute MTU."""
    if not intervals:
        raise ValueError("Approach A needs at least one MTU input.")
    dispatch = engine or FlowBasedDispatchLP()
    results = []
    previous_start: datetime | None = None
    common_origin: datetime | None = None
    for index, item in enumerate(intervals):
        domain = item["constraints_data"]
        market = item["market_inputs"]
        origin = item["forecast_origin_utc"]
        if market.get("mode") != "REPLAY":
            validate_forecast_vintage(domain, market, origin)
        parsed_origin = _parse_utc(origin, "forecast_origin_utc")
        if common_origin is None:
            common_origin = parsed_origin
        elif common_origin != parsed_origin:
            raise ValueError("All MTUs in Approach A must share one forecast origin.")
        start = _parse_utc(domain["delivery_start_utc"], "delivery_start_utc")
        if start.minute % 15 != 0 or start.second != 0 or start.microsecond != 0:
            raise ValueError(f"Approach A delivery start {start} is not 15-minute aligned.")
        if previous_start is not None and start <= previous_start:
            raise ValueError("Approach A intervals must be strictly increasing and unique.")
        previous_start = start
        adapted = _as_solver_market_inputs(market)
        result = dispatch.solve(
            constraints_data=domain,
            market_inputs=adapted,
            solver=solver,
            generator_available_capacity_mw=market.get("generator_available_capacity_mw", {}),
            hydro_cost_shifts=market.get("hydro_cost_shift_eur_mwh", {}),
            wind_multiplier=float(market.get("scenario_wind_multiplier", 1.0)),
            ram_multiplier=float(market.get("scenario_ram_multiplier", 1.0)),
        )
        result["model_version"] = "A_INDEPENDENT_MTU_FLOW_BASED_LP"
        result["forecast_kind"] = "DAY_AHEAD_FORECAST"
        result["forecast_origin_utc"] = _parse_utc(origin, "forecast_origin_utc").isoformat().replace("+00:00", "Z")
        result["demand_provenance"] = "ENTSOE_A65_A01_DAY_AHEAD_FORECAST_ONLY"
        result["boundary_policy"] = "PREVIOUS_OSLO_DAY_A44_SAME_LOCAL_MTU_PROXY"
        result["wind_availability_policy"] = (
            "A69_A01_TARGET_DAY_IF_PUBLISHED_BY_ORIGIN_ELSE_PREVIOUS_OSLO_DAY_SAME_LOCAL_MTU_PROXY"
        )
        result["warnings"] = list(result.get("warnings", [])) + [
            "Approach A is a day-ahead forecast, not an ex-post reconstruction.",
            "Boundary A44 values are a previous-day proxy, not a forecast of neighbouring-market prices.",
            "Hydro dispatch has no intertemporal water-energy constraint in Approach A.",
        ]
        results.append(result)
    feasible_all = all(result.get("feasible") is True for result in results)
    infeasible_any = any(result.get("feasible") is False for result in results)
    feasible = True if feasible_all else False if infeasible_any else None
    has_solver_infeasibility = any(
        result.get("feasibility_status") == "SOLVER_INFEASIBLE" for result in results
    )
    has_numerical_violation = any(
        result.get("feasibility_status") == "NUMERICAL_VIOLATION" for result in results
    )
    aggregate_status = (
        "SOLVED" if feasible is True
        else "INFEASIBLE" if has_solver_infeasibility
        else "SOLVED_WITH_NUMERICAL_VIOLATIONS" if has_numerical_violation
        else "SOLVER_ERROR"
    )
    return {
        "status": aggregate_status,
        "model_version": "A_INDEPENDENT_MTU_FLOW_BASED_LP",
        "forecast_kind": "DAY_AHEAD_FORECAST",
        "forecast_origin_utc": _parse_utc(intervals[0]["forecast_origin_utc"], "forecast_origin_utc").isoformat().replace("+00:00", "Z"),
        "interval_count": len(results),
        "feasible": feasible,
        "intervals": results,
        "method_note": "Each 15-minute LP is independent. Hydro is represented by fixed opportunity-cost assumptions; there is no day-level water-energy coupling.",
    }


def solve_approach_b(
    intervals: list[dict[str, Any]],
    *,
    daily_hydro_energy_budget_mwh: dict[str, float] | None = None,
    engine: MultiPeriodFlowBasedDispatchLP | None = None,
    solver: str = "MOSEK",
) -> dict[str, Any]:
    """Approach B: one coupled multi-period LP with an aggregate hydro budget."""
    if not intervals:
        raise ValueError("Approach B needs MTU inputs.")
    domains = []
    markets = []
    origins = []
    previous_start: datetime | None = None
    for item in intervals:
        domain = item["constraints_data"]
        market = item["market_inputs"]
        origin = item["forecast_origin_utc"]
        if market.get("mode") != "REPLAY":
            validate_forecast_vintage(domain, market, origin)
        start = _parse_utc(domain["delivery_start_utc"], "delivery_start_utc")
        if previous_start is not None and start - previous_start != timedelta(minutes=15):
            raise ValueError("Approach B intervals must be ordered and contiguous 15-minute MTUs.")
        previous_start = start
        origins.append(_parse_utc(origin, "forecast_origin_utc"))
        domains.append(domain)
        adapted = dict(market)
        adapted["boundary_price_forecasts_eur_mwh"] = dict(market["boundary_price_forecasts_eur_mwh"])
        markets.append(adapted)
    if len(set(origins)) != 1:
        raise ValueError("All intervals in one forecast must share the same forecast origin.")
    dispatch = engine or MultiPeriodFlowBasedDispatchLP()
    result = dispatch.solve(
        constraints_by_mtu=domains,
        market_inputs_by_mtu=markets,
        daily_hydro_energy_budget_mwh=daily_hydro_energy_budget_mwh,
        solver=solver,
    )
    result["forecast_kind"] = "DAY_AHEAD_FORECAST"
    result["forecast_origin_utc"] = origins[0].isoformat().replace("+00:00", "Z")
    return result
