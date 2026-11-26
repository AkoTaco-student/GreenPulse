"""ENTSO-E day-ahead price client and XML normalizer (API token required)."""

from __future__ import annotations

import os
import math
import hashlib
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import threading
import time as monotonic_time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
from services.api.http_client import env_float, env_int, read_url

ENTSOE_ENDPOINT = os.getenv(
    "greenpulse_ENTSOE_API_URL", "https://web-api.tp.entsoe.eu/api"
)
AREA_EIC = {
    "NO1": os.getenv("ENTSOE_EIC_NO1", "10YNO-1--------2"),
    "NO2": os.getenv("ENTSOE_EIC_NO2", "10YNO-2--------T"),
    "NO3": os.getenv("ENTSOE_EIC_NO3", "10YNO-3--------J"),
    "NO4": os.getenv("ENTSOE_EIC_NO4", "10YNO-4--------9"),
    "NO5": os.getenv("ENTSOE_EIC_NO5", "10Y1001A1001A48H"),
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
    "FI": "10YFI-1--------U",
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "DE_LU": "10Y1001A1001A82H",
    "NL": "10YNL----------L",
    "EE": "10Y1001A1001A39I",
    "LT": "10YLT-1001A0008Q",
    "PL": "10YPL-AREA-----S",
}
NORWEGIAN_AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5")
NORDIC_MARKET_AREAS = ("SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2")
BOUNDARY_PRICE_AREAS = NORDIC_MARKET_AREAS + ("DE_LU", "NL", "EE", "LT", "PL")
_PRICE_CACHE_TTL_SECONDS = 300.0
_PRICE_EMPTY_CACHE_TTL_SECONDS = 20.0
_PRICE_FAILURE_BACKOFF_SECONDS = 45.0
ENTSOE_TIMEOUT_SECONDS = env_float("GREENPULSE_ENTSOE_TIMEOUT_SECONDS", 75.0)
ENTSOE_RETRIES = env_int("GREENPULSE_ENTSOE_RETRIES", 2, maximum=4)
_PRICE_FAILURE_CACHE: dict[tuple[str, str, str, str, str], tuple[float, str]] = {}
_PRICE_CACHE: dict[tuple[str, str, str, str, str], tuple[float, list[dict[str, Any]]]] = {}
_PRICE_INFLIGHT: dict[tuple[str, str, str, str, str], Future[list[dict[str, Any]]]] = {}
_PRICE_CACHE_LOCK = threading.Lock()
_PRICE_STATS_LOCK = threading.Lock()
_PRICE_STATS = {
    "a44_cache_hits": 0,
    "a44_fetches": 0,
    "a44_inflight_waits": 0,
    "a44_http_requests": 0,
    "a44_http_elapsed_seconds": 0.0,
}


def price_cache_stats() -> dict[str, int | float]:
    with _PRICE_STATS_LOCK:
        return dict(_PRICE_STATS)


def _bump_price_stat(name: str) -> None:
    with _PRICE_STATS_LOCK:
        _PRICE_STATS[name] += 1


def _add_price_time(name: str, seconds: float) -> None:
    with _PRICE_STATS_LOCK:
        _PRICE_STATS[name] += seconds


def _price_cache_key(area: str, day: date, token: str) -> tuple[str, str, str, str, str]:
    """Separate A44 results whenever upstream, credentials, or area mapping changes."""
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return (area, day.isoformat(), ENTSOE_ENDPOINT, AREA_EIC[area], token_digest)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_publication_dt(value: Any) -> datetime | None:
    if not isinstance(value, (str, datetime)):
        return None
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_revision_number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _market_day_bounds_utc(day: date) -> tuple[datetime, datetime]:
    """Return the exact Oslo local delivery day in UTC, including DST days."""
    oslo = ZoneInfo("Europe/Oslo")
    local_start = datetime.combine(day, time.min, tzinfo=oslo)
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=oslo)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)


def _records_in_market_day(
    records: list[dict[str, Any]],
    day: date,
) -> list[dict[str, Any]]:
    """Keep one requested Oslo delivery day and collapse duplicate revisions by MTU."""
    start, end = _market_day_bounds_utc(day)
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        try:
            timestamp = _timestamp(str(record["timestamp"]))
            area = str(record["area"])
            interval = int(record.get("resolution_minutes", 60))
            price = float(record["price_eur_mwh"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (start <= timestamp < end) or not math.isfinite(price):
            continue
        key = (area, timestamp.isoformat())
        previous = selected.get(key)
        if previous is None:
            selected[key] = {**record, "timestamp": timestamp.isoformat().replace("+00:00", "Z")}
        else:
            prev_pub = _parse_publication_dt(previous.get("published_at_utc"))
            curr_pub = _parse_publication_dt(record.get("published_at_utc"))
            is_better = False
            if curr_pub is not None and prev_pub is not None:
                if curr_pub > prev_pub:
                    is_better = True
                elif curr_pub == prev_pub:
                    is_better = _parse_revision_number(record.get("document_revision_number")) >= _parse_revision_number(previous.get("document_revision_number"))
            elif curr_pub is not None and prev_pub is None:
                is_better = True
            elif curr_pub is None and prev_pub is None:
                is_better = _parse_revision_number(record.get("document_revision_number")) >= _parse_revision_number(previous.get("document_revision_number"))
            if is_better:
                selected[key] = {**record, "timestamp": timestamp.isoformat().replace("+00:00", "Z")}
    return sorted(selected.values(), key=lambda row: (row["timestamp"], row["area"]))


def _query_area_uncached(area: str, day: date, token: str) -> list[dict[str, Any]]:
    start, end = _market_day_bounds_utc(day)
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": AREA_EIC[area],
        "out_Domain": AREA_EIC[area],
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
        "contract_MarketAgreement.type": "A01",
    }
    url = f"{ENTSOE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "greenpulse/0.1 (market data observatory)"},
    )
    request_started = monotonic_time.perf_counter()
    try:
        _bump_price_stat("a44_http_requests")
        body = read_url(
            request,
            timeout_seconds=ENTSOE_TIMEOUT_SECONDS,
            retries=ENTSOE_RETRIES,
            source_label=f"ENTSO-E A44 {area}",
        )
    except RuntimeError as error:
        raise RuntimeError(f"Klarte ikke å hente data fra ENTSO-E: {error}") from error
    finally:
        _add_price_time("a44_http_elapsed_seconds", monotonic_time.perf_counter() - request_started)

    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise RuntimeError("ENTSO-E returnerte ugyldig XML.") from error

    records: list[dict[str, Any]] = []
    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    document_created_at = root.findtext(".//{*}createdDateTime")
    document_id = root.findtext(".//{*}mRID")
    document_revision = root.findtext(".//{*}revisionNumber")
    published_at_raw = root.findtext(".//{*}createdDateTime")
    parsed_pub = _parse_publication_dt(published_at_raw)
    published_at_utc = parsed_pub.isoformat().replace("+00:00", "Z") if parsed_pub else None
    for series in root.findall(".//{*}TimeSeries"):
        curve_type = (series.findtext("{*}curveType") or series.findtext(".//{*}curveType") or "").strip().upper()
        for period in series.findall("{*}Period"):
            interval = period.find("{*}timeInterval")
            start_el = interval.find("{*}start") if interval is not None else None
            end_el = interval.find("{*}end") if interval is not None else None
            resolution_el = period.find("{*}resolution")
            if start_el is None or not start_el.text or resolution_el is None:
                continue
            period_start = _timestamp(start_el.text)
            period_end = _timestamp(end_el.text) if end_el is not None and end_el.text else None
            resolution = resolution_el.text or "PT60M"
            step_minutes = 15 if resolution == "PT15M" else 60 if resolution == "PT60M" else 0
            if step_minutes == 0:
                continue

            total_positions: int | None = None
            if period_end is not None and period_end > period_start:
                step_seconds = step_minutes * 60
                span_seconds = int((period_end - period_start).total_seconds())
                if span_seconds >= step_seconds:
                    total_positions = span_seconds // step_seconds

            parsed_points: list[tuple[int, float]] = []
            for point in period.findall("{*}Point"):
                position_el = point.find("{*}position")
                amount_el = point.find("{*}price.amount")
                if position_el is None or amount_el is None:
                    continue
                try:
                    position = int(position_el.text or "0")
                    price = float(amount_el.text or "nan")
                except ValueError:
                    continue
                if position < 1:
                    continue
                parsed_points.append((position, price))

            parsed_points.sort(key=lambda item: item[0])
            for idx, (position, price) in enumerate(parsed_points):
                if curve_type == "A03":
                    if idx + 1 < len(parsed_points):
                        end_pos = parsed_points[idx + 1][0]
                    elif total_positions is not None and total_positions >= position:
                        end_pos = total_positions + 1
                    else:
                        end_pos = position + 1
                else:
                    end_pos = position + 1

                for expanded_pos in range(position, end_pos):
                    timestamp = period_start + timedelta(minutes=step_minutes * (expanded_pos - 1))
                    if not start <= timestamp < end:
                        continue
                    records.append({
                        "area": area,
                        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                        "price_eur_mwh": price,
                        "unit": "EUR/MWh",
                        "resolution_minutes": step_minutes,
                        "source": "ENTSO-E Transparency Platform",
                        "document_type": "A44",
                        "quality_status": "PUBLISHED",
                        "document_created_at_utc": document_created_at,
                        "document_id": document_id,
                        "document_revision_number": document_revision,
                        "retrieved_at_utc": retrieved_at,
                        "published_at_utc": published_at_utc,
                    })
    return records


def _query_area(area: str, day: date, token: str) -> list[dict[str, Any]]:
    """Share one A44 request/result between the monitor and dispatch routes."""
    key = _price_cache_key(area, day, token)
    now = monotonic_time.monotonic()
    with _PRICE_CACHE_LOCK:
        cached = _PRICE_CACHE.get(key)
        if cached and cached[0] > now:
            _bump_price_stat("a44_cache_hits")
            return [dict(r) for r in cached[1]]
        failure = _PRICE_FAILURE_CACHE.get(key)
        if failure and failure[0] > now:
            raise RuntimeError(
                f"ENTSO-E A44 request temporarily suppressed after a recent upstream failure: {failure[1]}"
            )
        if failure:
            _PRICE_FAILURE_CACHE.pop(key, None)
        future = _PRICE_INFLIGHT.get(key)
        is_owner = future is None
        if future is None:
            future = Future()
            _PRICE_INFLIGHT[key] = future

    if not is_owner:
        _bump_price_stat("a44_inflight_waits")
        return [dict(r) for r in future.result()]

    try:
        _bump_price_stat("a44_fetches")
        records = _query_area_uncached(area, day, token)
        ttl = _PRICE_CACHE_TTL_SECONDS if records else _PRICE_EMPTY_CACHE_TTL_SECONDS
        with _PRICE_CACHE_LOCK:
            _PRICE_CACHE[key] = (monotonic_time.monotonic() + ttl, records)
        future.set_result(records)
        return [dict(r) for r in records]
    except Exception as error:
        with _PRICE_CACHE_LOCK:
            _PRICE_FAILURE_CACHE[key] = (
                monotonic_time.monotonic() + _PRICE_FAILURE_BACKOFF_SECONDS,
                f"{type(error).__name__}: {error}",
            )
        future.set_exception(error)
        raise
    finally:
        with _PRICE_CACHE_LOCK:
            _PRICE_INFLIGHT.pop(key, None)


import json
from pathlib import Path
from services.api.forecast_calendar import delivery_periods

FIXTURES_PATH = Path(__file__).resolve().parents[2] / "data" / "processed" / "entsoe_market_sample_7d.json"


def _get_fallback_snapshot() -> list[dict[str, Any]]:
    """Return local validated snapshot from Fase 0 fixtures when live API is unavailable."""
    if not FIXTURES_PATH.exists():
        return []
    try:
        with FIXTURES_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        snapshot = data.get("current_snapshot", {})
        prices = snapshot.get("prices_eur_mwh", {})
        ts = snapshot.get("timestamp_utc", datetime.now(timezone.utc).isoformat())
        records = []
        for area, price in prices.items():
            records.append({
                "area": area,
                "timestamp": ts,
                "price_eur_mwh": float(price),
                "unit": "EUR/MWh",
                "resolution_minutes": 15,
                "source": "Fase 0 Verified Fixture (STALE_CACHE)",
                "document_type": "A44",
                "quality_status": "STALE_CACHE",
                "published_at_utc": None,
            })
        return records
    except Exception:
        return []


def get_day_ahead_prices(day: date) -> dict[str, Any]:
    """Hent og normaliser day-ahead-priser for fem norske budområder med eksplisitt full-day validering."""
    expected_periods = delivery_periods(day)
    expected_interval_count = len(expected_periods)
    expected_timestamps = {p.isoformat().replace("+00:00", "Z") for p in expected_periods}
    market_areas = NORWEGIAN_AREAS

    token = os.getenv("ENTSOE_API_TOKEN", "").strip()
    if not token:
        fallback_records = _get_fallback_snapshot()
        return {
            "available": bool(fallback_records),
            "source": "ENTSO-E Transparency Platform / Fase 0 Snapshot",
            "message": "ENTSOE_API_TOKEN ikke satt; benytter verifisert Fase 0-snapshot (STALE_CACHE). Dette snapshotet inneholder kun enkeltpunkter og representerer ikke en full dag.",
            "records": fallback_records,
            "data_status": "STALE_CACHE",
            "completeness": "INCOMPLETE",
            "quality_status": "STALE_CACHE" if fallback_records else "EMPTY",
            "day_utc": day.isoformat(),
            "expected_interval_count": expected_interval_count,
            "coverage_by_area": {area: (1 if any(r["area"] == area for r in fallback_records) else 0) for area in market_areas},
        }

    records: list[dict[str, Any]] = []
    area_errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(5, len(market_areas))) as executor:
        requests = {
            executor.submit(_query_area, area, day, token): area
            for area in market_areas
        }
        for future in as_completed(requests):
            area = requests[future]
            try:
                records.extend(future.result())
            except RuntimeError as error:
                area_errors[area] = str(error)

    records = _records_in_market_day(records, day)

    area_records: dict[str, set[str]] = {area: set() for area in market_areas}
    for r in records:
        if r["area"] in area_records:
            area_records[r["area"]].add(r["timestamp"])

    if not records and not area_errors:
        fallback_records = _get_fallback_snapshot()
        return {
            "available": bool(fallback_records),
            "source": "ENTSO-E Transparency Platform / Fase 0 Snapshot",
            "message": "ENTSO-E returnerte ingen priser for dagen; benytter verifisert Fase 0-snapshot (STALE_CACHE). Dette snapshotet representerer ikke en full dag.",
            "records": fallback_records,
            "area_errors": area_errors,
            "data_status": "STALE_CACHE",
            "completeness": "INCOMPLETE",
            "quality_status": "STALE_CACHE" if fallback_records else "EMPTY",
            "day_utc": day.isoformat(),
            "expected_interval_count": expected_interval_count,
            "coverage_by_area": {area: (1 if any(r["area"] == area for r in fallback_records) else 0) for area in market_areas},
        }

    if len(area_errors) == len(market_areas):
        data_status = "ERROR"
        completeness = "INCOMPLETE"
        quality_status = "ERROR"
        message = "Alle ENTSO-E-spørringer feilet."
    elif area_errors:
        data_status = "PARTIAL"
        completeness = "INCOMPLETE"
        quality_status = "PARTIAL"
        message = f"ENTSO-E-feil for {len(area_errors)} område(r)."
    else:
        complete_areas = [area for area, ts_set in area_records.items() if expected_timestamps.issubset(ts_set)]
        is_complete = len(complete_areas) == len(market_areas)
        data_status = "LIVE"
        completeness = "COMPLETE" if is_complete else "INCOMPLETE"
        quality_status = "PUBLISHED" if is_complete else "PARTIAL"
        message = None if is_complete else f"Ufullstendig tidsserie: {len(complete_areas)}/{len(market_areas)} områder har alle {expected_interval_count} forventede MTUer."

    return {
        "available": bool(records),
        "source": "ENTSO-E Transparency Platform",
        "document_type": "A44",
        "day_utc": day.isoformat(),
        "records": records,
        "area_errors": area_errors,
        "data_status": data_status,
        "completeness": completeness,
        "quality_status": quality_status,
        "expected_interval_count": expected_interval_count,
        "coverage_by_area": {area: len(ts_set) for area, ts_set in area_records.items()},
        "message": message,
    }
