"""Small client for JAO's publication-monitoring metadata.

The domain endpoint and monitoring endpoint are deliberately separate: the
latter confirms that the CNE Flowbased Domain product has been received.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from typing import Any, Callable

MONITOR_CACHE_TTL_SECONDS = 60.0
MONITOR_LOCK = threading.Lock()
MONITOR_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def monitoring_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize the list and wrapped response shapes returned by JAO."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "results", "monitoring"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    if any(key in payload for key in ("page", "pages", "status", "fileType")):
        return [payload]
    return []


def normalized_page_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def is_flow_based_domain_entry(row: dict[str, Any]) -> bool:
    """Match the received CNE Flowbased Domain, not shadow-price files."""
    file_type = str(row.get("fileType", "")).strip().upper()
    pages_value = row.get("pages", [])
    if isinstance(pages_value, str):
        pages = [pages_value]
    elif isinstance(pages_value, list):
        pages = pages_value
    else:
        pages = []

    if any(normalized_page_name(page) == "flowbaseddomain" for page in pages):
        return file_type in ("", "CNE")
    # Older payloads used a singular `page`. Never apply this fallback when
    # `pages` explicitly describes another publication.
    if pages:
        return False
    return normalized_page_name(row.get("page", "")) == "finalcomputation" and file_type in ("", "CNE")


def monitoring_status_for_day(
    day_start_utc: datetime,
    day_end_utc: datetime,
    *,
    request_json: Callable[..., Any],
    monitoring_url: str,
    iso_z: Callable[..., str],
    cache_ttl_seconds: float = MONITOR_CACHE_TTL_SECONDS,
    on_cache_hit: Callable[[], None] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return publication status, with HTTP injected for easy unit testing."""
    cache_key = iso_z(day_start_utc)
    now = time.monotonic()
    with MONITOR_LOCK:
        cached = MONITOR_CACHE.get(cache_key)
        if not force_refresh and cached and cached[0] > now:
            if on_cache_hit:
                on_cache_hit()
            return dict(cached[1])

        query = {
            "FromUtc": iso_z(day_start_utc, milliseconds=True),
            "ToUtc": iso_z(day_end_utc, milliseconds=True),
        }
        try:
            payload = request_json(
                monitoring_url,
                query,
                source_label="JAO publication monitoring",
            )
            rows = monitoring_rows(payload)
            matches = [row for row in rows if is_flow_based_domain_entry(row)]
            if not matches:
                status = {
                    "status": "UNKNOWN",
                    "reason": "MONITORING_FLOWBASED_DOMAIN_ENTRY_NOT_FOUND",
                    "raw_entry_count": len(rows),
                }
            else:
                values = [str(row.get("status", "UNKNOWN")).strip() for row in matches]
                non_received = next((value for value in values if value.casefold() != "received"), None)
                state = "Received" if non_received is None else non_received
                primary_entry = matches[0]
                status = {
                    "status": state,
                    "entries": matches,
                    "deadline": primary_entry.get("deadline"),
                    "last_modified_on": primary_entry.get("lastModifiedOn"),
                    "file_type": primary_entry.get("fileType"),
                    "pages": primary_entry.get("pages"),
                    "reason": None if state.casefold() == "received" else "JAO_DATASET_NOT_RECEIVED",
                    "raw_entry_count": len(rows),
                }
        except Exception as error:
            status = {
                "status": "UNKNOWN",
                "reason": "MONITORING_REQUEST_FAILED",
                "error": f"{type(error).__name__}: {error}",
            }

        MONITOR_CACHE[cache_key] = (time.monotonic() + cache_ttl_seconds, status)
        return dict(status)
