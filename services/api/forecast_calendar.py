"""Norway-local day-ahead forecast calendar helpers (DST-safe)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")


def next_oslo_delivery_day(now: datetime | None = None) -> date:
    """Default the forecast request to the next Oslo-local delivery day."""
    current = now or datetime.now(OSLO)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return current.astimezone(OSLO).date() + timedelta(days=1)


def default_forecast_origin(delivery_day: date) -> datetime:
    """Use 11:15 Oslo on D-1: after the normal JAO publication window, pre-auction."""
    local = datetime.combine(delivery_day - timedelta(days=1), time(11, 15), tzinfo=OSLO)
    return local.astimezone(timezone.utc)


def validate_pre_auction_origin(delivery_day: date, origin: datetime) -> datetime:
    """Require a timezone-aware information cutoff before Nordic DA gate closure."""
    if origin.tzinfo is None:
        raise ValueError("forecast_origin_utc must be timezone-aware.")
    origin_utc = origin.astimezone(timezone.utc)
    gate_closure = datetime.combine(
        delivery_day - timedelta(days=1), time(12, 0), tzinfo=OSLO
    ).astimezone(timezone.utc)
    if origin_utc >= gate_closure:
        raise ValueError("Forecast origin must be before Nordic day-ahead gate closure (12:00 Oslo time on D-1).")
    return origin_utc


def delivery_periods(delivery_day: date) -> list[datetime]:
    """Return all 15-minute MTU starts for the Oslo-local delivery day."""
    local_start = datetime.combine(delivery_day, time.min, tzinfo=OSLO)
    local_end = datetime.combine(delivery_day + timedelta(days=1), time.min, tzinfo=OSLO)
    start = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    periods = []
    cursor = start
    while cursor < end:
        periods.append(cursor)
        cursor += timedelta(minutes=15)
    return periods
