"""Optional human-readable labels for selected Nordic CNEC records.

This module is presentation metadata only. It must never be used to decide
whether a JAO domain or solver input is valid. The keys below are the
user-supplied representative identifiers; replace them with exact identifiers
from the corresponding Nordic RCC declassification archive before relying on
the labels operationally.
"""
from __future__ import annotations

from typing import Any

# User-supplied representative mappings. These are not treated as fuzzy
# patterns: a record is labelled only when one of its identifiers matches a
# key exactly after trimming and case normalization.
KNOWN_NORDIC_RCC_MAPPINGS: dict[str, dict[str, str]] = {
    "CNEC_NO1_NO3_HASLE": {
        "element_name": "Hasle - Borgvik 420 kV",
        "corridor": "NO1 -> SE3",
        "substations": "Hasle / Borgvik",
        "voltage_kv": "420",
        "tso": "STATNETT",
    },
    "CNEC_NO2_SK_KRISTIANSAND": {
        "element_name": "Kristiansand - Tjele (Skagerrak HVDC 1-4)",
        "corridor": "NO2 -> DK1",
        "substations": "Kristiansand / Tjele",
        "voltage_kv": "500",
        "tso": "STATNETT / ENERGINET",
    },
    "CNEC_NO2_NL_NORLED": {
        "element_name": "Feda - Eemshaven (NorNed HVDC)",
        "corridor": "NO2 -> NL",
        "substations": "Feda / Eemshaven",
        "voltage_kv": "450",
        "tso": "STATNETT / TENNET",
    },
    "CNEC_NO2_NK_NORDLINK": {
        "element_name": "Tonstad - Wilster (NordLink HVDC)",
        "corridor": "NO2 -> DE",
        "substations": "Tonstad / Wilster",
        "voltage_kv": "525",
        "tso": "STATNETT / TENNET",
    },
    "CNEC_NO3_NO4_AURA": {
        "element_name": "Aura - Viklandet 300/420 kV",
        "corridor": "NO3 -> NO4 (Snitt Midt-Nord)",
        "substations": "Aura / Viklandet",
        "voltage_kv": "420",
        "tso": "STATNETT",
    },
    "CNEC_NO5_NO1_DAGALI": {
        "element_name": "Dagali - Nore 300 kV",
        "corridor": "NO5 -> NO1",
        "substations": "Dagali / Nore",
        "voltage_kv": "300",
        "tso": "STATNETT",
    },
    "CNEC_NO2_NO5_SAUDA": {
        "element_name": "Sauda - Ertsmyra 420 kV",
        "corridor": "NO5 -> NO2",
        "substations": "Sauda / Ertsmyra",
        "voltage_kv": "420",
        "tso": "STATNETT",
    },
}


def _key(value: Any) -> str:
    return str(value or "").strip().casefold()


_MAPPING_BY_KEY = {_key(name): details for name, details in KNOWN_NORDIC_RCC_MAPPINGS.items()}


def deanonymize_cnec(constraint: dict[str, Any]) -> dict[str, Any]:
    """Attach labels only on an exact identifier match.

    Substring matching is deliberately avoided: notably, an empty identifier
    must never match every entry in the mapping table.
    """
    constraint_id = constraint.get("constraint_id") or constraint.get("mrId") or constraint.get("id")
    cnec_name = constraint.get("cnec_name") or constraint.get("cnecName")
    candidates = [value for value in (constraint_id, cnec_name) if _key(value)]
    match = next((_MAPPING_BY_KEY[_key(value)] for value in candidates if _key(value) in _MAPPING_BY_KEY), None)

    if match is not None:
        return {
            **constraint,
            **match,
            "deanonymized": True,
            "deanonymization_match": "EXACT_USER_SUPPLIED_KEY",
            "deanonymization_mapping_status": "REPRESENTATIVE_UNVERIFIED",
        }

    return {
        **constraint,
        "element_name": constraint.get("cne_name") or constraint.get("cneName") or cnec_name or constraint_id or "Unknown CNEC",
        "corridor": "Unknown",
        "voltage_kv": "Unknown",
        "substations": "Unknown",
        "deanonymized": False,
        "deanonymization_match": None,
        "deanonymization_mapping_status": "NO_EXACT_MAPPING",
    }
