"""
Economic Dispatch LP Model (NO1-NO5)
Konveks lineær programmeringsmodell (LP) formulert i CVXPY.
Løser markeds-dispatch under overføringsbegrensninger og utleder
områdepriser og flaskehalsverdier direkte fra problemets dualvariabler.
"""

from typing import Dict, Any, Optional
from pathlib import Path
import yaml
import cvxpy as cp
import numpy as np
from models.grid.norway_area_network import load_area_network

class EconomicDispatchLP:
    def __init__(self, network_config_path: Optional[Path] = None, assets_config_path: Optional[Path] = None):
        base_dir = Path(__file__).resolve().parent.parent.parent / "config"
        self.network_path = network_config_path or (base_dir / "network.yaml")
        self.assets_path = assets_config_path or (base_dir / "assets.yaml")

        self.network = load_area_network(self.network_path)
        legacy_loads_path = base_dir / "tmp" / "legacy_nominal_loads.yaml"
        with legacy_loads_path.open("r", encoding="utf-8") as source:
            self.legacy_loads = yaml.safe_load(source)["areas"]

        with open(self.assets_path, "r", encoding="utf-8") as f:
            self.assets = yaml.safe_load(f)

        self.nodes = [n["id"] for n in self.network["nodes"]]
        self.node_indices = {nid: i for i, nid in enumerate(self.nodes)}
        self.lines = self.network["lines"]
        self.generators = self.assets["generators"]
        self.voll = float(self.assets.get("parameters", {}).get("load_shed_penalty_eur", 3000.0))

    def solve(
        self,
        demand_multipliers: Optional[Dict[str, float]] = None,
        hydro_cost_shifts: Optional[Dict[str, float]] = None,
        line_capacity_overrides: Optional[Dict[str, float]] = None,
        wind_multiplier: float = 1.0,
        generator_availability: Optional[Dict[str, float]] = None,
        solver: str = "MOSEK"
    ) -> Dict[str, Any]:
        """
        Løser nodal DC Economic Dispatch med CVXPY.
        """
        num_nodes = len(self.nodes)
        num_gens = len(self.generators)
        num_lines = len(self.lines)

        # 1. Beslutningsvariabler
        # Produksjon per generator (MW)
        p = cp.Variable(num_gens, name="generation", nonneg=True)

        # Spenningsvinkler (rad) ved hver node (theta)
        theta = cp.Variable(num_nodes, name="phase_angle")

        # Kraftflyt på hver linje (MW)
        f = cp.Variable(num_lines, name="line_flow")

        # Ikke-levert last / load shedding (MW) ved hver node
        s = cp.Variable(num_nodes, name="load_shed", nonneg=True)

        # 2. Forberedelse av parametere
        demand_mult = demand_multipliers or {}
        hydro_shift = hydro_cost_shifts or {}
        cap_overrides = line_capacity_overrides or {}
        generator_availability = generator_availability or {}

        for area, multiplier in demand_mult.items():
            if area not in self.node_indices or float(multiplier) < 0:
                raise ValueError(f"Ugyldig etterspørselsmultiplikator for {area}.")
        if not 0 <= float(wind_multiplier) <= 1:
            raise ValueError("wind_multiplier må være mellom 0 og 1.")
        for generator_id, availability in generator_availability.items():
            if generator_id not in {g["id"] for g in self.generators} or not 0 <= float(availability) <= 1:
                raise ValueError(f"Ugyldig tilgjengelighet for generator {generator_id}.")
        for line_id, capacity in cap_overrides.items():
            if line_id not in {line["id"] for line in self.lines} or float(capacity) < 0:
                raise ValueError(f"Ugyldig kapasitetsgrense for linje {line_id}.")

        # Etterspørsel per node (MW)
        demand = np.array([
            float(self.legacy_loads[n["id"]]) * demand_mult.get(n["id"], 1.0)
            for n in self.network["nodes"]
        ])

        # Generator marginalkostnader og kapasitetsgrenser
        costs = []
        p_min = []
        p_max = []
        for g in self.generators:
            c = float(g["marginal_cost_eur"])
            if "hydro" in g["fuel_type"]:
                c += hydro_shift.get(g["node"], hydro_shift.get("ALL", 0.0))
            costs.append(c)
            availability = float(generator_availability.get(g["id"], 1.0))
            if "wind" in g["fuel_type"]:
                availability *= float(wind_multiplier)
            p_min.append(float(g["min_mw"]) * availability)
            p_max.append(float(g["capacity_mw"]) * availability)

        costs = np.array(costs)
        p_min = np.array(p_min)
        p_max = np.array(p_max)

        # 3. Målfunksjon: Minimer samlede generatorkostnader + straff for lastavbrudd
        objective = cp.Minimize(cp.sum(cp.multiply(costs, p)) + self.voll * cp.sum(s))

        # 4. Bibetingelser
        constraints = []

        # Reference bus fasevinkel settes til 0 (NO1 er slack bus)
        constraints.append(theta[0] == 0)

        # Produksjonsgrenser: P_min <= p <= P_max
        gen_min_con = p >= p_min
        gen_max_con = p <= p_max
        constraints.append(gen_min_con)
        constraints.append(gen_max_con)

        # DC Power flow likninger: f_l = b_l * (theta_from - theta_to)
        # og kapasitetsbegrensninger: -F_max <= f_l <= F_max
        line_max_cons = []
        line_min_cons = []
        for l_idx, line in enumerate(self.lines):
            u_idx = self.node_indices[line["from_node"]]
            v_idx = self.node_indices[line["to_node"]]
            b_val = float(line["susceptance_b"])
            base_mva = float(self.network["system"]["base_mva"])

            # Flyt i MW
            constraints.append(f[l_idx] == b_val * base_mva * (theta[u_idx] - theta[v_idx]))

            cap = float(cap_overrides.get(line["id"], line["capacity_mw"]))
            con_pos = f[l_idx] <= cap
            con_neg = f[l_idx] >= -cap
            line_max_cons.append(con_pos)
            line_min_cons.append(con_neg)
            constraints.append(con_pos)
            constraints.append(con_neg)

        # Nodal kraftbalanse (Kirchhoffs strømlov ved hver node)
        # Sum(p_g i node n) - Sum(f_out) + Sum(f_in) + s_n = d_n
        balance_constraints = []
        for n_idx, node_id in enumerate(self.nodes):
            # Generatorer ved denne noden
            gen_in_node = [g_idx for g_idx, g in enumerate(self.generators) if g["node"] == node_id]
            gen_sum = cp.sum(p[gen_in_node]) if gen_in_node else 0

            # Linjer som forlater denne noden (utstrøm)
            out_lines = [l_idx for l_idx, l in enumerate(self.lines) if l["from_node"] == node_id]
            out_sum = cp.sum(f[out_lines]) if out_lines else 0

            # Linjer som ankommer denne noden (innstrøm)
            in_lines = [l_idx for l_idx, l in enumerate(self.lines) if l["to_node"] == node_id]
            in_sum = cp.sum(f[in_lines]) if in_lines else 0

            bal_con = (gen_sum - out_sum + in_sum + s[n_idx] == demand[n_idx])
            balance_constraints.append(bal_con)
            constraints.append(bal_con)

        # 5. Løs konveks optimering med valgt solver
        problem = cp.Problem(objective, constraints)
        
        solver_enum = getattr(cp, solver, cp.MOSEK)
        try:
            if solver_enum not in cp.installed_solvers():
                raise RuntimeError(f"Solver {solver} er ikke installert.")
            problem.solve(solver=solver_enum)
        except Exception:
            fallback = next((name for name in ("CLARABEL", "SCIPY", "OSQP") if name in cp.installed_solvers()), None)
            if fallback is None:
                raise RuntimeError("Ingen kompatibel CVXPY-solver er installert.")
            problem.solve(solver=fallback)

        # 6. Tolk resultater og hent ut dualvariabler (skyggepriser)
        if problem.status not in [cp.OPTIMAL, cp.OPTIMAL_INACCURATE]:
            return {
                "status": problem.status,
                "feasible": False,
                "message": f"Solver status: {problem.status}"
            }

        # Nodal marginalpris = dualvariabel til kraftbalanse (EUR/MWh)
        nodal_marginal_prices = {}
        for n_idx, node_id in enumerate(self.nodes):
            dual_val = balance_constraints[n_idx].dual_value
            # CVXPY's constraint is written as generation balance == demand.
            # Its stored dual therefore has the opposite sign of d(cost)/d(demand).
            price = -float(dual_val) if dual_val is not None else 0.0
            nodal_marginal_prices[node_id] = round(price, 2)

        # Linjestatus, flyt og overføringsflaskehalser (skyggepris)
        line_results = []
        active_bottlenecks = []
        for l_idx, line in enumerate(self.lines):
            cap = float(cap_overrides.get(line["id"], line["capacity_mw"]))
            flow_val = float(f.value[l_idx])
            utilization = abs(flow_val) / cap if cap > 0 else 0.0

            # Dualvariabel på kapasitetsgrensen (verdi av 1 MW økt kapasitet)
            dual_pos = line_max_cons[l_idx].dual_value
            dual_neg = line_min_cons[l_idx].dual_value
            shadow_price = 0.0
            if dual_pos is not None and abs(dual_pos) > 1e-4:
                shadow_price = float(dual_pos)
            elif dual_neg is not None and abs(dual_neg) > 1e-4:
                shadow_price = float(dual_neg)

            is_binding = utilization >= 0.999 or shadow_price > 0.01
            if is_binding:
                active_bottlenecks.append({
                    "line_id": line["id"],
                    "from_node": line["from_node"],
                    "to_node": line["to_node"],
                    "flow_mw": round(flow_val, 1),
                    "capacity_mw": round(cap, 1),
                    "shadow_price_eur_mw": round(shadow_price, 2)
                })

            line_results.append({
                "line_id": line["id"],
                "from_node": line["from_node"],
                "to_node": line["to_node"],
                "flow_mw": round(flow_val, 1),
                "capacity_mw": round(cap, 1),
                "utilization_pct": round(utilization * 100, 1),
                "shadow_price_eur_mw": round(shadow_price, 2),
                "is_binding": is_binding
            })

        # Generatorfordeling
        gen_results = []
        for g_idx, g in enumerate(self.generators):
            dispatch_mw = float(p.value[g_idx])
            gen_results.append({
                "generator_id": g["id"],
                "name": g["name"],
                "node": g["node"],
                "fuel_type": g["fuel_type"],
                "dispatch_mw": round(dispatch_mw, 1),
                "capacity_mw": round(p_max[g_idx], 1),
                "marginal_cost_eur": round(float(costs[g_idx]), 2),
                "utilization_pct": round((dispatch_mw / p_max[g_idx]) * 100, 1)
            })

        total_shedding = float(np.sum(s.value))

        return {
            "status": problem.status,
            "feasible": True,
            "result_kind": "MODELLED_SINGLE_PERIOD_DC_DISPATCH",
            "network_model": self.network["model_kind"],
            "data_provenance": "ARCHIVED_LEGACY_NOMINAL_LOADS_AND_CONFIGURED_ASSETS_NOT_OBSERVED_GRID_STATE",
            "warnings": ["Dette er en pedagogisk DC-modell med legacy-linjeratings; bruk flow-based-dispatch for JAO-publiserte CNEC/RAM/PTDF-begrensninger."],
            "total_system_cost_eur": round(float(problem.value), 2),
            "nodal_marginal_prices": nodal_marginal_prices,
            "active_bottlenecks": active_bottlenecks,
            "generators": gen_results,
            "lines": line_results,
            "load_shedding_total_mw": round(total_shedding, 2),
            "wind_availability_multiplier": float(wind_multiplier),
            "demand_per_node": {self.nodes[i]: round(float(demand[i]), 1) for i in range(num_nodes)}
        }
