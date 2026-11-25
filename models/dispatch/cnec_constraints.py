"""Shared PTDF/RAM matrix validation used by both LP formulations."""
from __future__ import annotations

import math
import re
from typing import Any

import numpy as np


MONITORING_ONLY_CNEC_PREFIXES = ("border_cnec_", "netposition_")
ALLOCATION_CONSTRAINT_PREFIXES = ("ac_minimum_", "ac_maximum_")
_ALLOCATION_NAME = re.compile(r"^ac_(minimum|maximum)_(.+)$", re.IGNORECASE)


def _optional_bool(value: Any) -> bool | None:
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


def _allocation_constraint_details(row: dict[str, Any]) -> tuple[str, str] | None:
    """Return (bound, zone) for JAO AC_Minimum/AC_Maximum rows."""
    name = str(row.get("cnec_name") or row.get("cne_name") or "").strip()
    match = _ALLOCATION_NAME.match(name)
    if not match:
        return None
    bound = match.group(1).casefold()
    suffix = match.group(2).strip()
    # Spanning-backup names can append an MTU/mRID after the virtual-zone
    # identifier. Match the longest known zone at the start of the suffix.
    from models.dispatch.jao_market_constraints import SUPPORTED_MARKET_ZONES

    for zone in sorted(SUPPORTED_MARKET_ZONES, key=len, reverse=True):
        if suffix.casefold() == zone.casefold():
            return bound, zone
        if suffix.casefold().startswith(zone.casefold()):
            remainder = suffix[len(zone):]
            if remainder.startswith((" ", "_", "-")):
                return bound, zone
    return None


def _is_allocation_constraint(row: dict[str, Any]) -> bool:
    cnec_type = str(row.get("cnec_type") or row.get("type") or "").strip().casefold()
    name = str(row.get("cnec_name") or row.get("cne_name") or "").strip().casefold()
    return cnec_type == "allocation_constraint" or name.startswith(ALLOCATION_CONSTRAINT_PREFIXES)


def is_market_cnec(row: dict[str, Any]) -> bool:
    """Return True only for constraints used by JAO market coupling.

    JAO's Border_CNECs and NetPosition CNECs are monitoring-only. Explicit
    Significant=False rows are disregarded in flow-based parameter calculation.
    Explicit NonRedundant=False rows are mathematically redundant and can be
    omitted from the LP. AC_Minimum/AC_Maximum allocation constraints remain
    active solver constraints. Missing metadata is treated conservatively:
    keep the row rather than silently weakening the domain.
    """
    name = str(row.get("cnec_name") or row.get("cne_name") or "").strip().casefold()
    if name.startswith(MONITORING_ONLY_CNEC_PREFIXES):
        return False

    is_allocation = _is_allocation_constraint(row)
    significant = _optional_bool(row.get("significant", row.get("Significant")))
    if significant is False and not is_allocation:
        return False

    non_redundant = _optional_bool(
        row.get("non_redundant", row.get("nonRedundant", row.get("NonRedundant")))
    )
    if non_redundant is False and not is_allocation:
        return False
    return True


def market_cnec_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter non-market rows consistently, including older cached snapshots."""
    return [row for row in rows if isinstance(row, dict) and is_market_cnec(row)]


def cnec_filter_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count active market CNECs and each excluded/retained row class."""
    summary = {
        "raw_rows": len(rows),
        "market_rows": 0,
        "allocation_constraint_rows": 0,
        "other_market_cnec_rows": 0,
        "monitoring_only_rows": 0,
        "non_significant_rows": 0,
        "redundant_rows": 0,
        "invalid_rows": 0,
    }
    for row in rows:
        if not isinstance(row, dict):
            summary["invalid_rows"] += 1
            continue
        name = str(row.get("cnec_name") or row.get("cne_name") or "").strip().casefold()
        if name.startswith(MONITORING_ONLY_CNEC_PREFIXES):
            summary["monitoring_only_rows"] += 1
            continue
        is_allocation = _is_allocation_constraint(row)
        if _optional_bool(row.get("significant", row.get("Significant"))) is False and not is_allocation:
            summary["non_significant_rows"] += 1
            continue
        if (
            _optional_bool(row.get("non_redundant", row.get("nonRedundant", row.get("NonRedundant")))) is False
            and not is_allocation
        ):
            summary["redundant_rows"] += 1
            continue
        summary["market_rows"] += 1
        if is_allocation:
            summary["allocation_constraint_rows"] += 1
        else:
            summary["other_market_cnec_rows"] += 1
    return summary


def build_cnec_matrix(
    rows: list[dict[str, Any]], zones: tuple[str, ...] | list[str]
) -> tuple[np.ndarray, np.ndarray]:
    """Return ordered ``PTDF[c,z]`` and ``RAM[c]`` arrays.

    Monitoring-only rows are excluded defensively so older saved input snapshots
    are handled consistently with newly normalized JAO responses. Missing PTDFs
    are errors, never implicit zeros.
    """
    rows = market_cnec_rows(rows)
    if not rows:
        raise ValueError("The JAO domain contains no market-coupling CNEC rows.")
    columns: list[list[float]] = []
    margins: list[float] = []
    for row in rows:
        ptdf = row.get("ptdf", {})
        missing = [zone for zone in zones if zone not in ptdf]
        if missing:
            raise ValueError(f"CNEC {row.get('constraint_id')} is missing PTDF columns: {missing}.")
        try:
            values = [float(ptdf[zone]) for zone in zones]
            ram = float(row["ram_mw"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"CNEC {row.get('constraint_id')} has invalid PTDF/RAM data.") from error
        if not all(math.isfinite(value) for value in values) or not math.isfinite(ram):
            raise ValueError(f"CNEC {row.get('constraint_id')} has non-finite PTDF/RAM data.")

        allocation = _allocation_constraint_details(row)
        if allocation is not None:
            bound, allocation_zone = allocation
            if allocation_zone not in zones:
                raise ValueError(
                    f"JAO allocation constraint {row.get('cnec_name')} references unknown zone {allocation_zone}."
                )
            expected_sign = -1.0 if bound == "minimum" else 1.0
            expected = {zone: 0.0 for zone in zones}
            expected[allocation_zone] = expected_sign
            bad_columns = [
                zone for zone in zones
                if abs(float(ptdf[zone]) - expected[zone]) > 1e-6
            ]
            if bad_columns:
                raise ValueError(
                    f"JAO allocation constraint {row.get('cnec_name')} must have PTDF {expected_sign:+.0f} "
                    f"only on {allocation_zone}; inconsistent columns={bad_columns}."
                )
            # Negative RAM is valid for an AC_Minimum constraint when the
            # minimum net position is positive: -NP <= -minimum. Do not reject
            # it merely because this is an allocation constraint.

        columns.append(values)
        margins.append(ram)
    return np.asarray(columns, dtype=float), np.asarray(margins, dtype=float)
