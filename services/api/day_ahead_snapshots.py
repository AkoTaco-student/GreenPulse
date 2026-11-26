"""Gzip-backed snapshot cache for GreenPulse day-ahead forecasts and replay runs."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from services.api.forecast_calendar import delivery_periods

CACHE_SCHEMA_VERSION = 3
_ROOT = Path(__file__).resolve().parents[2]
_FORECAST_DIR = _ROOT / "data" / "processed" / "forecasts"


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("snapshot datetimes must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def forecast_snapshot_path(delivery_day: date, origin: datetime) -> Path:
    _FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    tag = utc_text(origin).replace(":", "").replace("-", "")
    return _FORECAST_DIR / f"{delivery_day.isoformat()}_origin_{tag}_forecast.json.gz"


def replay_snapshot_path(delivery_day: date) -> Path:
    _FORECAST_DIR.mkdir(parents=True, exist_ok=True)
    return _FORECAST_DIR / f"{delivery_day.isoformat()}_replay.json.gz"


def _atomic_write_gzip(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as zipped:
                zipped.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError, EOFError):
        return None


def _validate_snapshot(delivery_day: date, snapshot: dict[str, Any], *, origin: datetime | None = None, replay: bool = False) -> bool:
    if snapshot.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
        return False
    if snapshot.get("mode") != ("REPLAY" if replay else "FORECAST"):
        return False
    if snapshot.get("delivery_day_local") != delivery_day.isoformat():
        return False

    origin_text = snapshot.get("forecast_origin_utc")
    if origin_text is not None:
        if not isinstance(origin_text, str):
            return False
        try:
            snapshot_origin = datetime.fromisoformat(origin_text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if snapshot_origin.tzinfo is None:
            return False
        if origin is not None and snapshot_origin.astimezone(timezone.utc) != origin.astimezone(timezone.utc):
            return False
    elif origin is not None:
        return False

    intervals = snapshot.get("intervals")
    if not isinstance(intervals, list):
        return False
    expected = delivery_periods(delivery_day)
    if len(intervals) != len(expected):
        return False

    for interval, expected_period in zip(intervals, expected):
        text = interval.get("delivery_start_utc") if isinstance(interval, dict) else None
        if not isinstance(text, str):
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
        if parsed.astimezone(timezone.utc) != expected_period:
            return False

    if replay:
        if snapshot.get("collected_interval_count") != len(expected):
            return False
        if not all(isinstance(interval, dict) and isinstance(interval.get("ready"), bool) for interval in intervals):
            return False
        actual_ready_count = sum(interval["ready"] is True for interval in intervals)
        if actual_ready_count == 0:
            return False
        return snapshot.get("ready_interval_count") == actual_ready_count
    actual_ready_count = sum(isinstance(interval, dict) and interval.get("ready") is True for interval in intervals)
    return actual_ready_count > 0


def save_forecast_snapshot(delivery_day: date, origin: datetime, snapshot: dict[str, Any]) -> Path:
    payload = dict(snapshot)
    payload["cache_schema_version"] = CACHE_SCHEMA_VERSION
    payload["mode"] = "FORECAST"
    payload["delivery_day_local"] = delivery_day.isoformat()
    payload["forecast_origin_utc"] = utc_text(origin)
    path = forecast_snapshot_path(delivery_day, origin)
    _atomic_write_gzip(path, payload)
    return path


def load_forecast_snapshot(delivery_day: date, origin: datetime) -> dict[str, Any] | None:
    snapshot = _load(forecast_snapshot_path(delivery_day, origin))
    if snapshot is None or not _validate_snapshot(delivery_day, snapshot, origin=origin, replay=False):
        return None
    return snapshot


def save_replay_snapshot(delivery_day: date, snapshot: dict[str, Any]) -> Path:
    payload = dict(snapshot)
    payload["cache_schema_version"] = CACHE_SCHEMA_VERSION
    payload["mode"] = "REPLAY"
    payload["delivery_day_local"] = delivery_day.isoformat()
    path = replay_snapshot_path(delivery_day)
    _atomic_write_gzip(path, payload)
    return path


def load_replay_snapshot(delivery_day: date, origin: datetime | None = None) -> dict[str, Any] | None:
    snapshot = _load(replay_snapshot_path(delivery_day))
    if snapshot is None or not _validate_snapshot(delivery_day, snapshot, origin=origin, replay=True):
        return None
    return snapshot
