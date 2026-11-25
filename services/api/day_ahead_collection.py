"""One-day, DST-aware collection and readiness normalization for day-ahead dispatch."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any, Callable

from models.grid.flow_based_domain import fetch_flow_based_domain_batch
from services.api.forecast_calendar import default_forecast_origin, delivery_periods
from services.api.market_inputs import (
    fetch_flow_based_forecast_inputs_batch,
    fetch_flow_based_market_inputs_batch,
    prefetch_day_ahead_forecast_sources,
    prefetch_replay_market_sources,
)

Progress = Callable[[str, int | None, int | None], None]
AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5")


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("forecast origin must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")




def _timed_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    value = function(*args, **kwargs)
    return value, time.perf_counter() - started


def readiness_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    domain = item.get("constraints_data") or {}
    market = item.get("market_inputs") or {}

    domain_status = str(domain.get("status", "UNKNOWN")).upper()
    if domain_status != "READY":
        reason = domain.get("reason") or domain.get("vintage_validation_reason")
        reasons.append(f"JAO_DOMAIN_{domain_status}" + (f": {reason}" if reason else ""))
    elif domain.get("forecast_origin_usable") is False:
        reasons.append("JAO_FORECAST_ORIGIN_NOT_USABLE")

    missing_load = list(market.get("missing_load_areas") or [])
    missing_wind = list(
        market.get("missing_wind_availability_areas")
        or market.get("missing_renewable_forecast_areas")
        or market.get("missing_wind_forecast_areas")
        or []
    )
    missing_boundary = list(market.get("missing_boundary_price_zones") or [])
    if missing_load:
        reasons.append("MISSING_A65_LOAD: " + ", ".join(str(area) for area in missing_load))
    if missing_wind:
        reasons.append("MISSING_WIND_AVAILABILITY: " + ", ".join(str(area) for area in missing_wind))
    if missing_boundary:
        reasons.append("MISSING_A44_BOUNDARY: " + ", ".join(str(area) for area in missing_boundary))

    market_status = str(market.get("status", "UNKNOWN")).upper()
    if market_status not in {"READY", ""}:
        reasons.append(f"MARKET_INPUTS_{market_status}")
    if market.get("forecast_origin_usable") is False:
        reasons.append("MARKET_FORECAST_ORIGIN_NOT_USABLE")

    return list(dict.fromkeys(reasons))


def _domain_zones(domain: dict[str, Any]) -> list[str]:
    zones: set[str] = set(domain.get("zones") or [])
    for constraint in domain.get("constraints") or []:
        ptdf = constraint.get("ptdf") if isinstance(constraint, dict) else None
        if isinstance(ptdf, dict):
            zones.update(str(zone) for zone in ptdf)
    return sorted(zones)


def collect_day_ahead_inputs(
    delivery_day: date,
    forecast_origin: datetime | None = None,
    *,
    mode: str = "FORECAST",
    max_workers: int = 12,
    progress_callback: Progress | None = None,
) -> dict[str, Any]:
    mode = mode.upper()
    if mode not in {"FORECAST", "REPLAY"}:
        raise ValueError("mode must be FORECAST or REPLAY")

    origin = forecast_origin or default_forecast_origin(delivery_day)
    if origin.tzinfo is None:
        raise ValueError("forecast_origin must be timezone-aware")
    origin = origin.astimezone(timezone.utc)

    periods = delivery_periods(delivery_day)
    expected = len(periods)
    timings: dict[str, float] = {}

    if progress_callback:
        progress_callback("PREFETCHING_SHARED_SOURCES", 0, expected)

    # JAO domains and ENTSO-E sources are independent. Fetch them concurrently
    # so solver start is gated by the slower source, not the sum of both waits.
    stage_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as stage_pool:
        source_function = (
            prefetch_day_ahead_forecast_sources if mode == "FORECAST"
            else prefetch_replay_market_sources
        )
        source_future = stage_pool.submit(
            _timed_call, source_function, periods, max_workers=max_workers
        )
        if progress_callback:
            progress_callback("COLLECTING_JAO_DOMAINS", 0, expected)
        domain_future = stage_pool.submit(
            _timed_call, fetch_flow_based_domain_batch, periods, None if mode == "REPLAY" else origin
        )
        domains, jao_elapsed = domain_future.result()
        prefetched, prefetch_elapsed = source_future.result()

    timings["parallel_source_collection_seconds"] = round(time.perf_counter() - stage_started, 6)
    timings["prefetch_seconds"] = round(prefetch_elapsed, 6)
    timings["jao_domain_seconds"] = round(jao_elapsed, 6)

    zones_by_mtu: dict[str, list[str]] = {}
    for period in periods:
        key = utc_text(period)
        zones_by_mtu[key] = _domain_zones(domains.get(key) or {})

    if progress_callback:
        progress_callback("COLLECTING_MARKET_INPUTS", 0, expected)
    started = time.perf_counter()
    if mode == "REPLAY":
        markets = fetch_flow_based_market_inputs_batch(
            periods, zones_by_mtu, max_workers=max_workers, prefetched_sources=prefetched
        )
    else:
        markets = fetch_flow_based_forecast_inputs_batch(
            periods, origin, zones_by_mtu, prefetched_sources=prefetched, max_workers=max_workers
        )
    timings["market_input_seconds"] = round(time.perf_counter() - started, 6)

    intervals: list[dict[str, Any]] = []
    readiness_by_mtu: dict[str, list[str]] = {}
    errors_by_mtu: dict[str, list[str]] = {}

    for index, period in enumerate(periods, 1):
        key = utc_text(period)
        domain = domains.get(key) or {
            "status": "NO_DATA",
            "reason": "JAO domain batch returned no MTU record",
            "delivery_start_utc": key,
            "constraints": [],
            "zones": [],
        }
        market = markets.get(key) or {
            "status": "INCOMPLETE",
            "forecast_origin_usable": False,
            "missing_load_areas": list(AREAS),
            "missing_boundary_price_zones": [],
            "missing_wind_forecast_areas": [],
            "demand_mw": {},
            "boundary_price_forecasts_eur_mwh": {},
            "generator_available_capacity_mw": {},
        }
        normalized_market = dict(market)
        if mode == "REPLAY":
            normalized_market["mode"] = "REPLAY"
            normalized_market["forecast_origin_usable"] = True
            normalized_market["boundary_price_forecasts_eur_mwh"] = dict(
                market.get("boundary_prices_eur_mwh")
                or market.get("boundary_price_forecasts_eur_mwh")
                or {}
            )
        else:
            normalized_market["mode"] = "FORECAST"

        interval = {
            "delivery_start_utc": key,
            "forecast_origin_utc": utc_text(origin),
            "constraints_data": domain,
            "market_inputs": normalized_market,
        }
        reasons = readiness_reasons(interval)
        interval["readiness_reasons"] = reasons
        interval["ready"] = not reasons
        readiness_by_mtu[key] = reasons
        if reasons:
            errors_by_mtu[key] = reasons
        intervals.append(interval)

        if progress_callback:
            progress_callback("COLLECTING_MTUS", index, expected)

    collected = sum(1 for interval in intervals if interval["constraints_data"] and interval["market_inputs"])
    ready_count = sum(1 for interval in intervals if interval["ready"])

    return {
        "mode": mode,
        "delivery_day_local": delivery_day.isoformat(),
        "forecast_origin_utc": utc_text(origin),
        "expected_interval_count": expected,
        "collected_interval_count": collected,
        "ready_interval_count": ready_count,
        "intervals": intervals,
        "readiness_by_mtu": readiness_by_mtu,
        "errors_by_mtu": errors_by_mtu,
        "collection_timings": timings,
    }
