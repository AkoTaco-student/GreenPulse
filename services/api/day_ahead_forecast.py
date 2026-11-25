"""Collect forecast-vintage inputs and run GreenPulse day-ahead dispatch models.

A solves each ready MTU independently. B solves the full day or contiguous
ready blocks. Reservoir-coupled B requires the complete delivery day.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo
import yaml

from models.dispatch.day_ahead_forecast import solve_approach_a, solve_approach_b
from models.grid.flow_based_domain import flow_based_cache_stats
from services.api.forecast_calendar import default_forecast_origin, delivery_periods, validate_pre_auction_origin
from services.api.market_inputs import (
    market_input_cache_stats,
)
from services.api.day_ahead_collection import (
    collect_day_ahead_inputs,
    readiness_reasons as _readiness_reasons,
    utc_text as _utc,
)
from services.api.day_ahead_snapshots import (
    forecast_snapshot_path as _forecast_snapshot_path,
    load_forecast_snapshot as _load_forecast_snapshot,
    load_replay_snapshot as _load_replay_snapshot,
    save_forecast_snapshot as _save_forecast_snapshot,
    save_replay_snapshot as _save_replay_snapshot,
)

AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5")
MTU = timedelta(minutes=15)
Progress = Callable[[str, int | None, int | None], None]


def _solver_inputs(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: item[key] for key in ("constraints_data", "market_inputs", "forecast_origin_utc")}
        for item in intervals
    ]


def _day_ahead_scenario(scenario_id: str) -> dict[str, Any]:
    supported = {"normal_base", "cold_snap_winter", "dry_year_low_inflow", "grid_congestion_no1_no2"}
    if scenario_id not in supported:
        raise ValueError(f"Scenario {scenario_id!r} is not supported by the JAO day-ahead LP.")
    path = Path(__file__).resolve().parents[2] / "config" / "scenarios.yaml"
    with path.open("r", encoding="utf-8") as source:
        scenarios = yaml.safe_load(source).get("scenarios", [])
    for scenario in scenarios:
        if scenario.get("id") == scenario_id:
            return scenario
    raise ValueError(f"Day-ahead scenario {scenario_id!r} is not configured.")


def _apply_day_ahead_scenario(
    intervals: list[dict[str, Any]], scenario: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply supported scenario assumptions on copies, preserving raw snapshots."""
    scalar_demand = float(scenario.get("demand_multiplier", 1.0))
    area_demand = {str(area): float(value) for area, value in scenario.get("demand_multipliers", {}).items()}
    wind_multiplier = float(scenario.get("wind_multiplier", 1.0))
    hydro_shift = float(scenario.get("hydro_value_shift_eur", 0.0))
    hydro_areas = scenario.get("hydro_areas") or AREAS
    ram_multiplier = float(scenario.get("ram_multiplier", 1.0))
    if not 0.01 <= ram_multiplier <= 1.0:
        raise ValueError("Scenario RAM multiplier must be in [0.01, 1.0].")
    adjusted = []
    for item in intervals:
        copy = dict(item)
        market = dict(item["market_inputs"])
        demand = dict(market.get("demand_mw", {}))
        for area, value in demand.items():
            demand[area] = float(value) * scalar_demand * area_demand.get(area, 1.0)
        market["demand_mw"] = demand
        market["scenario_wind_multiplier"] = wind_multiplier
        market["scenario_ram_multiplier"] = ram_multiplier
        shifts = dict(market.get("hydro_cost_shift_eur_mwh", {}))
        if hydro_shift:
            for area in hydro_areas:
                shifts[str(area)] = float(shifts.get(str(area), 0.0)) + hydro_shift
        market["hydro_cost_shift_eur_mwh"] = shifts
        copy["market_inputs"] = market
        adjusted.append(copy)
    return adjusted


def _feasible(result: dict[str, Any]) -> bool | None:
    if result.get("feasible") is False:
        return False
    if result.get("feasible") is True:
        return True
    return None


def _feasibility_status(value: bool | None) -> str:
    # Kept for callers without interval detail. The main day-ahead aggregator
    # distinguishes solver infeasibility from a post-solve numerical warning.
    return "SOLVED" if value is True else "UNKNOWN" if value is None else "SOLVED_WITH_NUMERICAL_VIOLATIONS"


def _not_run(model: str, reason: str) -> dict[str, Any]:
    return {"model": model, "status": "NOT_RUN", "feasible": None, "intervals": [], "reason": reason}


def _cache_stats() -> dict[str, int | float]:
    return {**flow_based_cache_stats(), **market_input_cache_stats()}


def _solve_b(ready: list[dict[str, Any]], hydro: dict[str, Any] | None = None, progress: Progress | None = None, **kwargs: Any) -> dict[str, Any]:
    hydro = hydro or {}
    if not ready:
        return _not_run("B", "NO_READY_INTERVALS")

    if hydro.get("daily_hydro_energy_budget_mwh"):
        sorted_ready = sorted(ready, key=lambda value: value["delivery_start_utc"])
        first = datetime.fromisoformat(sorted_ready[0]["delivery_start_utc"].replace("Z", "+00:00"))
        local_day = first.astimezone(ZoneInfo("Europe/Oslo")).date()
        expected = [_utc(period) for period in delivery_periods(local_day)]
        if [item["delivery_start_utc"] for item in sorted_ready] != expected:
            return _not_run("B", "DAILY_HYDRO_BUDGET_REQUIRES_COMPLETE_CONTIGUOUS_DELIVERY_DAY")

    blocks: list[list[dict[str, Any]]] = []
    for item in sorted(ready, key=lambda value: value["delivery_start_utc"]):
        if not blocks:
            blocks.append([item])
            continue
        previous = datetime.fromisoformat(blocks[-1][-1]["delivery_start_utc"].replace("Z", "+00:00"))
        current = datetime.fromisoformat(item["delivery_start_utc"].replace("Z", "+00:00"))
        if current - previous == MTU:
            blocks[-1].append(item)
        else:
            blocks.append([item])

    if progress:
        progress("SOLVING_APPROACH_B", 0, len(ready))
    block_results = []
    all_intervals = []
    elapsed_seconds = 0.0
    for index, block in enumerate(blocks, 1):
        started = time.perf_counter()
        if len(block) < 2:
            result = _not_run("B", "CONTIGUOUS_READY_BLOCK_TOO_SHORT_FOR_APPROACH_B")
        else:
            try:
                result = solve_approach_b(_solver_inputs(block), **hydro)
            except Exception as exc:
                result = {"model": "B", "status": "SOLVER_ERROR", "feasible": None,
                          "intervals": [], "reason": f"{type(exc).__name__}: {exc}"}
        block_elapsed = time.perf_counter() - started
        elapsed_seconds += block_elapsed
        if result.get("feasible") is True:
            all_intervals.extend(result.get("intervals", []))
        block_results.append({
            "block_index": index,
            "start_utc": block[0]["delivery_start_utc"],
            "end_utc": block[-1]["delivery_start_utc"],
            "input_interval_count": len(block),
            "elapsed_seconds": round(block_elapsed, 6),
            "result": result,
        })
        if progress:
            progress("SOLVING_APPROACH_B", sum(len(item) for item in blocks[:index]), len(ready))

    solved_blocks = [item["result"] for item in block_results if item["result"].get("status") != "NOT_RUN"]
    feasible = False if any(_feasible(item) is False for item in solved_blocks) else (
        True if solved_blocks and all(_feasible(item) is True for item in solved_blocks) else None
    )
    has_solver_infeasibility = any(
        item.get("feasibility_status") == "SOLVER_INFEASIBLE" for item in solved_blocks
    )
    has_numerical_violation = any(
        item.get("feasibility_status") == "NUMERICAL_VIOLATION" for item in solved_blocks
    )
    status = "NOT_RUN" if not solved_blocks else (
        "SOLVED" if feasible is True
        else "SOLVED_WITH_INFEASIBILITY" if has_solver_infeasibility
        else "SOLVED_WITH_NUMERICAL_VIOLATIONS" if has_numerical_violation
        else "SOLVER_ERROR"
    )
    return {
        "model": "B", "status": status, "feasible": feasible,
        "model_version": "B_MULTI_PERIOD_FLOW_BASED_LP",
        "execution_policy": "CONTIGUOUS_READY_BLOCKS", "block_count": len(blocks),
        "intervals": all_intervals, "block_results": block_results,
        "elapsed_seconds": round(elapsed_seconds, 6),
    }


def _price_records(model: str, result: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    replay = snapshot.get("mode") == "REPLAY"
    for interval in result.get("intervals", []):
        if interval.get("feasible") is False:
            continue
        timestamp = interval.get("delivery_start_utc")
        prices = interval.get("nodal_marginal_prices_eur_mwh") or interval.get("nodal_marginal_prices") or {}
        for area in AREAS:
            if timestamp and prices.get(area) is not None:
                records.append({
                    "area": area,
                    "timestamp": timestamp,
                    "price_eur_mwh": float(prices[area]),
                    "unit": "EUR/MWh",
                    "resolution_minutes": 15,
                    "source": f"GreenPulse CVXPY Approach {model}",
                    "document_type": "MODELLED_REPLAY" if replay else "MODELLED_DA_FORECAST",
                    "process_type": "EX_POST_REPLAY" if replay else "PRE_AUCTION_FORECAST",
                    "quality_status": "MODELLED_REPLAY" if replay else "MODELLED_FORECAST",
                    "model_version": result.get("model_version"),
                    "forecast_origin_utc": snapshot["forecast_origin_utc"],
                    "delivery_day_local": snapshot["delivery_day_local"],
                })
    return records


def run_both_day_ahead_models(
    delivery_day: date,
    *, daily_hydro_energy_budget_mwh: dict[str, float] | None = None,
    scenario_id: str = "normal_base",
    forecast_origin_utc: datetime | None = None,
    max_workers: int = 12,
    mode: str = "FORECAST",
    refresh_inputs: bool = False,
    progress_callback: Progress | None = None,
    persist_snapshot: bool = False,
) -> dict[str, Any]:
    """Collect inputs once, solve Approach B first, and use Approach A only for gaps."""
    mode = mode.upper()
    if mode not in {"FORECAST", "REPLAY"}:
        raise ValueError("mode must be FORECAST or REPLAY.")
    scenario = _day_ahead_scenario(scenario_id)
    origin = forecast_origin_utc or default_forecast_origin(delivery_day)
    if origin.tzinfo is None:
        raise ValueError("forecast_origin_utc must be timezone-aware.")
    origin = origin.astimezone(timezone.utc)
    if mode == "FORECAST":
        origin = validate_pre_auction_origin(delivery_day, origin)

    total_started = time.perf_counter()
    collection_started = time.perf_counter()
    cache_stats_before = _cache_stats()
    snapshot_cache_hit = False
    if mode == "REPLAY":
        snapshot = None if refresh_inputs else _load_replay_snapshot(delivery_day, origin)
        if snapshot is not None:
            snapshot_cache_hit = True
            if progress_callback:
                loaded_count = snapshot.get("ready_interval_count", len(snapshot["intervals"]))
                progress_callback("LOADED_REPLAY_SNAPSHOT", loaded_count, snapshot["expected_interval_count"])
        else:
            snapshot = collect_day_ahead_inputs(delivery_day, origin, max_workers=max_workers, mode=mode, progress_callback=progress_callback)
            # Persist a complete collection even when some MTUs are not ready.
            # The snapshot retains explicit per-MTU readiness and diagnostics,
            # so scenario changes can reuse inputs without rerunning upstream fetches.
            if snapshot["collected_interval_count"] == snapshot["expected_interval_count"]:
                _save_replay_snapshot(delivery_day, snapshot)
    else:
        snapshot = None if refresh_inputs else _load_forecast_snapshot(delivery_day, origin)
        if snapshot is not None:
            snapshot_cache_hit = True
            if progress_callback:
                progress_callback(
                    "LOADED_FORECAST_SNAPSHOT",
                    snapshot.get("collected_interval_count", len(snapshot["intervals"])),
                    snapshot["expected_interval_count"],
                )
        else:
            snapshot = collect_day_ahead_inputs(delivery_day, origin, max_workers=max_workers, mode=mode, progress_callback=progress_callback)
            if snapshot["collected_interval_count"] == snapshot["expected_interval_count"]:
                _save_forecast_snapshot(delivery_day, origin, snapshot)
    collection_seconds = time.perf_counter() - collection_started
    cache_stats_after = _cache_stats()
    upstream_cache_stats = {
        key: cache_stats_after.get(key, 0) - cache_stats_before.get(key, 0)
        for key in cache_stats_after
    }

    ready = [item for item in snapshot["intervals"] if item["ready"]]
    solver_ready = _apply_day_ahead_scenario(ready, scenario)
    hydro = {"daily_hydro_energy_budget_mwh": daily_hydro_energy_budget_mwh} if daily_hydro_energy_budget_mwh else {}

    solver_started = time.perf_counter()
    approach_b = _solve_b(solver_ready, hydro, progress_callback)
    b_times = {interval["delivery_start_utc"] for interval in approach_b.get("intervals", [])}
    fallback = [item for item in solver_ready if item["delivery_start_utc"] not in b_times]

    if fallback:
        approach_a_started = time.perf_counter()
        approach_a = solve_approach_a(_solver_inputs(fallback))
        approach_a["elapsed_seconds"] = round(time.perf_counter() - approach_a_started, 6)
    else:
        approach_a = _not_run("A", "APPROACH_B_COVERS_ALL_READY_MTUS")
        approach_a["elapsed_seconds"] = 0.0
    solver_seconds = time.perf_counter() - solver_started

    models = {"A": approach_a, "B": approach_b}
    states = [_feasible(model) for model in models.values() if model.get("status") != "NOT_RUN"]
    ready_count = len(ready)
    modelled_times = b_times | {
        interval["delivery_start_utc"]
        for interval in approach_a.get("intervals", [])
    }
    expected = snapshot["expected_interval_count"]
    solver_infeasibility = any(
        model.get("status") == "INFEASIBLE"
        or model.get("status") == "SOLVED_WITH_INFEASIBILITY"
        or any(
            item.get("feasibility_status") == "SOLVER_INFEASIBLE"
            for item in model.get("intervals", [])
        )
        or any(
            block.get("result", {}).get("feasibility_status") == "SOLVER_INFEASIBLE"
            for block in model.get("block_results", [])
        )
        for model in models.values()
    )
    numerical_violation = any(
        model.get("status") == "SOLVED_WITH_NUMERICAL_VIOLATIONS"
        or any(
            item.get("feasibility_status") == "NUMERICAL_VIOLATION"
            for item in model.get("intervals", [])
        )
        or any(
            block.get("result", {}).get("feasibility_status") == "NUMERICAL_VIOLATION"
            for block in model.get("block_results", [])
        )
        for model in models.values()
    )
    if not ready:
        status = "INPUTS_INCOMPLETE"
    elif solver_infeasibility:
        status = "SOLVED_WITH_INFEASIBILITY"
    elif numerical_violation or False in states:
        status = "SOLVED_WITH_NUMERICAL_VIOLATIONS"
    elif None in states:
        status = "SOLVED_WITH_UNKNOWN_FEASIBILITY"
    elif ready_count < expected or len(modelled_times) < expected:
        status = "SOLVED_PARTIAL"
    else:
        status = "SOLVED"

    records = {name: _price_records(name, model, snapshot) for name, model in models.items()}
    missing = [item["delivery_start_utc"] for item in snapshot["intervals"] if not item["ready"]]

    result = {
        "mode": mode,
        "scenario_id": scenario_id,
        "status": status,
        "delivery_day_local": snapshot["delivery_day_local"],
        "forecast_origin_utc": snapshot["forecast_origin_utc"],
        "expected_interval_count": expected,
        "interval_count": expected,
        "ready_interval_count": ready_count,
        "modelled_interval_count": {name: len(model.get("intervals", [])) for name, model in models.items()},
        "modelled_interval_count_total": len(modelled_times),
        "coverage": {
            "expected_interval_count": expected,
            "collected_interval_count": snapshot["collected_interval_count"],
            "ready_interval_count": ready_count,
            "ready_fraction": ready_count / expected if expected else 0.0,
            "missing_interval_count": len(missing),
            "missing_intervals_utc": missing,
        },
        "errors_by_mtu": snapshot["errors_by_mtu"],
        "readiness_by_mtu": snapshot["readiness_by_mtu"],
        "model_price_records": records,
        "model_price_record_counts": {name: len(rows) for name, rows in records.items()},
        "timings": {
            "snapshot_cache_hit": snapshot_cache_hit,
            "collection_or_cache_load_seconds": round(collection_seconds, 6),
            "collection_stages": snapshot.get("collection_timings", {}),
            "upstream_cache_stats": upstream_cache_stats,
            "approach_b_seconds": approach_b.get("elapsed_seconds", 0.0),
            "approach_a_seconds": approach_a.get("elapsed_seconds", 0.0),
            "solver_stage_seconds": round(solver_seconds, 6),
            "total_seconds": round(time.perf_counter() - total_started, 6),
        },
        "models": models,
        "warnings": [
            "B is the primary coupled model; A is used only for ready MTUs not covered by B.",
            f"Scenario assumptions applied: {scenario.get('name', scenario_id)}.",
            "Wind is fixed to A75/A16 realized output in replay or capped by an A69/A01 MTU forecast; missing wind blocks the affected MTU.",
            "REPLAY mode uses published or replay inputs, not an as-issued D-1 forecast." if mode == "REPLAY" else "Strict forecast mode requires publication-vintage checks.",
        ],
    }

    if persist_snapshot:
        root = Path(__file__).resolve().parents[2]
        forecast_dir = root / "data" / "processed" / "forecasts"
        forecast_dir.mkdir(parents=True, exist_ok=True)
        tag = snapshot["forecast_origin_utc"].replace(":", "").replace("-", "")
        result_file = forecast_dir / f"{delivery_day.isoformat()}_origin_{tag}_{scenario_id}_results.json"
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["result_path"] = str(result_file.relative_to(root))
    return result
