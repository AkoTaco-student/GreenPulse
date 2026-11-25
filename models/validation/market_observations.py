"""Kvalitetskontroll og sammenligning av observerte markedsdata."""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any

from models.grid.norway_area_network import AREAS

MARKET_PRICE_AREAS = AREAS + (
    "SE1", "SE2", "SE3", "SE4", "FI", "DK1", "DK2", "DE_LU", "NL",
)


def validate_price_observations(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Kontroller format, område, enhet og tidsstempel på prisobservasjoner."""
    errors: list[str] = []
    accepted: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        area = str(record.get("area", ""))
        timestamp = str(record.get("timestamp", ""))
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed_timestamp.tzinfo is None:
                raise ValueError("Tidsstempel må ha tidssone.")
        except ValueError:
            errors.append(f"Rad {index}: ugyldig ISO-8601-tidsstempel.")
            continue
        try:
            price = float(record["price_eur_mwh"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"Rad {index}: pris mangler eller er ikke numerisk.")
            continue
        if not math.isfinite(price):
            errors.append(f"Rad {index}: pris må være et endelig tall.")
            continue
        if area not in MARKET_PRICE_AREAS:
            errors.append(f"Rad {index}: ukjent norsk budområde {area!r}.")
            continue
        if record.get("unit") != "EUR/MWh":
            errors.append(f"Rad {index}: forventet pris-enhet EUR/MWh.")
            continue
        accepted.append({**record, "area": area, "price_eur_mwh": price})

    return {
        "valid": not errors,
        "accepted_count": len(accepted),
        "errors": errors,
        "records": accepted,
        "quality_status": "VALIDATED" if not errors else "PARTIAL",
    }


def compare_observed_to_model(
    observations: list[dict[str, Any]], modeled_prices: dict[str, float]
) -> dict[str, Any]:
    """Sammenlign siste observerte pris per område med modellpris."""
    latest: dict[str, tuple[str, float]] = {}
    for record in observations:
        area = str(record["area"])
        timestamp = str(record["timestamp"])
        value = float(record["price_eur_mwh"])
        if area not in latest or timestamp > latest[area][0]:
            latest[area] = (timestamp, value)
    observed = {area: value for area, (_, value) in latest.items()}
    common_areas = sorted(set(observed) & set(modeled_prices))
    differences = {
        area: round(float(observed[area]) - float(modeled_prices[area]), 2)
        for area in common_areas
    }
    result = {
        "available": bool(differences),
        "unit": "EUR/MWh",
        "areas": {
            area: {
                "observed": round(float(observed[area]), 2),
                "modeled": round(float(modeled_prices[area]), 2),
                "difference": differences[area],
            }
            for area in common_areas
        },
        "mean_absolute_difference": (
            round(sum(abs(value) for value in differences.values()) / len(differences), 2)
            if differences else None
        ),
        "interpretation": (
            "Prisavviket er observert minus modellert marginalkostnad. "
            "Det er ikke i seg selv en identifisert årsak."
        ),
    }
    result["observation_timestamps"] = {
        area: timestamp for area, (timestamp, _) in latest.items()
    }
    result["source"] = "ENTSO-E Transparency Platform"
    return result
