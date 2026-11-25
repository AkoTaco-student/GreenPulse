"""Small retrying HTTP helper for upstream market-data APIs.

Retries are deliberately bounded and limited to transient network failures,
HTTP 429, and common upstream 5xx responses. Requests are still subject to a
per-attempt socket timeout and must be safe to repeat (GET only in this project).
"""

from __future__ import annotations

import http.client
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Callable

TRANSIENT_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _default_ssl_context() -> ssl.SSLContext | None:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except (ImportError, Exception):
        return None


def env_float(
    name: str, default: float, *, minimum: float = 0.1, maximum: float = 600.0
) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and minimum <= value <= maximum else default


def env_int(name: str, default: int, *, minimum: int = 0, maximum: int = 5) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, min(30.0, float(value)))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, min(30.0, (target - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def read_url(
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    retries: int,
    source_label: str,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bytes:
    """Read a GET response with longer timeouts and bounded transient retries."""
    last_error: Exception | None = None
    ssl_context = _default_ssl_context()
    for attempt in range(retries + 1):
        try:
            if opener is urllib.request.urlopen and ssl_context is not None:
                with opener(request, timeout=timeout_seconds, context=ssl_context) as response:  # type: ignore[call-arg]
                    return response.read()
            else:
                with opener(request, timeout=timeout_seconds) as response:  # type: ignore[attr-defined]
                    return response.read()
        except urllib.error.HTTPError as error:
            body = error.read(2048).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{source_label} HTTP {error.code}: {body}")
            if error.code not in TRANSIENT_HTTP_STATUS or attempt >= retries:
                raise last_error from error
            delay = _retry_after_seconds(error)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            last_error = error
            if attempt >= retries:
                raise RuntimeError(f"{source_label} request failed after {attempt + 1} attempt(s): {error}") from error
            delay = None
        if delay is None:
            delay = min(8.0, 1.5 * (2 ** attempt))
        time.sleep(delay)
    raise RuntimeError(f"{source_label} request failed: {last_error}")
