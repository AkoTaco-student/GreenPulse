"""Norway-local day-ahead forecast calendar helpers (DST-safe)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")

# A FORECAST job is only valid for an information cutoff inside this D-1 window
# (Oslo time): 11:15 is after the normal JAO final-computation publication,
# 12:00 is Nordic day-ahead gate closure. The lower bound is inclusive.
FORECAST_WINDOW_START = time(11, 15)
FORECAST_WINDOW_END = time(12, 0)


def oslo_today(now: datetime | None = None) -> date:
    """Oslo-local calendar date; REPLAY is always run for this delivery day."""
    current = now or datetime.now(OSLO)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware.")
    return current.astimezone(OSLO).date()


def next_oslo_delivery_day(now: datetime | None = None) -> date:
    """Default the forecast request to the next Oslo-local delivery day."""
    return oslo_today(now) + timedelta(days=1)


def forecast_window_utc(delivery_day: date) -> tuple[datetime, datetime]:
    """Return the [start, end) UTC instants of the 11:15-12:00 Oslo D-1 window."""
    d_minus_1 = delivery_day - timedelta(days=1)
    start = datetime.combine(d_minus_1, FORECAST_WINDOW_START, tzinfo=OSLO)
    end = datetime.combine(d_minus_1, FORECAST_WINDOW_END, tzinfo=OSLO)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def default_forecast_origin(delivery_day: date) -> datetime:
    """Use the start of the forecast window: 11:15 Oslo on D-1."""
    return forecast_window_utc(delivery_day)[0]


def validate_pre_auction_origin(delivery_day: date, origin: datetime) -> datetime:
    """Require a timezone-aware information cutoff inside the 11:15-12:00 D-1 window."""
    if origin.tzinfo is None:
        raise ValueError("forecast_origin_utc must be timezone-aware.")
    origin_utc = origin.astimezone(timezone.utc)
    window_start, window_end = forecast_window_utc(delivery_day)
    if origin_utc >= window_end:
        raise ValueError("Forecast origin must be before Nordic day-ahead gate closure (12:00 Oslo time on D-1).")
    if origin_utc < window_start:
        raise ValueError("Forecast origin must not be earlier than 11:15 Oslo time on D-1.")
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
