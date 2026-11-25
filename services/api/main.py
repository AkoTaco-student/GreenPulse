"""greenpulse API: observed ENTSO-E prices remain separate from modeled dispatch."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]


from dotenv import load_dotenv

def _load_local_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

_load_local_environment()

from models.dispatch.dispatch_lp import EconomicDispatchLP
from models.dispatch.flow_based_dispatch import FlowBasedDispatchLP
from models.grid.flow_based_domain import fetch_flow_based_domain
from models.validation.market_observations import (
    compare_observed_to_model,
    validate_price_observations,
)
from services.api.market_data import get_day_ahead_prices
from services.api.market_inputs import fetch_flow_based_market_inputs, fetch_flow_based_forecast_inputs, NO_AREAS
from services.api.forecast_calendar import (
    default_forecast_origin,
    next_oslo_delivery_day,
    validate_pre_auction_origin,
)
from services.api.day_ahead_jobs import submit_day_ahead_job, get_day_ahead_job

app = FastAPI(
    title="greenpulse API",
    description="Norwegian Power Market & Grid Observatory API",
    version="1.2.0",
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv(
        "greenpulse_WEB_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

CONFIG_DIR = PROJECT_ROOT / "config"
dispatch_engine = EconomicDispatchLP()
flow_based_dispatch_engine = FlowBasedDispatchLP()


class OptimizationRequest(BaseModel):
    scenario_id: str | None = "normal_base"
    demand_multipliers: dict[str, float] = Field(default_factory=dict)
    hydro_cost_shifts: dict[str, float] = Field(default_factory=dict)
    line_capacity_overrides: dict[str, float] = Field(default_factory=dict)
    generator_availability: dict[str, float] = Field(default_factory=dict)
    wind_multiplier: float | None = None
    ram_multiplier: float = 1.0
    solver: str = "HIGHS"


class PriceComparisonRequest(BaseModel):
    modeled_prices: dict[str, float]


class DayAheadForecastRequest(BaseModel):
    delivery_day: date = Field(
        default_factory=next_oslo_delivery_day,
        description="Oslo-local delivery date (YYYY-MM-DD); defaults to tomorrow.",
    )
    forecast_origin_utc: datetime | None = Field(
        default=None, description="Information cutoff. Defaults to D-1 11:15 Europe/Oslo, before day-ahead gate closure."
    )
    daily_hydro_energy_budget_mwh: dict[str, float] = Field(
        default_factory=dict,
        description="Optional, explicitly scenario-labelled total usable hydro energy by NO zone (MWh); requires a complete delivery day.",
    )
    max_workers: int = Field(default=8, ge=1, le=12)
    mode: str = Field(default="FORECAST", description="FORECAST for strict D-1 origin; REPLAY for ex-post or nowcast diagnostics.")
    scenario_id: str = Field(default="normal_base", description="Supported day-ahead scenarios: normal_base and cold_snap_winter. Legacy line overrides do not modify JAO CNECs.")
    refresh_inputs: bool = Field(default=False, description="Ignore the persisted day snapshot; recent in-process source-cache entries may still be reused.")


def _load_scenarios() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "scenarios.yaml"
    if not path.exists():
        raise HTTPException(status_code=500, detail="scenarios.yaml ikke funnet")
    with path.open("r", encoding="utf-8") as source:
        return yaml.safe_load(source).get("scenarios", [])


def _scenario_inputs(request: OptimizationRequest) -> dict[str, Any]:
    demand = dict(request.demand_multipliers)
    hydro = dict(request.hydro_cost_shifts)
    line_caps = dict(request.line_capacity_overrides)
    availability = dict(request.generator_availability)
    wind_multiplier = request.wind_multiplier
    warnings: list[str] = []

    if request.scenario_id:
        scenario = next(
            (item for item in _load_scenarios() if item["id"] == request.scenario_id),
            None,
        )
        if scenario is None:
            raise HTTPException(status_code=404, detail=f"Ukjent scenario: {request.scenario_id}")
        scalar_demand = float(scenario.get("demand_multiplier", 1.0))
        for area in ("NO1", "NO2", "NO3", "NO4", "NO5"):
            demand.setdefault(area, scalar_demand)
        for area, multiplier in scenario.get("demand_multipliers", {}).items():
            if area not in request.demand_multipliers:
                demand[area] = float(multiplier)

        hydro_shift = float(scenario.get("hydro_value_shift_eur", 0.0))
        if hydro_shift:
            hydro_areas = scenario.get("hydro_areas")
            for area in hydro_areas or ():
                hydro.setdefault(area, hydro_shift)
            if not hydro_areas:
                hydro.setdefault("ALL", hydro_shift)
        wind_multiplier = (
            request.wind_multiplier
            if request.wind_multiplier is not None
            else float(scenario.get("wind_multiplier", 1.0))
        )
        for line in dispatch_engine.lines:
            line_id = line["id"]
            scenario_line_cap = scenario.get("line_overrides", {}).get(line_id)
            default_capacity = float(line["capacity_mw"]) * float(
                scenario.get("line_capacity_multiplier", 1.0)
            )
            line_caps.setdefault(
                line_id,
                float(scenario_line_cap if scenario_line_cap is not None else default_capacity),
            )
        if scenario.get("interconnector_overrides"):
            warnings.append(
                "Scenarioet inneholder utenlandskabler, men den reduserte NO1-NO5-modellen "
                "har ingen eksterne noder; kabelendringen er ikke brukt i dispatchberegningen."
            )

    return {
        "demand_multipliers": demand,
        "hydro_cost_shifts": hydro,
        "line_capacity_overrides": line_caps,
        "generator_availability": availability,
        "wind_multiplier": 1.0 if wind_multiplier is None else wind_multiplier,
        "solver": request.solver,
        "warnings": warnings,
    }


def _solve(request: OptimizationRequest) -> dict[str, Any]:
    inputs = _scenario_inputs(request)
    warnings = inputs.pop("warnings")
    try:
        result = dispatch_engine.solve(**inputs)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    result["scenario_id"] = request.scenario_id
    result["warnings"] = [*result.get("warnings", []), *warnings]
    return result


def _flow_based_inputs(request: OptimizationRequest) -> tuple[dict[str, Any], list[str]]:
    inputs = _scenario_inputs(request)
    warnings = list(inputs.pop("warnings"))
    inputs.pop("line_capacity_overrides", None)
    scenario = next(
        (item for item in _load_scenarios() if item["id"] == request.scenario_id),
        {},
    ) if request.scenario_id else {}
    if request.line_capacity_overrides or scenario.get("line_overrides"):
        warnings.append(
            "Individuelle legacy-linjeoverstyringer kan ikke mappes til JAO CNEC-rader; "
            "bruk ram_multiplier for et eksplisitt, syntetisk scenario."
        )
    inputs["ram_multiplier"] = request.ram_multiplier * float(
        scenario.get("flow_based_ram_multiplier", 1.0)
    )
    return inputs, warnings


def _delivery_period(delivery_time: datetime | None) -> datetime:
    value = delivery_time or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise HTTPException(status_code=422, detail="delivery_time må inkludere tidssone (helst UTC).")
    return value


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "service": "greenpulse API", "version": "1.2.0"}


@app.get("/api/market/prices")
def get_market_prices(day: date | None = Query(default=None)) -> dict[str, Any]:
    """Hent ENTSO-E day-ahead-priser; manglende token gir ikke syntetiske tall."""
    market_day = day or datetime.now(ZoneInfo("Europe/Oslo")).date()
    result = get_day_ahead_prices(market_day)
    validation = validate_price_observations(result.get("records", []))
    result["validation"] = {
        "valid": validation["valid"],
        "accepted_count": validation["accepted_count"],
        "errors": validation["errors"],
        "quality_status": validation["quality_status"],
    }
    result["records"] = validation["records"]
    return result


@app.get("/api/market/overview")
def get_market_overview() -> dict[str, Any]:
    """Returner modellstatus, ikke feilmerket som observerte driftsdata."""
    market_data = get_day_ahead_prices(datetime.now(ZoneInfo("Europe/Oslo")).date())
    validated = validate_price_observations(market_data.get("records", []))
    observed_areas = sorted({record["area"] for record in validated["records"]})
    return {
        "system_status": "OBSERVATIONS_AND_MODEL" if observed_areas else "MODELLED_ONLY",
        "observed_data_available": bool(observed_areas),
        "observed_areas": observed_areas,
        "source_status": market_data.get("message", market_data.get("quality_status", "ENTSO-E status unavailable")),
        "dispatch": dispatch_engine.solve(),
    }


@app.get("/api/scenarios")
def get_scenarios() -> list[dict[str, Any]]:
    return _load_scenarios()


@app.post("/api/optimization/run")
def run_optimization(request: OptimizationRequest) -> dict[str, Any]:
    """Kjør én konveks, enperiodisk DC-dispatch med eksplisitte antakelser."""
    return _solve(request)


@app.get("/api/grid/flow-based-domain")
def get_flow_based_domain(
    delivery_time: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Return the published JAO Nordic CNEC/PTDF/RAM rows for one MTU."""
    try:
        return fetch_flow_based_domain(_delivery_period(delivery_time))
    except HTTPException:
        raise
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/optimization/day-ahead-inputs")
def get_day_ahead_forecast_inputs(
    delivery_time: datetime,
    forecast_origin_utc: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Inspect forecast-vintage input data for one requested MTU (no solver run)."""
    if delivery_time.tzinfo is None:
        raise HTTPException(status_code=422, detail="delivery_time må inneholde tidssone.")
    period = _delivery_period(delivery_time)
    origin = forecast_origin_utc or default_forecast_origin(period.astimezone(ZoneInfo("Europe/Oslo")).date())
    try:
        origin = validate_pre_auction_origin(period.astimezone(ZoneInfo("Europe/Oslo")).date(), origin)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        domain = fetch_flow_based_domain(period, forecast_origin_utc=origin)
        market = fetch_flow_based_forecast_inputs(period, origin, domain.get("zones", []))
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail={"message": "Upstream input request failed.", "error": str(error)}) from error
    return {"constraints_data": domain, "market_inputs": market}


@app.post("/api/optimization/day-ahead-forecast", status_code=202)
def start_day_ahead_forecast(request: DayAheadForecastRequest) -> dict[str, Any]:
    """Queue paired 15-minute day-ahead LPs for the full Oslo delivery day."""
    origin = request.forecast_origin_utc or default_forecast_origin(request.delivery_day)
    mode = request.mode.strip().upper()
    if mode not in {"FORECAST", "REPLAY"}:
        raise HTTPException(status_code=422, detail="mode must be FORECAST or REPLAY.")
    scenarios = {item.get("id"): item for item in _load_scenarios()}
    scenario = scenarios.get(request.scenario_id)
    if scenario is None:
        raise HTTPException(status_code=422, detail=f"Unknown day-ahead scenario: {request.scenario_id}")
    if request.scenario_id == "grid_congestion_no1_no2":
        raise HTTPException(
            status_code=422,
            detail="Flaskehals is a legacy physical-line demonstration; it cannot safely override published JAO CNECs in the flow-based day-ahead model.",
        )
    if request.scenario_id not in {"normal_base", "cold_snap_winter"}:
        raise HTTPException(status_code=422, detail=f"Scenario {request.scenario_id} is not implemented for day-ahead dispatch.")
    if mode == "FORECAST":
        try:
            origin = validate_pre_auction_origin(request.delivery_day, origin)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    if mode == "FORECAST" and datetime.now(timezone.utc) < origin:
        return {
            "status": "WAITING_FOR_FORECAST_ORIGIN",
            "delivery_day_local": request.delivery_day.isoformat(),
            "forecast_origin_utc": origin.isoformat().replace("+00:00", "Z"),
            "mode": mode,
            "message": "The pre-auction information cutoff has not been reached. Submit a FORECAST again at or after the forecast origin, or use REPLAY for ex-post published inputs.",
        }
    job = submit_day_ahead_job(
        request.delivery_day,
        forecast_origin_utc=origin,
        daily_hydro_energy_budget_mwh=request.daily_hydro_energy_budget_mwh,
        scenario_id=request.scenario_id,
        max_workers=request.max_workers,
        mode=mode,
        refresh_inputs=request.refresh_inputs,
    )
    return job


@app.get("/api/optimization/day-ahead-forecast/jobs/{job_id}")
def get_day_ahead_forecast_job(job_id: str) -> dict[str, Any]:
    """Return in-process job status and the paired A/B result when complete."""
    job = get_day_ahead_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Forecast job not found; in-memory jobs are lost on API restart.")
    return job


@app.get("/api/optimization/flow-based-inputs")
def get_flow_based_inputs(
    delivery_time: datetime | None = Query(default=None),
    include_generation_diagnostics: bool = Query(default=False),
) -> dict[str, Any]:
    """Inspect the exact ENTSO-E inputs selected for one published JAO MTU."""
    period = _delivery_period(delivery_time)
    try:
        domain = fetch_flow_based_domain(period)
        inputs = fetch_flow_based_market_inputs(
            period,
            domain.get("zones", []),
            include_generation_diagnostics=include_generation_diagnostics,
        )
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return {**inputs, "jao_constraint_count": domain.get("constraint_count", 0), "jao_zones": domain.get("zones", [])}


@app.post("/api/optimization/flow-based-run")
def run_flow_based_optimization(
    request: OptimizationRequest,
    delivery_time: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Solve dispatch under the JAO Nordic flow-based market-domain constraints."""
    period = _delivery_period(delivery_time)
    try:
        domain = fetch_flow_based_domain(period)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    try:
        market_inputs = fetch_flow_based_market_inputs(period, domain.get("zones", []))
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if market_inputs.get("status") != "READY":
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Flow-based solve is blocked because required market inputs are unavailable; no fixed-load fallback is permitted.",
                "market_inputs": market_inputs,
            },
        )
    inputs, warnings = _flow_based_inputs(request)
    solver = inputs.pop("solver")
    ram_multiplier = inputs.pop("ram_multiplier")
    try:
        result = flow_based_dispatch_engine.solve(
            constraints_data=domain,
            market_inputs=market_inputs,
            solver=solver,
            ram_multiplier=ram_multiplier,
            **inputs,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    result["scenario_id"] = request.scenario_id
    result["warnings"] = warnings
    return result


@app.post("/api/optimization/flow-based-forecast")
def forecast_flow_based_scenarios(
    request: OptimizationRequest,
    delivery_time: datetime | None = Query(default=None),
) -> dict[str, Any]:
    """Solve deterministic low/base/high scenarios over one published MTU."""
    period = _delivery_period(delivery_time)
    try:
        domain = fetch_flow_based_domain(period)
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    try:
        market_inputs = fetch_flow_based_market_inputs(period, domain.get("zones", []))
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if market_inputs.get("status") != "READY":
        raise HTTPException(
            status_code=503,
            detail={
                "message": "Scenario solve is blocked because required market inputs are unavailable; no fixed-load fallback is permitted.",
                "market_inputs": market_inputs,
            },
        )
    base, warnings = _flow_based_inputs(request)
    solver = base.pop("solver")
    ram_multiplier = base.pop("ram_multiplier")
    base.pop("line_capacity_overrides", None)
    assumptions = {
        "low": {"demand": 0.95, "wind_delta": 0.10, "ram": 1.05, "hydro_delta": -5.0},
        "base": {"demand": 1.0, "wind_delta": 0.0, "ram": 1.0, "hydro_delta": 0.0},
        "high": {"demand": 1.05, "wind_delta": -0.10, "ram": 0.95, "hydro_delta": 5.0},
    }
    results: dict[str, Any] = {}
    try:
        for label, case in assumptions.items():
            case_inputs = dict(base)
            case_inputs["demand_multipliers"] = {
                area: multiplier * case["demand"]
                for area, multiplier in base["demand_multipliers"].items()
            }
            case_inputs["wind_multiplier"] = min(
                1.0, max(0.0, base["wind_multiplier"] + case["wind_delta"])
            )
            case_inputs["hydro_cost_shifts"] = {
                area: shift + case["hydro_delta"]
                for area, shift in base["hydro_cost_shifts"].items()
            }
            results[label] = flow_based_dispatch_engine.solve(
                constraints_data=domain,
                market_inputs=market_inputs,
                solver=solver,
                ram_multiplier=ram_multiplier * case["ram"],
                **case_inputs,
            )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "forecast_kind": "SINGLE_MTU_DIAGNOSTIC_SCENARIO_RANGE_NOT_DAY_AHEAD_FORECAST",
        "probabilities_assigned": False,
        "scenario_id": request.scenario_id,
        "source": domain["source"],
        "delivery_start_utc": domain["delivery_start_utc"],
        "assumptions": assumptions,
        "results": results,
        "warnings": warnings,
    }


@app.post("/api/optimization/forecast")
def forecast_scenarios(request: OptimizationRequest) -> dict[str, Any]:
    """Beregn scenariointervall (lav/basis/høy), ikke en statistisk sannsynlighet."""
    base = _scenario_inputs(request)
    warnings = base.pop("warnings")
    cases = {
        "low": {"demand_scale": 0.95, "wind": min(1.0, base["wind_multiplier"] + 0.15), "hydro_shift": -5.0, "capacity_scale": 1.05},
        "base": {"demand_scale": 1.0, "wind": base["wind_multiplier"], "hydro_shift": 0.0, "capacity_scale": 1.0},
        "high": {"demand_scale": 1.05, "wind": max(0.0, base["wind_multiplier"] - 0.15), "hydro_shift": 5.0, "capacity_scale": 0.95},
    }
    results: dict[str, Any] = {}
    for label, assumptions in cases.items():
        inputs = {key: value for key, value in base.items() if key != "wind_multiplier"}
        inputs["demand_multipliers"] = {
            area: multiplier * assumptions["demand_scale"]
            for area, multiplier in base["demand_multipliers"].items()
        }
        inputs["hydro_cost_shifts"] = {
            area: shift + assumptions["hydro_shift"]
            for area, shift in base["hydro_cost_shifts"].items()
        }
        inputs["wind_multiplier"] = assumptions["wind"]
        inputs["line_capacity_overrides"] = {
            line_id: capacity * assumptions["capacity_scale"]
            for line_id, capacity in base["line_capacity_overrides"].items()
        }
        inputs.pop("solver", None)
        results[label] = dispatch_engine.solve(**inputs)
    return {
        "forecast_kind": "DETERMINISTIC_SCENARIO_RANGE",
        "probabilities_assigned": False,
        "currency_unit": "EUR/MWh",
        "scenario_id": request.scenario_id,
        "assumptions": cases,
        "results": results,
        "warnings": warnings,
    }


@app.post("/api/validation/price-comparison")
def price_comparison(
    request: PriceComparisonRequest,
    day: date | None = Query(default=None),
) -> dict[str, Any]:
    """Sammenlign publiserte priser med modellverdier, kun der begge finnes."""
    data = get_day_ahead_prices(day or date.today())
    validation = validate_price_observations(data.get("records", []))
    comparison = compare_observed_to_model(validation["records"], request.modeled_prices)
    return {
        "available": data.get("available", False),
        "comparison": comparison,
        "source_errors": data.get("area_errors", {}),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("services.api.main:app", host="0.0.0.0", port=8000, reload=True)
