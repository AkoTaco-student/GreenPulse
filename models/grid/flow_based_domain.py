"""Retrieve and validate the published Nordic JAO flow-based domain.

JAO's ``lastModifiedOn`` is response-envelope metadata, not a CNEC-row
field. The monitoring endpoint confirms publication of the CNE Flowbased
Domain for the Oslo business day. One paginated delivery-day request is
validated before its CNECs are grouped and shared across the day's MTUs.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from services.api.http_client import env_float, env_int, read_url
from models.grid.jao_domain_normalization import (
    finite_float as _parse_finite_float,
    iso_z as _iso_z,
    normalize_jao_records,
    parse_jao_utc as _parse_utc,
)
from models.grid.jao_domain_diagnostics import check_domain_feasibility
from models.grid.jao_monitoring import (
    MONITOR_CACHE as _MONITOR_CACHE,
    MONITOR_LOCK as _MONITOR_LOCK,
    is_flow_based_domain_entry as _is_flow_based_domain_entry,
    monitoring_rows as _monitoring_rows,
    normalized_page_name as _normalized_page_name,
    monitoring_status_for_day as _jao_monitoring_status_for_day,
)

NO_ZONES = ("NO1", "NO2", "NO3", "NO4", "NO5")
BASE_EXTERNAL_ZONES = ("SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2")
ALL_NORDIC_ZONES = NO_ZONES + BASE_EXTERNAL_ZONES
JAO_BASE_URL = "https://publicationtool.jao.eu/nordic/api"
JAO_DOMAIN_URL = f"{JAO_BASE_URL}/data/finalComputation"
JAO_MONITORING_URL = f"{JAO_BASE_URL}/system/monitoring"
JAO_TIMEOUT_SECONDS = env_float("GREENPULSE_JAO_TIMEOUT_SECONDS", 90.0)
JAO_RETRIES = env_int("GREENPULSE_JAO_RETRIES", 2, maximum=4)
# Keep a conservative default. This can be tuned locally after checking JAO's
# current rate policy; do not reduce it blindly to mask slow source collection.
JAO_MIN_REQUEST_INTERVAL_SECONDS = max(
    0.1, env_float("GREENPULSE_JAO_MIN_REQUEST_INTERVAL_SECONDS", 0.70)
)
# The JAO endpoint supports large Take values. A delivery-day batch typically
# fits in 2-4 pages, instead of one serialized request for every UTC hour.
JAO_PAGE_SIZE = env_int("GREENPULSE_JAO_PAGE_SIZE", 50000, maximum=100000)
JAO_MAX_PAGES = env_int("GREENPULSE_JAO_MAX_PAGES", 24, maximum=40)
DAY_CACHE_TTL_SECONDS = 300.0
HOUR_CACHE_TTL_SECONDS = 300.0  # retained for the single-hour compatibility path
MONITOR_CACHE_TTL_SECONDS = 60.0

# Serialize actual HTTP operations and enforce a minimum interval between them.
_API_REQUEST_LOCK = threading.Lock()
_API_LAST_REQUEST_FINISHED = 0.0
_HOUR_LOCK = threading.Lock()
_HOUR_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_HOUR_INFLIGHT: dict[str, Future[dict[str, Any]]] = {}
_DAY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_DAY_INFLIGHT: dict[str, Future[dict[str, Any]]] = {}
_CACHE_STATS_LOCK = threading.Lock()
_CACHE_STATS = {
    "jao_http_requests": 0,
    "jao_domain_page_requests": 0,
    "jao_monitor_requests": 0,
    "jao_serialized_wait_seconds": 0.0,
    "jao_http_elapsed_seconds": 0.0,
    "monitor_cache_hits": 0,
    "hour_cache_hits": 0,
    "hour_fetches": 0,
    "hour_inflight_waits": 0,
    "day_cache_hits": 0,
    "day_fetches": 0,
    "day_inflight_waits": 0,
}


def flow_based_cache_stats() -> dict[str, int | float]:
    with _CACHE_STATS_LOCK:
        return dict(_CACHE_STATS)


def _bump_cache_stat(name: str) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[name] += 1


def _add_cache_time(name: str, seconds: float) -> None:
    with _CACHE_STATS_LOCK:
        _CACHE_STATS[name] += seconds


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Delivery/origin timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _parse_publication_utc(value: Any) -> datetime | None:
    """Parse response-level publication metadata only when timezone-aware."""
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _quarter_hour_start(value: datetime) -> datetime:
    value = _utc(value)
    return value.replace(minute=(value.minute // 15) * 15, second=0, microsecond=0)


def _parse_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _request_json(
    url: str,
    query: dict[str, str],
    *,
    source_label: str,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
) -> Any:
    """Issue one serialized JAO GET request with bounded retry behaviour."""
    global _API_LAST_REQUEST_FINISHED
    request_url = f"{url}?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(
        request_url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "greenpulse/1.3 (JAO publication data client)",
        },
    )
    serialized_started = time.perf_counter()
    with _API_REQUEST_LOCK:
        wait = JAO_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _API_LAST_REQUEST_FINISHED)
        if wait > 0:
            time.sleep(wait)
        _add_cache_time("jao_serialized_wait_seconds", time.perf_counter() - serialized_started)
        try:
            _bump_cache_stat("jao_http_requests")
            if source_label.startswith("JAO Nordic finalComputation"):
                _bump_cache_stat("jao_domain_page_requests")
            elif source_label == "JAO publication monitoring":
                _bump_cache_stat("jao_monitor_requests")
            request_started = time.perf_counter()
            body = read_url(
                request,
                timeout_seconds=timeout_seconds,
                retries=JAO_RETRIES,
                source_label=source_label,
            )
        finally:
            if "request_started" in locals():
                _add_cache_time("jao_http_elapsed_seconds", time.perf_counter() - request_started)
            _API_LAST_REQUEST_FINISHED = time.monotonic()

    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError as error:
            raise RuntimeError(f"{source_label} returned invalid gzip data.") from error
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{source_label} returned invalid JSON.") from error
    if isinstance(payload, dict) and payload.get("rejected"):
        messages = payload.get("messages")
        raise RuntimeError(f"{source_label} request rejected: {messages or 'no message supplied'}")
    if not isinstance(payload, (dict, list)):
        raise RuntimeError(f"{source_label} returned an unexpected JSON response type.")
    return payload


def _monitoring_status_for_day(
    day_start_utc: datetime,
    day_end_utc: datetime,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Read JAO publication status; optionally bypass the short-lived cache.

    The full-day page collector uses a fresh read both before and after paging.
    This checks whether the *published CNE Flowbased Domain* changed while the
    pages were being assembled, without incorrectly requiring response-envelope
    timestamps from separate HTTP requests to be identical.
    """
    return _jao_monitoring_status_for_day(
        day_start_utc,
        day_end_utc,
        request_json=_request_json,
        monitoring_url=JAO_MONITORING_URL,
        iso_z=_iso_z,
        cache_ttl_seconds=0.0 if force_refresh else MONITOR_CACHE_TTL_SECONDS,
        on_cache_hit=None if force_refresh else lambda: _bump_cache_stat("monitor_cache_hits"),
        force_refresh=force_refresh,
    )


def _monitoring_publication_signature(monitoring: dict[str, Any]) -> tuple[Any, ...] | None:
    """Stable identity for the received CNE Flowbased Domain publication."""
    if str(monitoring.get("status", "")).casefold() != "received":
        return None
    entries = monitoring.get("entries")
    if not isinstance(entries, list) or not entries:
        return None

    signature: list[tuple[Any, ...]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        modified = _parse_publication_utc(entry.get("lastModifiedOn"))
        if modified is None:
            return None
        pages_value = entry.get("pages", [])
        if isinstance(pages_value, str):
            pages = [pages_value]
        elif isinstance(pages_value, list):
            pages = pages_value
        else:
            pages = []
        normalized_pages = tuple(sorted(_normalized_page_name(value) for value in pages))
        signature.append((
            str(entry.get("fileType", "")).strip().upper(),
            normalized_pages,
            str(entry.get("status", "")).strip().casefold(),
            _iso_z(modified, milliseconds=True),
        ))

    return tuple(sorted(signature)) if signature else None


def _apply_monitoring_publication_vintage(
    day_data: dict[str, Any], monitoring: dict[str, Any]
) -> dict[str, Any]:
    """Use stable monitoring metadata as the publication-version guard.

    finalComputation's response-level lastModifiedOn is collected per page and
    can differ slightly between requests. The monitoring publication timestamp
    identifies the received CNE Flowbased Domain. Use the later of the two
    timestamps as a conservative forecast-vintage cutoff.
    """
    monitor_times = [
        parsed
        for entry in (monitoring.get("entries") or [])
        if isinstance(entry, dict)
        for parsed in [_parse_publication_utc(entry.get("lastModifiedOn"))]
        if parsed is not None
    ]
    if not monitor_times:
        primary_monitor_time = _parse_publication_utc(monitoring.get("last_modified_on"))
        if primary_monitor_time is not None:
            monitor_times.append(primary_monitor_time)
    monitor_time = max(monitor_times) if monitor_times else None
    page_time = _parse_publication_utc(day_data.get("last_modified_on_utc"))
    candidates = [value for value in (monitor_time, page_time) if value is not None]
    if not candidates:
        return {
            **day_data,
            "publication_metadata_complete": False,
            "publication_timestamp_source": None,
        }
    effective_time = max(candidates)
    if monitor_time is not None and page_time is not None:
        source = "max(finalComputation_page_envelopes, monitoring_publication)"
    elif monitor_time is not None:
        source = "jao_monitoring.lastModifiedOn"
    else:
        source = "finalComputation.lastModifiedOn_max_across_pages"
    return {
        **day_data,
        "last_modified_on_utc": _iso_z(effective_time, milliseconds=True),
        "publication_metadata_complete": True,
        "publication_timestamp_source": source,
        "monitoring_last_modified_on_utc": (
            _iso_z(monitor_time, milliseconds=True) if monitor_time else None
        ),
        "page_last_modified_on_utc": (
            _iso_z(page_time, milliseconds=True) if page_time else None
        ),
    }


def _fetch_hour_uncached(
    hour_start_utc: datetime,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Adapter preserving the current module's monkeypatchable request hook."""
    from models.grid.jao_domain_pages import fetch_hour_uncached

    return fetch_hour_uncached(
        hour_start_utc,
        timeout_seconds=timeout_seconds,
        request_json=_request_json,
        domain_url=JAO_DOMAIN_URL,
        iso_z=_iso_z,
        parse_publication_utc=_parse_publication_utc,
        parse_nonnegative_int=_parse_nonnegative_int,
        normalize_records=normalize_jao_records,
        page_size=JAO_PAGE_SIZE,
        max_pages=JAO_MAX_PAGES,
    )


def _fetch_hour(
    hour_start_utc: datetime,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch once per UTC hour, coalescing concurrent requests and caching only complete data."""
    key = _iso_z(_utc(hour_start_utc).replace(minute=0, second=0, microsecond=0))
    now = time.monotonic()
    with _HOUR_LOCK:
        cached = _HOUR_CACHE.get(key)
        if cached and cached[0] > now:
            _bump_cache_stat("hour_cache_hits")
            return cached[1]
        future = _HOUR_INFLIGHT.get(key)
        is_owner = future is None
        if is_owner:
            future = Future()
            _HOUR_INFLIGHT[key] = future

    if not is_owner:
        _bump_cache_stat("hour_inflight_waits")
        return future.result()

    try:
        _bump_cache_stat("hour_fetches")
        result = _fetch_hour_uncached(hour_start_utc, timeout_seconds)
        if result.get("dataset_complete") is True:
            with _HOUR_LOCK:
                _HOUR_CACHE[key] = (time.monotonic() + HOUR_CACHE_TTL_SECONDS, result)
        future.set_result(result)
        return result
    except Exception as error:
        future.set_exception(error)
        raise
    finally:
        with _HOUR_LOCK:
            _HOUR_INFLIGHT.pop(key, None)


def _fetch_day_uncached(
    day_start_utc: datetime,
    day_end_utc: datetime,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Fetch the full Oslo delivery-day JAO domain in a few paginated calls."""
    from models.grid.jao_domain_pages import fetch_day_uncached

    return fetch_day_uncached(
        day_start_utc,
        day_end_utc,
        timeout_seconds=timeout_seconds,
        request_json=_request_json,
        domain_url=JAO_DOMAIN_URL,
        iso_z=_iso_z,
        parse_publication_utc=_parse_publication_utc,
        parse_nonnegative_int=_parse_nonnegative_int,
        normalize_records=normalize_jao_records,
        page_size=JAO_PAGE_SIZE,
        max_pages=JAO_MAX_PAGES,
    )


def _fetch_day(
    day_start_utc: datetime,
    day_end_utc: datetime,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
    *,
    publication_signature: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """Fetch/cache a full delivery day for one identified JAO publication."""
    start = _utc(day_start_utc)
    end = _utc(day_end_utc)
    key = f"{_iso_z(start)}|{_iso_z(end)}|{publication_signature!r}"
    now = time.monotonic()
    with _HOUR_LOCK:
        cached = _DAY_CACHE.get(key)
        if cached and cached[0] > now:
            _bump_cache_stat("day_cache_hits")
            return cached[1]
        future = _DAY_INFLIGHT.get(key)
        is_owner = future is None
        if is_owner:
            future = Future()
            _DAY_INFLIGHT[key] = future

    if not is_owner:
        _bump_cache_stat("day_inflight_waits")
        return future.result()

    try:
        _bump_cache_stat("day_fetches")
        result = _fetch_day_uncached(start, end, timeout_seconds)
        if result.get("dataset_complete") is True:
            with _HOUR_LOCK:
                _DAY_CACHE[key] = (time.monotonic() + DAY_CACHE_TTL_SECONDS, result)
        future.set_result(result)
        return result
    except Exception as error:
        future.set_exception(error)
        raise
    finally:
        with _HOUR_LOCK:
            _DAY_INFLIGHT.pop(key, None)


def _empty_day_data() -> dict[str, Any]:
    return {
        "records_by_mtu": {},
        "last_modified_on_utc": None,
        "publication_metadata_complete": False,
        "raw_last_modified_on": None,
        "dataset_complete": None,
        "dataset_completeness_reason": "JAO_DELIVERY_DAY_DATA_NOT_REQUESTED",
        "returned_row_count": None,
        "expected_row_count": None,
        "expected_row_count_source": None,
        "total_rows": None,
        "total_rows_with_filter": None,
        "page_count": None,
        "normalized_record_count": 0,
        "expected_mtu_count": None,
        "mtus_with_records": 0,
        "missing_mtu_keys": [],
        "messages": None,
    }


def _empty_hour_data() -> dict[str, Any]:
    return {
        "records": [],
        "last_modified_on_utc": None,
        "publication_metadata_complete": False,
        "raw_last_modified_on": None,
        "dataset_complete": None,
        "dataset_completeness_reason": "JAO_HOURLY_DATA_NOT_REQUESTED",
        "returned_row_count": None,
        "expected_row_count": None,
        "expected_row_count_source": None,
        "total_rows": None,
        "total_rows_with_filter": None,
        "messages": None,
    }


def fetch_flow_based_domain(
    delivery_time: datetime,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
    forecast_origin_utc: datetime | None = None,
    *,
    _shared_monitoring: dict[str, Any] | None = None,
    _shared_hour_data: dict[str, Any] | None = None,
    _shared_day_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the complete CNEC/PTDF domain for one 15-minute MTU.

    ``published_at_utc`` is the common forecast-vintage contract consumed by
    ``validate_forecast_vintage``. ``last_modified_on_utc`` is retained as an
    explicit JAO metadata alias for diagnostics.
    """
    period_start = _quarter_hour_start(delivery_time)
    period_end = period_start + timedelta(minutes=15)

    oslo_day = period_start.astimezone(ZoneInfo("Europe/Oslo")).date()
    oslo_tz = ZoneInfo("Europe/Oslo")
    local_start = datetime.combine(oslo_day, datetime.min.time(), tzinfo=oslo_tz)
    local_end = datetime.combine(oslo_day + timedelta(days=1), datetime.min.time(), tzinfo=oslo_tz)
    day_start_utc = local_start.astimezone(timezone.utc)
    day_end_utc = local_end.astimezone(timezone.utc)

    monitoring = _shared_monitoring or _monitoring_status_for_day(day_start_utc, day_end_utc)
    monitoring_received = str(monitoring.get("status", "UNKNOWN")).casefold() == "received"

    if _shared_day_data is not None:
        day_data = _shared_day_data
        records = list(day_data.get("records_by_mtu", {}).get(_iso_z(period_start), []))
    elif _shared_hour_data is not None:
        # Compatibility hook for older one-hour callers.
        hour_data = _shared_hour_data
        records = [
            row for row in hour_data.get("records", [])
            if row.get("delivery_start_utc") == _iso_z(period_start)
        ] if hour_data.get("dataset_complete") is True else []
        day_data = {
            **_empty_day_data(),
            **{key: value for key, value in hour_data.items() if key != "records"},
            "records_by_mtu": {_iso_z(period_start): records},
        }
    elif monitoring_received:
        day_data, monitoring = _fetch_day_with_publication_guard(
            day_start_utc, day_end_utc, timeout_seconds
        )
        monitoring_received = str(monitoring.get("status", "UNKNOWN")).casefold() == "received"
        records = list(day_data.get("records_by_mtu", {}).get(_iso_z(period_start), []))
    else:
        # Avoid polling finalComputation before JAO confirms publication.
        day_data = _empty_day_data()
        records = []

    dataset_complete = day_data.get("dataset_complete")
    if not monitoring_received or dataset_complete is not True:
        # Never allow a partial day response to leak into the optimizer.
        records = []

    zones = sorted(
        {zone for row in records for zone in row.get("ptdf", {})},
        key=lambda zone: (zone not in NO_ZONES, zone),
    )
    last_modified = _parse_publication_utc(day_data.get("last_modified_on_utc"))
    origin = _utc(forecast_origin_utc) if forecast_origin_utc is not None else None

    if not monitoring_received:
        origin_usable: bool | None = False if origin is not None else None
        vintage_reason = "JAO_FINALCOMPUTATION_NOT_CONFIRMED_RECEIVED"
    elif dataset_complete is not True:
        origin_usable = False if origin is not None else None
        vintage_reason = day_data.get("dataset_completeness_reason") or "JAO_DELIVERY_DAY_DATASET_INCOMPLETE"
    elif not day_data.get("publication_metadata_complete") or last_modified is None:
        origin_usable = False if origin is not None else None
        vintage_reason = "JAO_LAST_MODIFIED_METADATA_MISSING_OR_TIMEZONE_UNVERIFIED"
    elif origin is not None and last_modified > origin:
        origin_usable = False
        vintage_reason = "JAO_DATA_LAST_MODIFIED_AFTER_FORECAST_ORIGIN"
    else:
        origin_usable = True if origin is not None else None
        vintage_reason = None

    if not monitoring_received:
        domain_status = "PUBLICATION_NOT_CONFIRMED"
        reason = monitoring.get("reason") or vintage_reason
    elif dataset_complete is not True:
        domain_status = "DATASET_INCOMPLETE"
        reason = day_data.get("dataset_completeness_reason") or "JAO_DELIVERY_DAY_DATASET_INCOMPLETE"
    elif not records:
        domain_status = "NO_ACTIVE_CNECS_FOR_MTU"
        reason = "JAO_RETURNED_NO_USABLE_CNEC_ROWS_FOR_DELIVERY_MTU"
    elif origin is not None and origin_usable is not True:
        domain_status = "PUBLICATION_VINTAGE_UNVERIFIED"
        reason = vintage_reason
    else:
        domain_status = "READY"
        reason = None

    published_at = day_data.get("last_modified_on_utc")
    return {
        "source": "JAO Nordic Publication Tool / Nordic RCC",
        "source_url": JAO_DOMAIN_URL,
        "monitoring_url": JAO_MONITORING_URL,
        "methodology": "Nordic flow-based CNEC RAM and PTDF",
        "projection": "Full published PTDF zone set including Nordic virtual bidding zones",
        "delivery_start_utc": _iso_z(period_start),
        "delivery_end_utc": _iso_z(period_end),
        "unit": "MW",
        "status": domain_status,
        "reason": reason,
        "constraint_count": len(records),
        "zones": zones,
        "constraints": records,
        "monitoring_status": monitoring.get("status", "UNKNOWN"),
        "monitoring_diagnostics": monitoring,
        # Common forecast-vintage contract plus the original JAO field name.
        "published_at_utc": published_at,
        "last_modified_on_utc": published_at,
        "raw_last_modified_on": day_data.get("raw_last_modified_on"),
        "publication_timestamp_source": day_data.get("publication_timestamp_source"),
        "monitoring_last_modified_on_utc": day_data.get("monitoring_last_modified_on_utc"),
        "page_last_modified_on_utc": day_data.get("page_last_modified_on_utc"),
        "jao_page_timestamp_spread_seconds": day_data.get("page_timestamp_spread_seconds"),
        "publication_metadata_complete": day_data.get("publication_metadata_complete", False),
        "dataset_complete": dataset_complete,
        "dataset_completeness_reason": day_data.get("dataset_completeness_reason"),
        "returned_row_count": day_data.get("returned_row_count"),
        "expected_row_count": day_data.get("expected_row_count"),
        "expected_row_count_source": day_data.get("expected_row_count_source"),
        "forecast_origin_utc": _iso_z(origin) if origin else None,
        "forecast_origin_usable": origin_usable,
        "vintage_validation_reason": vintage_reason,
        "total_rows": day_data.get("total_rows"),
        "total_rows_with_filter": day_data.get("total_rows_with_filter"),
        "jao_page_count": day_data.get("page_count"),
        "normalized_day_record_count": day_data.get("normalized_record_count"),
        "expected_day_mtu_count": day_data.get("expected_mtu_count"),
        "mtus_with_records": day_data.get("mtus_with_records"),
        "missing_day_mtu_keys": day_data.get("missing_mtu_keys", []),
        "api_messages": day_data.get("messages"),
    }


def _fetch_day_with_publication_guard(
    day_start: datetime,
    day_end: datetime,
    timeout_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetch a day only when the CNE publication remains stable across paging.

    One full retry is allowed if JAO publishes a new version while pages are
    being collected. The cache key includes the monitoring publication
    signature, so a previous version cannot be reused after a publication.
    """
    last_monitoring: dict[str, Any] = {}
    last_data = _empty_day_data()

    for attempt in range(2):
        before = _monitoring_status_for_day(day_start, day_end, force_refresh=True)
        last_monitoring = before
        before_signature = _monitoring_publication_signature(before)
        if before_signature is None:
            return (
                {
                    **_empty_day_data(),
                    "dataset_complete": False,
                    "dataset_completeness_reason": (
                        "JAO_MONITORING_PUBLICATION_VERSION_UNVERIFIED_BEFORE_PAGINATION"
                    ),
                },
                before,
            )

        last_data = _fetch_day(
            day_start,
            day_end,
            timeout_seconds,
            publication_signature=before_signature,
        )
        after = _monitoring_status_for_day(day_start, day_end, force_refresh=True)
        last_monitoring = after
        after_signature = _monitoring_publication_signature(after)

        if after_signature == before_signature:
            return _apply_monitoring_publication_vintage(last_data, after), after

        if attempt == 0 and after_signature is not None:
            # The publication legitimately changed during the first attempt.
            # Retry against the new monitoring signature rather than mixing pages.
            continue

        reason = (
            "JAO_MONITORING_PUBLICATION_VERSION_CHANGED_DURING_PAGINATION"
            if after_signature is not None
            else "JAO_MONITORING_PUBLICATION_VERSION_UNVERIFIED_AFTER_PAGINATION"
        )
        return (
            {
                **last_data,
                "dataset_complete": False,
                "dataset_completeness_reason": reason,
            },
            after,
        )

    return (
        {
            **last_data,
            "dataset_complete": False,
            "dataset_completeness_reason": "JAO_MONITORING_PUBLICATION_VERSION_CHANGED_DURING_PAGINATION",
        },
        last_monitoring,
    )


def fetch_flow_based_domain_batch(
    delivery_times: list[datetime],
    forecast_origin_utc: datetime | None = None,
    timeout_seconds: float = JAO_TIMEOUT_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Fetch one guarded Oslo delivery-day JAO domain and fan it out to MTUs."""
    if not delivery_times:
        return {}
    periods = [_quarter_hour_start(value) for value in delivery_times]
    oslo_days = {period.astimezone(ZoneInfo("Europe/Oslo")).date() for period in periods}
    if len(oslo_days) != 1:
        raise ValueError("A JAO domain batch must contain one Oslo-local delivery day.")
    oslo_day = next(iter(oslo_days))
    oslo_tz = ZoneInfo("Europe/Oslo")
    day_start = datetime.combine(oslo_day, datetime.min.time(), tzinfo=oslo_tz).astimezone(timezone.utc)
    day_end = datetime.combine(oslo_day + timedelta(days=1), datetime.min.time(), tzinfo=oslo_tz).astimezone(timezone.utc)

    monitoring, day_data = None, None
    try:
        day_data, monitoring = _fetch_day_with_publication_guard(
            day_start, day_end, timeout_seconds
        )
    except Exception as error:
        monitoring = _monitoring_status_for_day(day_start, day_end)
        day_data = {
            **_empty_day_data(),
            "dataset_complete": False,
            "dataset_completeness_reason": f"{type(error).__name__}: {error}",
        }

    monitoring = monitoring or _monitoring_status_for_day(day_start, day_end)
    origin = _utc(forecast_origin_utc) if forecast_origin_utc is not None else None
    results = {}
    for period in periods:
        results[_iso_z(period)] = fetch_flow_based_domain(
            period,
            timeout_seconds,
            origin,
            _shared_monitoring=monitoring,
            _shared_day_data=day_data,
        )
    return results

