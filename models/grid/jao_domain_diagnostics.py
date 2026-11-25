"""Pure diagnostics for checking observed net positions against JAO CNECs."""
from __future__ import annotations

from typing import Any


def check_domain_feasibility(
    constraints: list[dict[str, Any]],
    net_positions_mw: dict[str, float],
    tolerance_mw: float = 10.0,
) -> dict[str, Any]:
    """Check the supplied PTDF/RAM inequalities for one net-position vector."""
    violations: list[dict[str, Any]] = []
    binding: list[dict[str, Any]] = []
    max_violation = 0.0

    for row in constraints:
        ptdf = row.get("ptdf", {})
        ram = float(row.get("ram_mw", 0.0))
        calculated_flow = sum(
            float(coefficient) * float(net_positions_mw.get(zone, 0.0))
            for zone, coefficient in ptdf.items()
        )
        slack = ram - calculated_flow
        if slack < -tolerance_mw:
            violation = -slack
            max_violation = max(max_violation, violation)
            violations.append({
                "constraint_id": row.get("constraint_id"),
                "cnec_name": row.get("cnec_name"),
                "ram_mw": ram,
                "calculated_flow_mw": round(calculated_flow, 2),
                "margin_violated_mw": round(violation, 2),
            })
        elif slack <= tolerance_mw:
            binding.append({
                "constraint_id": row.get("constraint_id"),
                "cnec_name": row.get("cnec_name"),
                "ram_mw": ram,
                "calculated_flow_mw": round(calculated_flow, 2),
                "remaining_slack_mw": round(slack, 2),
            })

    return {
        "is_feasible": not violations,
        "tolerance_mw": tolerance_mw,
        "total_constraints_checked": len(constraints),
        "violation_count": len(violations),
        "binding_count": len(binding),
        "max_violation_mw": round(max_violation, 2),
        "violations": violations,
        "binding_constraints": binding,
    }
