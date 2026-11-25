"""Pure validation for information available at a declared forecast origin."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


def parse_utc(value: str | datetime, label: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


def validate_forecast_vintage(
    domain: dict[str, Any], market: dict[str, Any], forecast_origin_utc: str | datetime
) -> None:
    """Reject hindsight or untraceable inputs before either dispatch model sees them."""
    origin = parse_utc(forecast_origin_utc, "forecast_origin_utc")
    delivery_value = domain.get("delivery_start_utc") or market.get("delivery_start_utc")
    if delivery_value:
        delivery_day = parse_utc(delivery_value, "delivery_start_utc").astimezone(ZoneInfo("Europe/Oslo")).date()
        gate_closure = datetime.combine(
            delivery_day - timedelta(days=1), time(12, 0), tzinfo=ZoneInfo("Europe/Oslo")
        ).astimezone(timezone.utc)
        if origin >= gate_closure:
            raise ValueError("Forecast origin is at or after Nordic day-ahead gate closure; the input vintage is not a pre-auction forecast.")
    if domain.get("delivery_start_utc") and market.get("delivery_start_utc"):
        domain_start = parse_utc(domain["delivery_start_utc"], "JAO delivery_start_utc")
        market_start = parse_utc(market["delivery_start_utc"], "market delivery_start_utc")
        if domain_start != market_start:
            raise ValueError("JAO domain and market input timestamps do not refer to the same MTU.")
    market_origin = market.get("forecast_origin_utc")
    if market_origin and parse_utc(market_origin, "market forecast_origin_utc") != origin:
        raise ValueError("Market input forecast origin differs from requested forecast origin.")
    if domain.get("forecast_origin_utc") and parse_utc(domain["forecast_origin_utc"], "JAO forecast_origin_utc") != origin:
        raise ValueError("JAO domain forecast origin differs from requested forecast origin.")
    if domain.get("forecast_origin_usable") is not True:
        raise ValueError("JAO domain is not verified as available at forecast origin.")
    if market.get("forecast_origin_usable") is not True:
        raise ValueError("Market input snapshot is not verified as available at forecast origin.")

    domain_publication = domain.get("published_at_utc")
    if not domain_publication:
        raise ValueError("JAO domain publication timestamp is missing; forecast vintage cannot be verified.")
    if parse_utc(domain_publication, "JAO published_at_utc") > origin:
        raise ValueError("JAO domain was published after the requested forecast origin.")

    for area in ("NO1", "NO2", "NO3", "NO4", "NO5"):
        record = market.get("load_inputs", {}).get(area)
        if not record:
            raise ValueError(f"Missing day-ahead load forecast metadata for {area}.")
        if record.get("document_type") != "A65" or record.get("process_type") != "A01":
            raise ValueError(f"{area} load input is not ENTSO-E A65/A01 day-ahead forecast.")
        if record.get("quality_status") != "PUBLISHED_FORECAST":
            raise ValueError(f"{area} load input is not marked as a published forecast.")
        if record.get("available_at_forecast_origin") is not True:
            raise ValueError(f"{area} load forecast was not verified as available at forecast origin.")
        published = record.get("published_at_utc")
        if not published or parse_utc(published, f"{area} load publication") > origin:
            raise ValueError(f"{area} load forecast publication is after forecast origin or unknown.")

    renewable_inputs = market.get("renewable_forecast_inputs", {})
    for area in ("NO2", "NO3", "NO4"):
        record = renewable_inputs.get(area)
        if not record:
            continue
        if record.get("document_type") != "A69" or record.get("process_type") != "A01":
            raise ValueError(f"{area} wind input is not ENTSO-E A69/A01 forecast metadata.")
        if record.get("available_at_forecast_origin") is not True:
            raise ValueError(f"{area} wind forecast was not verified as available at forecast origin.")
        series = record.get("series", {})
        if not series:
            raise ValueError(f"{area} A69 forecast has no timestamped generation series.")
        for psr_type, item in series.items():
            published = item.get("published_at_utc")
            if not published or parse_utc(published, f"{area} A69 {psr_type} publication") > origin:
                raise ValueError(
                    f"{area} A69 {psr_type} forecast publication is after forecast origin or unknown."
                )

    boundary = market.get("boundary_price_inputs", {})
    if not boundary:
        raise ValueError("No forecast boundary-price inputs were supplied.")
    for zone, record in boundary.items():
        if record.get("forecast_method") != "LAGGED_A44_SAME_LOCAL_MTUD1":
            raise ValueError(f"Boundary price for {zone} is not the approved lagged-A44 forecast proxy.")
        if record.get("available_at_forecast_origin") is not True:
            raise ValueError(f"Boundary price proxy for {zone} was not available at forecast origin.")
        published = record.get("published_at_utc")
        if not published or parse_utc(published, f"{zone} boundary-price publication") > origin:
            raise ValueError(f"Boundary price proxy for {zone} is after forecast origin or publication is unknown.")
