"""Pure normalization of JAO finalComputation CNEC rows.

HTTP retrieval, publication checks and caches live in flow_based_domain.py.
This module only converts a response's data rows into solver-ready records.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from models.dispatch.jao_market_constraints import (
    SUPPORTED_MARKET_ZONES,
    SYNCHRONOUS_AREA_GROUPS,
)

NON_MARKET_VIRTUAL_ZONES = {"NO2_NSL", "DK1_VL"}
MONITORING_ONLY_PREFIXES = ("border_cnec_", "netposition_")


def parse_jao_utc(value: Any) -> datetime | None:
    """Parse dateTimeUtc; this field is explicitly UTC-valued by JAO."""
    if not isinstance(value, (str, datetime)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_z(value: datetime, *, milliseconds: bool = False) -> str:
    timespec = "milliseconds" if milliseconds else "seconds"
    return value.astimezone(timezone.utc).isoformat(timespec=timespec).replace("+00:00", "Z")


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_optional_bool(value: Any) -> bool | None:
    """Parse JAO boolean metadata without treating unknown values as False."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def normalize_jao_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the complete active JAO market-coupling CNEC set.

    A row with status OK is part of the published active domain. Missing or
    malformed PTDF columns are therefore an input-integrity error, not a
    reason to silently drop the constraint and enlarge the feasible domain.
    Monitoring-only Border_CNEC and NetPosition rows are excluded. AC
    allocation constraints are retained.
    """
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise ValueError("JAO response 'data' field is not a list.")

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("cneStatus", "")).strip().casefold() != "ok":
            continue

        cnec_name = str(row.get("cnecName") or row.get("cneName") or "").strip()
        if cnec_name.casefold().startswith(MONITORING_ONLY_PREFIXES):
            continue

        cnec_type = str(
            row.get("cnecType") or row.get("Type") or row.get("type") or ""
        ).strip()
        is_allocation_constraint = (
            cnec_type.casefold() == "allocation_constraint"
            or cnec_name.casefold().startswith(("ac_minimum_", "ac_maximum_"))
        )
        significant = _parse_optional_bool(row.get("significant", row.get("Significant")))
        if significant is False and not is_allocation_constraint:
            continue

        non_redundant = _parse_optional_bool(
            row.get("nonRedundant", row.get("non_redundant", row.get("NonRedundant")))
        )
        constraint_id = str(row.get("mrId") or row.get("id") or cnec_name or "UNKNOWN")
        ram_mw = finite_float(row.get("ram"))
        timestamp = parse_jao_utc(row.get("dateTimeUtc"))
        if ram_mw is None or timestamp is None:
            raise ValueError(
                f"Active JAO CNEC {constraint_id} is missing finite RAM or valid dateTimeUtc."
            )

        ptdf: dict[str, float] = {}
        invalid_ptdf_columns: list[str] = []
        for key, value in row.items():
            key_text = str(key)
            if not key_text.casefold().startswith("ptdf_"):
                continue
            zone = key_text[5:]
            if zone in NON_MARKET_VIRTUAL_ZONES:
                continue
            coefficient = finite_float(value)
            if coefficient is None:
                invalid_ptdf_columns.append(zone)
            else:
                ptdf[zone] = coefficient

        if invalid_ptdf_columns:
            raise ValueError(
                f"Active JAO CNEC {constraint_id} has non-finite PTDF values for {invalid_ptdf_columns}."
            )
        missing_zones = sorted(SUPPORTED_MARKET_ZONES - set(ptdf))
        unexpected_zones = sorted(set(ptdf) - SUPPORTED_MARKET_ZONES)
        if missing_zones or unexpected_zones:
            raise ValueError(
                f"Active JAO CNEC {constraint_id} has an incomplete or changed PTDF zone set: "
                f"missing={missing_zones}, unsupported={unexpected_zones}."
            )

        # A CNEC whose PTDF is constant within each synchronous area has zero
        # flow for every feasible JAO net-position vector and is redundant
        # under the synchronous-area balance equations.
        group_constant = True
        for group_zones in SYNCHRONOUS_AREA_GROUPS.values():
            values = [ptdf[zone] for zone in group_zones]
            if values and max(values) - min(values) > 1e-8:
                group_constant = False
                break
        if group_constant:
            continue

        normalized.append({
            "constraint_id": constraint_id,
            "cnec_name": str(row.get("cnecName") or ""),
            "cne_name": str(row.get("cneName") or ""),
            "cnec_type": cnec_type or None,
            "significant": significant,
            "non_redundant": non_redundant,
            "tso": str(row.get("tso") or ""),
            "contingency": str(row.get("contName") or ""),
            "delivery_start_utc": iso_z(timestamp),
            "ram_mw": ram_mw,
            "ptdf": ptdf,
            "validity_status": "PUBLISHED_ACTIVE_CNEC",
            "raw_last_modified_on": str(row.get("lastModifiedOn")) if row.get("lastModifiedOn") is not None else None,
        })
    return normalized
