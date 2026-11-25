"""Time-aligned ENTSO-E inputs for replay and strict day-ahead forecast models.

The replay loader may select A65/A16 actual load or A65/A01 forecast. The
forecast loader is deliberately separate and accepts only A65/A01 load and
A69/A01 renewable forecasts known at its explicit forecast origin. For A69, a
previous-Oslo-day same-local-MTU proxy is allowed and explicitly labelled when
the target-day vintage has not yet been published.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import math
import os
import threading
import time as monotonic_time
from typing import Any, Callable
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from services.api.http_client import env_float, env_int, read_url
from services.api.market_data import (
    AREA_EIC,
    BOUNDARY_PRICE_AREAS,
    ENTSOE_ENDPOINT,
    _market_day_bounds_utc,
    _query_area,
    price_cache_stats,
)

OSLO_TZ = ZoneInfo("Europe/Oslo")
NO_AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5")
WIND_GENERATOR_BY_ZONE = {
    "NO2": "GEN_NO2_WIND",
    "NO3": "GEN_NO3_WIND_FOSEN",
    "NO4": "GEN_NO4_WIND",
}
WIND_ZONES = tuple(WIND_GENERATOR_BY_ZONE.keys())
# ENTSO-E PSR type codes (current common code list): B16=solar, B18=wind offshore, B19=wind onshore.
WIND_PSR_TYPES = {"B18", "B19"}
SOLAR_PSR_TYPES = {"B16"}

# External virtual zones use the price of the external market they represent.
# Internal virtual zones use the price of their own endpoint bidding zone. This
# avoids reversing the price spread on internal links and, critically, avoids
# feeding observed NO2 A44 back into the NO2 price that the LP is estimating.
VIRTUAL_ZONE_PRICE_REFERENCE = {
    "NO2_ND": "NL",
    "NO2_NK": "DE_LU",
    "NO2_SK": "NO2",  # local Norwegian endpoint; endogenous, not an A44 input
    "FI_EL": "EE",
    "FI_FS": "FI",
    "SE3_FS": "SE3",
    "SE3_KS": "SE3",
    "SE3_SWL": "SE3",
    "SE4_BC": "DE_LU",
    "SE4_NB": "LT",
    "SE4_SP": "PL",
    "SE4_SWL": "SE4",
    "DK1_CO": "NL",
    "DK1_DE": "DE_LU",
    "DK1_KS": "DK1",
    "DK1_SB": "DK1",
    "DK1_SK": "DK1",
    "DK2_KO": "DE_LU",
    "DK2_SB": "DK2",
}
LOCAL_MODELLED_VIRTUAL_ZONES = frozenset({"NO2_SK"})

VIRTUAL_ZONE_PRICE_FALLBACKS = {
    "FI_EL": ("FI",),
}
ALL_BOUNDARY_AREAS = frozenset(
    (set(BOUNDARY_PRICE_AREAS)
     | set(VIRTUAL_ZONE_PRICE_REFERENCE.values())
     | {area for fallbacks in VIRTUAL_ZONE_PRICE_FALLBACKS.values() for area in fallbacks})
    - set(NO_AREAS)
)

READY_SNAPSHOT_TTL_SECONDS = 30.0
INCOMPLETE_SNAPSHOT_TTL_SECONDS = 10.0
EMPTY_DOCUMENT_TTL_SECONDS = 20.0
DOCUMENT_FAILURE_BACKOFF_SECONDS = 45.0
ENTSOE_TIMEOUT_SECONDS = env_float("GREENPULSE_ENTSOE_TIMEOUT_SECONDS", 75.0)
ENTSOE_RETRIES = env_int("GREENPULSE_ENTSOE_RETRIES", 2, maximum=4)

_DocumentKey = tuple[str, str, str, str, str, str, str]
_INPUT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_DOCUMENT_CACHE: dict[_DocumentKey, tuple[float, list[dict[str, Any]]]] = {}
_DOCUMENT_FAILURE_CACHE: dict[_DocumentKey, tuple[float, str]] = {}
_DOCUMENT_INFLIGHT: dict[_DocumentKey, Future[list[dict[str, Any]]]] = {}
_DOCUMENT_CACHE_LOCK = threading.Lock()
_CACHE_STATS_LOCK = threading.Lock()
_CACHE_STATS = {
    "input_snapshot_cache_hits": 0,
    "entsoe_document_cache_hits": 0,
    "entsoe_document_fetches": 0,
    "entsoe_document_inflight_waits": 0,
    "entsoe_document_fetch_seconds": 0.0,
}


# ---------------------------------------------------------------------------
# Time, Timestamp & Provenance Helpers
# ---------------------------------------------------------------------------


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
    return value.astimezone(timezone.utc)


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _format_utc_z(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _quarter_hour(value: datetime) -> datetime:
    dt = _utc(value)
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def _oslo_today() -> date:
    return datetime.now(OSLO_TZ).date()


def _single_oslo_day(delivery_times: list[datetime], label: str) -> tuple[list[datetime], date]:
    periods = [_quarter_hour(_utc(value)) for value in delivery_times]
    oslo_days = {period.astimezone(OSLO_TZ).date() for period in periods}
    if len(oslo_days) != 1:
        raise ValueError(f"{label} must contain exactly one Oslo-local delivery day.")
    return periods, next(iter(oslo_days))


def _day_ahead_publication_deadline_utc(delivery_day: date, document_type: str) -> datetime:
    """Return ENTSO-E regulatory day-ahead publication deadline in UTC."""
    publication_day = delivery_day - timedelta(days=1)
    deadline_local = {
        "A44": time(13, 0),  # SDAC day-ahead prices (~12:45 CET)
        "A65": time(12, 0),  # Day-ahead load forecast
        "A69": time(18, 0),  # Day-ahead wind/solar forecast
    }.get(document_type, time(18, 0))
    return datetime.combine(publication_day, deadline_local, tzinfo=OSLO_TZ).astimezone(timezone.utc)


def _available_at_timestamp(
    record: dict[str, Any],
    cutoff_utc: datetime,
    *,
    delivery_day: date | None = None,
    document_type: str | None = None,
) -> tuple[bool | None, str]:
    """Verify whether a record was published at or before `cutoff_utc`.

    On historical queries, ENTSO-E sets `<createdDateTime>` to the API response
    generation timestamp (within seconds of `retrieved_at_utc`). When detected,
    availability is checked against the regulatory gate-closure deadline.
    """
    pub_utc = _parse_utc(record.get("published_at_utc"))
    retrieved_utc = _parse_utc(record.get("retrieved_at_utc"))
    doc_type = document_type or str(record.get("document_type") or "")

    if pub_utc is not None:
        if pub_utc <= cutoff_utc:
            return True, "DOCUMENT_CREATED_AT_BEFORE_CUTOFF"
        is_api_response_ts = (
            retrieved_utc is not None and abs((retrieved_utc - pub_utc).total_seconds()) < 120.0
        )
        if not is_api_response_ts:
            return False, "REVISED_AFTER_CUTOFF"

    if delivery_day is not None and doc_type in {"A44", "A65", "A69"}:
        if _day_ahead_publication_deadline_utc(delivery_day, doc_type) <= cutoff_utc:
            return True, "ENTSOE_REGULATORY_GATE_CLOSURE_BEFORE_CUTOFF"
        return False, "BEFORE_ENTSOE_REGULATORY_GATE_CLOSURE"

    return (None, "MISSING_PUBLICATION_TIMESTAMP") if pub_utc is None else (False, "UNVERIFIED_TIMESTAMP")


# ---------------------------------------------------------------------------
# Cache Stats & ENTSO-E XML Client
# ---------------------------------------------------------------------------


def market_input_cache_stats() -> dict[str, int | float]:
    with _CACHE_STATS_LOCK:
        document_stats = dict(_CACHE_STATS)
    return {**document_stats, **price_cache_stats()}


def _bump_cache_stat(name: str) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[name] += 1


def _add_cache_time(name: str, seconds: float) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[name] += seconds


def _document_cache_key(area: str, day: date, document_type: str, process_type: str) -> _DocumentKey:
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return (area, day.isoformat(), document_type, process_type, ENTSOE_ENDPOINT, AREA_EIC[area], token_digest)


def _query_document_uncached(
    area: str,
    day: date,
    document_type: str,
    process_type: str,
    timeout_seconds: float = ENTSOE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("ENTSOE_API_TOKEN is not configured.")
    if area not in AREA_EIC:
        raise ValueError(f"No ENTSO-E EIC configured for {area}.")

    start, end = _market_day_bounds_utc(day)
    params: dict[str, str] = {
        "securityToken": token,
        "documentType": document_type,
        "processType": process_type,
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }
    if document_type == "A65":
        params["outBiddingZone_Domain"] = AREA_EIC[area]
    else:
        params["in_Domain"] = AREA_EIC[area]

    request = urllib.request.Request(
        f"{ENTSOE_ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "greenpulse/1.2 (time-aligned market inputs)"},
    )
    body = read_url(
        request,
        timeout_seconds=timeout_seconds,
        retries=ENTSOE_RETRIES,
        source_label=f"ENTSO-E {document_type}/{process_type} {area}",
    )

    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise RuntimeError(f"ENTSO-E {document_type}/{process_type} {area} returned invalid XML.") from error

    records: list[dict[str, Any]] = []
    retrieved_at = _format_utc_z(datetime.now(timezone.utc))
    document_id = root.findtext(".//{*}mRID")
    document_revision = root.findtext(".//{*}revisionNumber")
    pub_dt = _parse_utc(root.findtext(".//{*}createdDateTime"))
    published_at_utc = _format_utc_z(pub_dt) if pub_dt else None

    for series in root.findall(".//{*}TimeSeries"):
        psr_type = series.findtext(".//{*}MktPSRType/{*}psrType") or series.findtext(".//{*}psrType") or ""
        unit_name = series.findtext(".//{*}quantity_Measure_Unit.name") or series.findtext(".//{*}unit") or "MAW"
        for period in series.findall("{*}Period"):
            interval = period.find("{*}timeInterval")
            period_start_el = interval.find("{*}start") if interval is not None else None
            resolution_el = period.find("{*}resolution")
            if period_start_el is None or not period_start_el.text or resolution_el is None:
                continue
            period_start = _parse_utc(period_start_el.text)
            step_minutes = {"PT15M": 15, "PT30M": 30, "PT60M": 60, "PT1H": 60}.get(resolution_el.text or "")
            if period_start is None or step_minutes is None:
                continue
            for point in period.findall("{*}Point"):
                position_el = point.find("{*}position")
                quantity_el = point.find("{*}quantity")
                if position_el is None or quantity_el is None:
                    continue
                try:
                    position = int(position_el.text or "0")
                    value = float(quantity_el.text or "nan")
                except ValueError:
                    continue
                if position < 1 or not math.isfinite(value):
                    continue
                records.append({
                    "area": area,
                    "timestamp": period_start + timedelta(minutes=step_minutes * (position - 1)),
                    "value_mw": value,
                    "interval_minutes": step_minutes,
                    "psr_type": psr_type,
                    "process_type": process_type,
                    "document_type": document_type,
                    "unit": unit_name,
                    "quality_status": "PUBLISHED",
                    "document_created_at_utc": published_at_utc,
                    "document_id": document_id,
                    "document_revision_number": document_revision,
                    "retrieved_at_utc": retrieved_at,
                    "published_at_utc": published_at_utc,
                })
    return records


def _query_document(
    area: str,
    day: date,
    document_type: str,
    process_type: str,
    timeout_seconds: float = ENTSOE_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Share ENTSO-E document requests across routes and concurrent callers."""
    key = _document_cache_key(area, day, document_type, process_type)
    now = monotonic_time.monotonic()
    with _DOCUMENT_CACHE_LOCK:
        cached = _DOCUMENT_CACHE.get(key)
        if cached and cached[0] > now:
            _bump_cache_stat("entsoe_document_cache_hits")
            return [dict(r) for r in cached[1]]
        failure = _DOCUMENT_FAILURE_CACHE.get(key)
        if failure and failure[0] > now:
            raise RuntimeError(
                f"ENTSO-E {document_type}/{process_type} {area} suppressed after upstream failure: {failure[1]}"
            )
        _DOCUMENT_FAILURE_CACHE.pop(key, None)
        future = _DOCUMENT_INFLIGHT.get(key)
        is_owner = future is None
        if future is None:
            future = Future()
            _DOCUMENT_INFLIGHT[key] = future
    if not is_owner:
        _bump_cache_stat("entsoe_document_inflight_waits")
        return [dict(r) for r in future.result()]

    fetch_started = monotonic_time.perf_counter()
    try:
        _bump_cache_stat("entsoe_document_fetches")
        records = _query_document_uncached(area, day, document_type, process_type, timeout_seconds)
        if not records:
            ttl = EMPTY_DOCUMENT_TTL_SECONDS
        elif document_type == "A65" and process_type == "A16":
            ttl = 20.0
        elif document_type == "A75":
            ttl = 60.0
        else:
            ttl = 300.0
        with _DOCUMENT_CACHE_LOCK:
            _DOCUMENT_CACHE[key] = (monotonic_time.monotonic() + ttl, [dict(r) for r in records])
        future.set_result(records)
        return [dict(r) for r in records]
    except Exception as error:
        with _DOCUMENT_CACHE_LOCK:
            _DOCUMENT_FAILURE_CACHE[key] = (
                monotonic_time.monotonic() + DOCUMENT_FAILURE_BACKOFF_SECONDS,
                f"{type(error).__name__}: {error}",
            )
        future.set_exception(error)
        raise
    finally:
        _add_cache_time("entsoe_document_fetch_seconds", monotonic_time.perf_counter() - fetch_started)
        with _DOCUMENT_CACHE_LOCK:
            _DOCUMENT_INFLIGHT.pop(key, None)


def _run_entsoe_jobs(
    jobs: dict[tuple[str, str], Callable[[], list[dict[str, Any]]]],
    *,
    max_workers: int = 12,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[str, str]]:
    """Execute a dictionary of ENTSO-E fetch callables concurrently."""
    if not jobs:
        return {}, {}
    results: dict[tuple[str, str], list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    workers = max(1, min(int(max_workers), len(jobs), 16))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_key = {executor.submit(fn): key for key, fn in jobs.items()}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as error:
                results[key] = []
                errors[f"{key[0]}:{key[1]}"] = f"{type(error).__name__}: {error}"
    return results, errors


# ---------------------------------------------------------------------------
# Record & Zone Selection Helpers
# ---------------------------------------------------------------------------


def _select_covering_record(
    records: list[dict[str, Any]],
    start: datetime,
    *,
    interval_key: str = "interval_minutes",
    cutoff_utc: datetime | None = None,
    delivery_day: date | None = None,
    document_type: str | None = None,
) -> dict[str, Any] | None:
    """Pick the finest-resolution record covering start (optionally gated by cutoff_utc)."""
    matches: list[tuple[int, dict[str, Any]]] = []
    for record in records:
        record_start = _parse_utc(record.get("timestamp"))
        if record_start is None:
            continue
        try:
            interval = int(record.get(interval_key, 60))
        except (TypeError, ValueError):
            continue
        if not (record_start <= start < record_start + timedelta(minutes=interval)):
            continue
        if cutoff_utc is not None:
            available, _ = _available_at_timestamp(
                record,
                cutoff_utc,
                delivery_day=delivery_day,
                document_type=document_type,
            )
            if available is not True:
                continue
        matches.append((interval, record))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _price_for_mtu(
    records: list[dict[str, Any]],
    start: datetime,
    *,
    forecast_origin: datetime | None = None,
    delivery_day: date | None = None,
) -> dict[str, Any] | None:
    valid_records = []
    for record in records:
        try:
            price = float(record["price_eur_mwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(price):
            valid_records.append(record)
    chosen = _select_covering_record(
        valid_records,
        start,
        interval_key="resolution_minutes",
        cutoff_utc=forecast_origin,
        delivery_day=delivery_day,
        document_type="A44",
    )
    if chosen is None:
        return None
    record_start = _parse_utc(chosen["timestamp"])
    return {
        "price_eur_mwh": float(chosen["price_eur_mwh"]),
        "timestamp_utc": _format_utc_z(record_start) if record_start else str(chosen["timestamp"]),
        "resolution_minutes": int(chosen.get("resolution_minutes", 60)),
        "published_at_utc": chosen.get("published_at_utc"),
        "retrieved_at_utc": chosen.get("retrieved_at_utc"),
        "quality_status": chosen.get("quality_status", "PUBLISHED"),
    }


def _price_reference_for_zone(zone: str) -> str | None:
    if zone in NO_AREAS:
        return None
    if zone in VIRTUAL_ZONE_PRICE_REFERENCE:
        reference = VIRTUAL_ZONE_PRICE_REFERENCE[zone]
        return None if reference in NO_AREAS else reference
    return zone if zone in AREA_EIC else None


def _boundary_areas_for_zones(cnec_zones: list[str]) -> tuple[set[str], list[str]]:
    required: set[str] = set()
    unmapped: list[str] = []
    for zone in cnec_zones:
        if zone in NO_AREAS or zone in LOCAL_MODELLED_VIRTUAL_ZONES:
            continue
        reference = _price_reference_for_zone(zone)
        if reference:
            required.add(reference)
            required.update(VIRTUAL_ZONE_PRICE_FALLBACKS.get(zone, ()))
        else:
            unmapped.append(zone)
    return required, unmapped


# ---------------------------------------------------------------------------
# Replay Loader (Delivery Day D Actuals & Boundary Prices)
# ---------------------------------------------------------------------------


def prefetch_replay_market_sources(
    delivery_times: list[datetime], *, max_workers: int = 12
) -> dict[str, Any]:
    """Fetch independent replay source documents once for one Oslo delivery day."""
    if not delivery_times:
        return {"results": {}, "errors": {}, "timings": {"source_fetch_seconds": 0.0}}
    _, day = _single_oslo_day(delivery_times, "A replay source batch")
    today = _oslo_today()
    historical = day < today
    needs_actual_wind = day <= today
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    jobs: dict[tuple[str, str], Callable[[], list[dict[str, Any]]]] = {}
    for area in NO_AREAS:
        jobs[("load_actual", area)] = lambda a=area: _query_document(a, day, "A65", "A16")
        if not historical:
            jobs[("load_forecast", area)] = lambda a=area: _query_document(a, day, "A65", "A01")
    for area in WIND_ZONES:
        if not historical:
            jobs[("renewable_forecast", area)] = lambda a=area: _query_document(a, day, "A69", "A01")
        if needs_actual_wind:
            jobs[("generation_actual", area)] = lambda a=area: _query_document(a, day, "A75", "A16")
    for area in ALL_BOUNDARY_AREAS:
        if area in AREA_EIC:
            jobs[("price", area)] = lambda a=area: _query_area(a, day, token)
    started = monotonic_time.perf_counter()
    results, errors = _run_entsoe_jobs(jobs, max_workers=max_workers)
    return {
        "results": results,
        "errors": errors,
        "historical_replay": historical,
        "timings": {"source_fetch_seconds": round(monotonic_time.perf_counter() - started, 6)},
    }


def _assemble_replay_mtu(
    delivery_start: datetime,
    cnec_zones: list[str],
    results: dict[tuple[str, str], list[dict[str, Any]]],
    errors: dict[str, str],
    *,
    include_generation_diagnostics: bool,
) -> dict[str, Any]:
    day = delivery_start.astimezone(OSLO_TZ).date()
    oslo_today = _oslo_today()
    historical_replay = day < oslo_today
    collect_actual_generation = include_generation_diagnostics or day <= oslo_today
    required_price_areas, unmapped_zones = _boundary_areas_for_zones(cnec_zones)
    relevant_errors = {
        *(f"load_actual:{area}" for area in NO_AREAS),
        *(f"load_forecast:{area}" for area in NO_AREAS if not historical_replay),
        *(f"renewable_forecast:{area}" for area in WIND_ZONES if not historical_replay),
        *(f"generation_actual:{area}" for area in WIND_ZONES if collect_actual_generation),
        *(f"price:{area}" for area in required_price_areas),
    }
    filtered_errors = {k: v for k, v in errors.items() if k in relevant_errors}

    demand_mw: dict[str, float] = {}
    load_inputs: dict[str, dict[str, Any]] = {}
    renewable_forecast_mw: dict[str, dict[str, float]] = {}
    renewable_forecast_inputs: dict[str, dict[str, Any]] = {}
    generator_available_capacity_mw: dict[str, float] = {}
    fixed_generation_mw_by_generator: dict[str, float] = {}
    observed_generation: dict[str, list[dict[str, Any]]] = {}

    for zone in NO_AREAS:
        chosen = _select_covering_record(results.get(("load_actual", zone), []), delivery_start)
        is_actual = chosen is not None
        if chosen is None and not historical_replay:
            chosen = _select_covering_record(results.get(("load_forecast", zone), []), delivery_start)
        if chosen is not None:
            available, _ = _available_at_timestamp(chosen, delivery_start, delivery_day=day)
            demand_mw[zone] = float(chosen["value_mw"])
            load_inputs[zone] = {
                "load_mw": float(chosen["value_mw"]),
                "timestamp_utc": _format_utc_z(chosen["timestamp"]),
                "resolution_minutes": int(chosen["interval_minutes"]),
                "document_type": "A65",
                "process_type": chosen["process_type"],
                "quality_status": "OBSERVED_ACTUAL" if is_actual else "PUBLISHED_FORECAST",
                "published_at_utc": chosen.get("published_at_utc"),
                "retrieved_at_utc": chosen.get("retrieved_at_utc"),
                "available_at_delivery_start": available,
                "unit": chosen["unit"],
            }

        # Renewables (A69/A01)
        renewable_records = results.get(("renewable_forecast", zone), [])
        selected_renewables: dict[str, dict[str, Any]] = {}
        for psr_type in {str(r.get("psr_type", "")) for r in renewable_records}:
            if psr_type in WIND_PSR_TYPES | SOLAR_PSR_TYPES:
                rec = _select_covering_record(
                    [r for r in renewable_records if str(r.get("psr_type", "")) == psr_type],
                    delivery_start,
                )
                if rec is not None:
                    selected_renewables[psr_type] = rec
        if selected_renewables:
            wind_val = sum(float(r["value_mw"]) for p, r in selected_renewables.items() if p in WIND_PSR_TYPES)
            solar_val = sum(float(r["value_mw"]) for p, r in selected_renewables.items() if p in SOLAR_PSR_TYPES)
            values: dict[str, float] = {}
            if any(p in WIND_PSR_TYPES for p in selected_renewables):
                values["wind"] = wind_val
            if any(p in SOLAR_PSR_TYPES for p in selected_renewables):
                values["solar"] = solar_val
            renewable_forecast_mw[zone] = values
            renewable_forecast_inputs[zone] = {
                "document_type": "A69",
                "process_type": "A01",
                "quality_status": "PUBLISHED_FORECAST",
                "series": {
                    psr: {
                        "timestamp_utc": _format_utc_z(rec["timestamp"]),
                        "resolution_minutes": int(rec["interval_minutes"]),
                        "published_at_utc": rec.get("published_at_utc"),
                        "retrieved_at_utc": rec.get("retrieved_at_utc"),
                        "unit": rec["unit"],
                    }
                    for psr, rec in selected_renewables.items()
                },
            }

        # Actual Generation (A75/A16)
        gen_records = results.get(("generation_actual", zone), [])
        observed_generation[zone] = [
            {
                "psr_type": rec["psr_type"],
                "generation_mw": float(rec["value_mw"]),
                "timestamp_utc": _format_utc_z(rec["timestamp"]),
                "resolution_minutes": int(rec["interval_minutes"]),
                "published_at_utc": rec.get("published_at_utc"),
                "retrieved_at_utc": rec.get("retrieved_at_utc"),
            }
            for rec in gen_records
            if rec["timestamp"] <= delivery_start < rec["timestamp"] + timedelta(minutes=int(rec["interval_minutes"]))
        ]
        wind_gen = [item for item in observed_generation[zone] if item["psr_type"] in WIND_PSR_TYPES]
        if wind_gen and zone in WIND_GENERATOR_BY_ZONE:
            fixed_generation_mw_by_generator[WIND_GENERATOR_BY_ZONE[zone]] = sum(
                float(item["generation_mw"]) for item in wind_gen
            )

    for zone, generator_id in WIND_GENERATOR_BY_ZONE.items():
        wind_forecast = renewable_forecast_mw.get(zone, {}).get("wind")
        if wind_forecast is not None:
            generator_available_capacity_mw[generator_id] = max(0.0, float(wind_forecast))

    # Boundary Prices (A44)
    price_observations = {
        area: selected
        for area in required_price_areas
        if (selected := _price_for_mtu(results.get(("price", area), []), delivery_start)) is not None
    }
    boundary_prices: dict[str, float] = {}
    boundary_price_area: dict[str, str] = {}
    boundary_price_inputs: dict[str, dict[str, Any]] = {}
    unresolved_price_zones: list[str] = [f"{zone}->UNMAPPED" for zone in unmapped_zones]
    for zone in cnec_zones:
        primary_area = _price_reference_for_zone(zone)
        if primary_area is None:
            continue
        selected_area = primary_area if primary_area in price_observations else next(
            (fb for fb in VIRTUAL_ZONE_PRICE_FALLBACKS.get(zone, ()) if fb in price_observations),
            None,
        )
        if selected_area is None:
            unresolved_price_zones.append(f"{zone}->{primary_area}")
            continue
        selected = price_observations[selected_area]
        available, _ = _available_at_timestamp(selected, delivery_start, delivery_day=day, document_type="A44")
        boundary_prices[zone] = float(selected["price_eur_mwh"])
        boundary_price_area[zone] = selected_area
        boundary_price_inputs[zone] = {
            **selected,
            "reference_area": selected_area,
            "requested_reference_area": primary_area,
            "used_declared_proxy": selected_area != primary_area,
            "source": "ENTSO-E A44 day-ahead price",
            "available_at_delivery_start": available,
        }

    missing_load = [zone for zone in NO_AREAS if zone not in demand_mw]
    missing_wind = [
        zone for zone, gen_id in WIND_GENERATOR_BY_ZONE.items()
        if "wind" not in renewable_forecast_mw.get(zone, {}) and gen_id not in fixed_generation_mw_by_generator
    ]
    unsafe_loads = [
        z for z, item in load_inputs.items()
        if item["quality_status"] == "PUBLISHED_FORECAST" and item.get("available_at_delivery_start") is not True
    ]
    unsafe_prices = [
        z for z, item in boundary_price_inputs.items()
        if item.get("available_at_delivery_start") is not True
    ]
    warnings: list[str] = []
    if unsafe_loads:
        warnings.append(
            "A65 A01-prognosen var ikke verifisert publisert innen MTU-start for: "
            + ", ".join(unsafe_loads)
            + ". Snapshotet kan brukes til replay/nowcast, men ikke som dokumentert prognoseinput ved MTU-start."
        )
    if unsafe_prices:
        warnings.append(
            "A44-randprisen var ikke verifisert publisert innen MTU-start for: "
            + ", ".join(unsafe_prices)
            + ". Verdiene kan ikke dokumenteres som tilgjengelige for en prognose ved MTU-start."
        )

    is_ready = not missing_load and not unresolved_price_zones and not missing_wind
    return {
        "status": "READY" if is_ready else "INCOMPLETE",
        "delivery_start_utc": _format_utc_z(delivery_start),
        "delivery_end_utc": _format_utc_z(delivery_start + timedelta(minutes=15)),
        "demand_mw": demand_mw,
        "load_inputs": load_inputs,
        "renewable_forecast_mw": renewable_forecast_mw,
        "renewable_forecast_inputs": renewable_forecast_inputs,
        "generator_available_capacity_mw": generator_available_capacity_mw,
        "fixed_generation_mw_by_generator": fixed_generation_mw_by_generator,
        "observed_generation_by_type": observed_generation,
        "boundary_prices_eur_mwh": boundary_prices,
        "boundary_price_area": boundary_price_area,
        "boundary_price_inputs": boundary_price_inputs,
        "price_observations": price_observations,
        "price_observation_scope": (
            "EXTERNAL_BOUNDARY_AREAS_ONLY; observed NO1-NO5 prices are supplied by the independent A44 chart feed"
        ),
        "required_boundary_zones": sorted(required_price_areas),
        "missing_load_areas": missing_load,
        "missing_renewable_forecast_areas": missing_wind,
        "missing_wind_availability_areas": missing_wind,
        "missing_boundary_price_zones": unresolved_price_zones,
        "forecast_origin_usable_at_mtu_start": not warnings,
        "forecast_origin_warnings": warnings,
        "source_errors": filtered_errors,
        "load_policy": (
            "A65_A16_REALIZED_LOAD_ONLY"
            if historical_replay
            else "A65_A16_REALIZED_LOAD_PREFERRED_THEN_A65_A01_FORECAST"
        ),
        "generation_policy": (
            "HISTORICAL_REPLAY_A75_A16_REALIZED_WIND_FIXED_AS_INJECTION"
            if historical_replay
            else "A75_A16_REALIZED_WIND_FIXED_WHEN_AVAILABLE_OTHERWISE_A69_A01_AVAILABILITY_CEILING"
        ),
        "missing_wind_policy": "BLOCK_SOLVE_NO_NAMEPLATE_CAPACITY_FALLBACK",
        "generation_diagnostics_included": collect_actual_generation,
    }


def fetch_flow_based_market_inputs_batch(
    delivery_times: list[datetime],
    cnec_zones_by_mtu: dict[str, list[str]],
    *,
    max_workers: int = 12,
    prefetched_sources: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Assemble every MTU from one shared day-sized replay source snapshot."""
    if not delivery_times:
        return {}
    periods, day = _single_oslo_day(delivery_times, "A replay input batch")
    started = monotonic_time.perf_counter()
    sources = prefetched_sources or prefetch_replay_market_sources(periods, max_workers=max_workers)
    source_seconds = float(
        sources.get("timings", {}).get("source_fetch_seconds", monotonic_time.perf_counter() - started)
    )
    include_generation = day <= _oslo_today()
    output: dict[str, dict[str, Any]] = {}
    for period in periods:
        key = _format_utc_z(period)
        selection_started = monotonic_time.perf_counter()
        snapshot = _assemble_replay_mtu(
            period,
            cnec_zones_by_mtu.get(key, []),
            sources.get("results", {}),
            sources.get("errors", {}),
            include_generation_diagnostics=include_generation,
        )
        snapshot["collection_timings"] = {
            "shared_source_fetch_seconds": round(source_seconds, 6),
            "mtu_selection_seconds": round(monotonic_time.perf_counter() - selection_started, 6),
        }
        output[key] = snapshot
    return output


def fetch_flow_based_market_inputs(
    delivery_time: datetime,
    cnec_zones: list[str],
    include_generation_diagnostics: bool = False,
) -> dict[str, Any]:
    """Fetch one time-aligned replay input snapshot for a single 15-minute MTU."""
    delivery_start = _quarter_hour(_utc(delivery_time))
    zone_key = ",".join(sorted({str(z) for z in cnec_zones}))
    cache_key = f"{delivery_start.isoformat()}|generation={include_generation_diagnostics}|zones={zone_key}"
    now = monotonic_time.monotonic()
    with _CACHE_LOCK:
        cached = _INPUT_CACHE.get(cache_key)
        if cached and cached[0] > now:
            _bump_cache_stat("input_snapshot_cache_hits")
            return cached[1]

    day = delivery_start.astimezone(OSLO_TZ).date()
    historical_replay = day < _oslo_today()
    collect_actual_generation = include_generation_diagnostics or day <= _oslo_today()
    required_price_areas, _ = _boundary_areas_for_zones(cnec_zones)
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    jobs: dict[tuple[str, str], Callable[[], list[dict[str, Any]]]] = {}
    for zone in NO_AREAS:
        jobs[("load_actual", zone)] = lambda z=zone: _query_document(z, day, "A65", "A16")
        if not historical_replay:
            jobs[("load_forecast", zone)] = lambda z=zone: _query_document(z, day, "A65", "A01")
    for zone in WIND_ZONES:
        if not historical_replay:
            jobs[("renewable_forecast", zone)] = lambda z=zone: _query_document(z, day, "A69", "A01")
        if collect_actual_generation:
            jobs[("generation_actual", zone)] = lambda z=zone: _query_document(z, day, "A75", "A16")
    for area in required_price_areas:
        jobs[("price", area)] = lambda a=area: _query_area(a, day, token)

    results, errors = _run_entsoe_jobs(jobs, max_workers=12)
    snapshot = _assemble_replay_mtu(
        delivery_start,
        cnec_zones,
        results,
        errors,
        include_generation_diagnostics=include_generation_diagnostics,
    )
    with _CACHE_LOCK:
        ttl = READY_SNAPSHOT_TTL_SECONDS if snapshot.get("status") == "READY" else INCOMPLETE_SNAPSHOT_TTL_SECONDS
        _INPUT_CACHE[cache_key] = (monotonic_time.monotonic() + ttl, snapshot)
        for expired_key in [k for k, v in _INPUT_CACHE.items() if v[0] <= now]:
            _INPUT_CACHE.pop(expired_key, None)
    return snapshot


# ---------------------------------------------------------------------------
# Strict Day-Ahead Forecast Loader (Target Day D+1 Known at Origin on Day D)
# ---------------------------------------------------------------------------


def _prefetch_forecast_sources(
    delivery_times: list[datetime], boundary_areas: set[str], *, max_workers: int = 12
) -> dict[str, Any]:
    """Fetch shared day-sized forecast source payloads once for a delivery day."""
    if not delivery_times:
        return {"results": {}, "errors": {}, "timings": {"source_fetch_seconds": 0.0}}
    _, target_day = _single_oslo_day(delivery_times, "A forecast source prefetch")
    lag_day = target_day - timedelta(days=1)
    token = os.environ.get("ENTSOE_API_TOKEN", "").strip()
    jobs: dict[tuple[str, str], Callable[[], list[dict[str, Any]]]] = {}
    for area in NO_AREAS:
        jobs[("load_forecast", area)] = lambda a=area: _query_document(a, target_day, "A65", "A01")
    for area in WIND_ZONES:
        jobs[("renewable_forecast", area)] = lambda a=area: _query_document(a, target_day, "A69", "A01")
        jobs[("renewable_forecast_lagged", area)] = lambda a=area: _query_document(a, lag_day, "A69", "A01")
    for area in boundary_areas:
        if area in AREA_EIC:
            jobs[("lagged_price", area)] = lambda a=area: _query_area(a, lag_day, token)
    started = monotonic_time.perf_counter()
    results, errors = _run_entsoe_jobs(jobs, max_workers=max_workers)
    return {
        "results": results,
        "errors": errors,
        "timings": {"source_fetch_seconds": round(monotonic_time.perf_counter() - started, 6)},
    }


def prefetch_day_ahead_forecast_sources(
    delivery_times: list[datetime], *, max_workers: int = 12
) -> dict[str, Any]:
    """Preload common A65/A69/A44 forecast sources before JAO zones finish loading."""
    return _prefetch_forecast_sources(delivery_times, set(ALL_BOUNDARY_AREAS), max_workers=max_workers)


def _assemble_forecast_mtu(
    delivery_start: datetime,
    origin: datetime,
    cnec_zones: list[str],
    results: dict[tuple[str, str], list[dict[str, Any]]],
    errors: dict[str, str],
) -> dict[str, Any]:
    if origin >= delivery_start:
        raise ValueError("forecast_origin must be earlier than the delivery MTU.")
    target_local = delivery_start.astimezone(OSLO_TZ)
    target_day = target_local.date()
    lag_local = target_local - timedelta(days=1)
    lag_timestamp = lag_local.astimezone(timezone.utc)
    lag_day = lag_local.date()
    required_price_areas, unmapped_zones = _boundary_areas_for_zones(cnec_zones)
    relevant_errors = {
        *(f"load_forecast:{area}" for area in NO_AREAS),
        *(f"renewable_forecast:{area}" for area in WIND_ZONES),
        *(f"renewable_forecast_lagged:{area}" for area in WIND_ZONES),
        *(f"lagged_price:{area}" for area in required_price_areas),
    }
    filtered_errors = {k: v for k, v in errors.items() if k in relevant_errors}

    # 1. Day-Ahead Load Forecast (A65/A01)
    demand_mw: dict[str, float] = {}
    load_inputs: dict[str, dict[str, Any]] = {}
    post_origin_load: list[dict[str, Any]] = []
    for area in NO_AREAS:
        candidates = results.get(("load_forecast", area), [])
        record = _select_covering_record(
            candidates,
            delivery_start,
            cutoff_utc=origin,
            delivery_day=target_day,
            document_type="A65",
        )
        if record is None:
            for candidate in candidates:
                avail, _ = _available_at_timestamp(candidate, origin, delivery_day=target_day, document_type="A65")
                if avail is False and candidate.get("published_at_utc"):
                    entry = {
                        "area": area,
                        "published_at_utc": candidate.get("published_at_utc"),
                        "document_type": "A65",
                        "process_type": "A01",
                    }
                    if entry not in post_origin_load:
                        post_origin_load.append(entry)
            continue
        available, basis = _available_at_timestamp(record, origin, delivery_day=target_day, document_type="A65")
        demand_mw[area] = float(record["value_mw"])
        load_inputs[area] = {
            "load_mw": float(record["value_mw"]),
            "timestamp_utc": _format_utc_z(record["timestamp"]),
            "resolution_minutes": int(record["interval_minutes"]),
            "document_type": "A65",
            "process_type": "A01",
            "quality_status": "PUBLISHED_FORECAST",
            "published_at_utc": record.get("published_at_utc"),
            "retrieved_at_utc": record.get("retrieved_at_utc"),
            "available_at_forecast_origin": available is True,
            "availability_basis": basis,
            "unit": record.get("unit", "MAW"),
        }

    # 2. Day-Ahead Wind/Solar Forecast (A69/A01 target day, fallback to D-1 same local MTU)
    renewable_forecast_mw: dict[str, dict[str, float]] = {}
    renewable_forecast_inputs: dict[str, dict[str, Any]] = {}
    generator_available_capacity_mw: dict[str, float] = {}

    def _pick_renewables(recs: list[dict[str, Any]], ts: datetime, d_day: date) -> dict[str, dict[str, Any]]:
        chosen: dict[str, dict[str, Any]] = {}
        for psr in WIND_PSR_TYPES | SOLAR_PSR_TYPES:
            rec = _select_covering_record(
                [r for r in recs if str(r.get("psr_type", "")) == psr],
                ts,
                cutoff_utc=origin,
                delivery_day=d_day,
                document_type="A69",
            )
            if rec is not None:
                chosen[psr] = rec
        return chosen

    for area, generator_id in WIND_GENERATOR_BY_ZONE.items():
        chosen_by_type = _pick_renewables(results.get(("renewable_forecast", area), []), delivery_start, target_day)
        forecast_method = "A69_A01_TARGET_DELIVERY_DAY"
        effective_day = target_day
        if not any(psr in WIND_PSR_TYPES for psr in chosen_by_type):
            chosen_by_type = _pick_renewables(
                results.get(("renewable_forecast_lagged", area), []), lag_timestamp, lag_day
            )
            forecast_method = "LAGGED_A69_A01_PREVIOUS_OSLO_DAY_SAME_LOCAL_MTU"
            effective_day = lag_day
        if chosen_by_type:
            values: dict[str, float] = {}
            wind_items = [item for psr, item in chosen_by_type.items() if psr in WIND_PSR_TYPES]
            solar_items = [item for psr, item in chosen_by_type.items() if psr in SOLAR_PSR_TYPES]
            if wind_items:
                values["wind"] = sum(float(item["value_mw"]) for item in wind_items)
                generator_available_capacity_mw[generator_id] = max(0.0, values["wind"])
            if solar_items:
                values["solar"] = sum(float(item["value_mw"]) for item in solar_items)
            renewable_forecast_mw[area] = values
            renewable_forecast_inputs[area] = {
                "document_type": "A69",
                "process_type": "A01",
                "quality_status": "PUBLISHED_FORECAST",
                "forecast_method": forecast_method,
                "available_at_forecast_origin": all(
                    _available_at_timestamp(item, origin, delivery_day=effective_day, document_type="A69")[0] is True
                    for item in chosen_by_type.values()
                ),
                "series": {
                    psr: {
                        "timestamp_utc": _format_utc_z(item["timestamp"]),
                        "resolution_minutes": int(item["interval_minutes"]),
                        "published_at_utc": item.get("published_at_utc"),
                        "retrieved_at_utc": item.get("retrieved_at_utc"),
                        "unit": item.get("unit", "MAW"),
                    }
                    for psr, item in chosen_by_type.items()
                },
            }

    # 3. Lagged Boundary Price Proxy (Previous Oslo Day A44 Same Local MTU)
    boundary_price_forecasts: dict[str, float] = {}
    boundary_price_inputs: dict[str, dict[str, Any]] = {}
    missing_boundary: list[str] = [f"{zone}->UNMAPPED" for zone in unmapped_zones]
    for zone in cnec_zones:
        primary_area = _price_reference_for_zone(zone)
        if primary_area is None:
            continue
        selected_area: str | None = None
        price_record: dict[str, Any] | None = None
        for candidate_area in (primary_area, *VIRTUAL_ZONE_PRICE_FALLBACKS.get(zone, ())):
            price_record = _price_for_mtu(
                results.get(("lagged_price", candidate_area), []),
                lag_timestamp,
                forecast_origin=origin,
                delivery_day=lag_day,
            )
            if price_record is not None:
                selected_area = candidate_area
                break
        if price_record is None or selected_area is None:
            raw_exists = _price_for_mtu(results.get(("lagged_price", primary_area), []), lag_timestamp) is not None
            suffix = ":NOT_AVAILABLE_AT_ORIGIN" if raw_exists else ""
            missing_boundary.append(f"{zone}->{primary_area}{suffix}")
            continue
        available, basis = _available_at_timestamp(price_record, origin, delivery_day=lag_day, document_type="A44")
        boundary_price_forecasts[zone] = float(price_record["price_eur_mwh"])
        boundary_price_inputs[zone] = {
            **price_record,
            "reference_area": selected_area,
            "requested_reference_area": primary_area,
            "used_declared_proxy": selected_area != primary_area,
            "source": "ENTSO-E A44 previous delivery day",
            "forecast_method": "LAGGED_A44_SAME_LOCAL_MTUD1",
            "forecast_for_local_time": target_local.strftime("%H:%M"),
            "lagged_delivery_local_date": lag_day.isoformat(),
            "available_at_forecast_origin": available is True,
            "availability_basis": basis,
        }

    missing_load = [area for area in NO_AREAS if area not in demand_mw]
    missing_wind = [
        area for area, gen_id in WIND_GENERATOR_BY_ZONE.items()
        if gen_id not in generator_available_capacity_mw
    ]
    origin_warnings: list[str] = []
    if missing_load:
        origin_warnings.append("Missing A65/A01 load forecasts: " + ", ".join(missing_load))
    if missing_wind:
        origin_warnings.append("Missing A69/A01 wind forecasts: " + ", ".join(missing_wind))
    if missing_boundary:
        origin_warnings.append("Missing or unavailable lagged A44 boundary proxies: " + ", ".join(missing_boundary))

    usable = not missing_load and not missing_boundary and not missing_wind
    return {
        "status": "READY" if usable else "INCOMPLETE",
        "forecast_origin_usable": usable,
        "forecast_origin_utc": _format_utc_z(origin),
        "delivery_start_utc": _format_utc_z(delivery_start),
        "delivery_end_utc": _format_utc_z(delivery_start + timedelta(minutes=15)),
        "delivery_day_local": target_day.isoformat(),
        "demand_mw": demand_mw,
        "load_inputs": load_inputs,
        "renewable_forecast_mw": renewable_forecast_mw,
        "renewable_forecast_inputs": renewable_forecast_inputs,
        "generator_available_capacity_mw": generator_available_capacity_mw,
        "boundary_price_forecasts_eur_mwh": boundary_price_forecasts,
        "boundary_price_inputs": boundary_price_inputs,
        "missing_load_areas": missing_load,
        "missing_wind_forecast_areas": missing_wind,
        "missing_boundary_price_zones": missing_boundary,
        "source_errors": filtered_errors,
        "load_policy": "A65_A01_DAY_AHEAD_FORECAST_ONLY",
        "renewable_policy": "A69_A01_TARGET_DAY_IF_AVAILABLE_ELSE_PREVIOUS_OSLO_DAY_SAME_LOCAL_MTU",
        "boundary_price_policy": "PREVIOUS_OSLO_DELIVERY_DAY_A44_SAME_LOCAL_MTU_PROXY",
        "forecast_origin_warnings": origin_warnings,
        "post_origin_load_diagnostics": post_origin_load,
        "replay_mode_available": bool(post_origin_load),
    }


def fetch_flow_based_forecast_inputs_batch(
    delivery_times: list[datetime],
    forecast_origin: datetime,
    cnec_zones_by_mtu: dict[str, list[str]],
    *,
    prefetched_sources: dict[str, Any] | None = None,
    max_workers: int = 12,
) -> dict[str, dict[str, Any]]:
    """Fetch one Oslo delivery day's strict day-ahead forecast inputs in batch."""
    if not delivery_times:
        return {}
    origin = _utc(forecast_origin)
    periods, _ = _single_oslo_day(delivery_times, "A forecast input batch")
    reference_areas: set[str] = set()
    for period in periods:
        areas, _ = _boundary_areas_for_zones(cnec_zones_by_mtu.get(_format_utc_z(period), []))
        reference_areas.update(areas)
    started = monotonic_time.perf_counter()
    if prefetched_sources is None:
        prefetched_sources = _prefetch_forecast_sources(periods, reference_areas, max_workers=max_workers)
    source_timings = prefetched_sources.get("timings", {})
    assembled: dict[str, dict[str, Any]] = {}
    for period in periods:
        key = _format_utc_z(period)
        market = _assemble_forecast_mtu(
            period,
            origin,
            cnec_zones_by_mtu.get(key, []),
            prefetched_sources.get("results", {}),
            prefetched_sources.get("errors", {}),
        )
        market["collection_timings"] = {
            "shared_source_fetch_seconds": source_timings.get("source_fetch_seconds"),
            "mtu_selection_seconds": round(monotonic_time.perf_counter() - started, 6),
        }
        assembled[key] = market
    return assembled


def fetch_flow_based_forecast_inputs(
    delivery_time: datetime,
    forecast_origin: datetime,
    cnec_zones: list[str],
    *,
    _prefetched_results: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    _prefetched_errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch strict day-ahead forecast inputs for one 15-minute MTU."""
    delivery_start = _quarter_hour(_utc(delivery_time))
    origin = _utc(forecast_origin)
    if _prefetched_results is not None:
        return _assemble_forecast_mtu(
            delivery_start,
            origin,
            cnec_zones,
            _prefetched_results,
            _prefetched_errors or {},
        )
    key = _format_utc_z(delivery_start)
    return fetch_flow_based_forecast_inputs_batch(
        [delivery_start],
        origin,
        {key: cnec_zones},
        max_workers=8,
    )[key]