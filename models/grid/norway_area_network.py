"""Validate the reduced NO1-NO5 bidding-zone node set and schematic graph."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5")


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as source:
        data = yaml.safe_load(source)
    if not isinstance(data, dict):
        raise ValueError(f"Nettkonfigurasjon er ugyldig: {path}")
    return data


def load_area_nodes(config_path: Path | None = None) -> list[dict[str, Any]]:
    """Return the five modeled area nodes in stable canonical order."""
    path = config_path or Path(__file__).resolve().parents[2] / "config" / "network.yaml"
    config = _load_yaml(path)
    nodes = config.get("nodes", [])
    node_ids = [str(node["id"]) for node in nodes]
    if len(node_ids) != len(AREAS) or set(node_ids) != set(AREAS):
        raise ValueError("Femområdemodellen må inneholde NO1, NO2, NO3, NO4 og NO5 én gang hver.")
    by_id = {str(node["id"]): node for node in nodes}
    return [by_id[area] for area in AREAS]


def load_area_network(config_path: Path | None = None) -> dict[str, Any]:
    """Load legacy pedagogical DC graph; line ratings are assumptions, not facts."""
    path = config_path or Path(__file__).resolve().parents[2] / "config" / "network.yaml"
    network = _load_yaml(path)
    nodes = load_area_nodes(path)
    node_ids = {str(node["id"]) for node in nodes}
    for line in network.get("lines", []):
        if line["from_node"] not in node_ids or line["to_node"] not in node_ids:
            raise ValueError(f"Ukjent budområde på linje {line.get('id', '<uten id>')}.")
        if float(line["capacity_mw"]) <= 0 or float(line["susceptance_b"]) <= 0:
            raise ValueError(f"Ugyldige nettparametere på linje {line.get('id', '<uten id>')}.")

    network["model_kind"] = "schematic_norwegian_bidding_zone_dc"
    network["is_actual_transmission_grid"] = False
    network["line_data_provenance"] = "legacy_assumptions_not_official_ratings"
    return network


def summarize_line_flows(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calculate utilization and status from modeled line flows."""
    summaries = []
    for line in lines:
        capacity = float(line["capacity_mw"])
        flow = float(line["flow_mw"])
        if capacity <= 0:
            raise ValueError(f"Linjekapasitet må være positiv: {line.get('line_id')}.")
        utilization = abs(flow) / capacity
        summaries.append({
            **line,
            "utilization_pct": round(utilization * 100, 1),
            "is_binding": bool(line.get("is_binding", utilization >= 0.999)),
        })
    return summaries
