"""
The control loop. This is the seam that links every component:

    telemetry  ->  optimizer.optimize()  ->  executor (migrate)
                                         ->  state.publish()  ->  dashboard

Run it alongside the dashboard in one process:

    uvicorn controller.app:app --host 0.0.0.0 --port 8080

The optimizer knows nothing about Kubernetes.
The dashboard knows nothing about the optimizer.
This module is the only thing that knows about both.
"""

import time
import json
import os
import subprocess
import traceback
from typing import Callable, Dict, Optional, Tuple

from controller import state
from controller.metapolicy import evaluate_meta_policy
from optimizer import (Node, Workload, optimize, evaluate, node_load,
                       predicted_p99, node_power)

CONTROL_PERIOD_S = 2.0


# ------------------------------------------------------ snapshot conversion

def build_snapshot(nodes: Dict[str, Node],
                   workloads: Dict[str, Workload],
                   placement: Dict[str, str],
                   front: list,
                   chosen_objs,
                   policy: str,
                   raw_snapshot: Optional[dict] = None) -> dict:
    """
    Turn optimizer-native objects into the frozen /api/state contract.

    This function is the translation layer. If the dashboard contract ever
    changes, it changes here and nowhere else.
    """
    load = node_load(placement, nodes, workloads)
    util = {n: min(load[n] / nodes[n].cores, 1.0) for n in nodes}

    nodes_out = {}
    for name, n in nodes.items():
        c_util = round(util[name], 3)
        m_util = round(min(0.2 + util[name] * 0.5, 0.95), 3)
        p_w = round(node_power(n, util[name]), 1)

        n_rx = 0
        n_tx = 0
        if raw_snapshot and "nodes" in raw_snapshot and name in raw_snapshot["nodes"]:
            raw_n = raw_snapshot["nodes"][name]
            if "cpu_util" in raw_n: c_util = raw_n["cpu_util"]
            if "mem_util" in raw_n: m_util = raw_n["mem_util"]
            if "power_w" in raw_n: p_w = raw_n["power_w"]
            if "net_rx_bps" in raw_n: n_rx = raw_n["net_rx_bps"]
            if "net_tx_bps" in raw_n: n_tx = raw_n["net_tx_bps"]

        nodes_out[name] = {
            "tier": n.tier,
            "raw_hostname": n.raw_hostname,
            "zone": getattr(n, "zone", n.tier),
            "cpu_util": c_util,
            "mem_util": m_util,
            "ready": n.ready,
            "base_latency_ms": n.base_latency_ms,
            "power_w": p_w,
            "cost_per_hr": n.cost_per_hr,
            "cpu_cores": n.cores,
            "net_rx_bps": n_rx,
            "net_tx_bps": n_tx,
        }

    workloads_out = {}
    for w_name, n_name in placement.items():
        w = workloads[w_name]
        workloads_out[w_name] = {
            "node": n_name,
            "p99_ms": round(predicted_p99(w, nodes[n_name], util[n_name]), 1),
            "rps": round(w.rps),
            "sla_ms": w.sla_ms,
            "class": "latency-critical" if w.allowed_tiers == ("edge",)
                     else ("batch" if w.sla_ms >= 200 else "standard"),
            "last_moved": w.last_moved,
            "net_rx_bps": 0,
            "net_tx_bps": 0,
        }

    front_out = []
    for cand_placement, objs in front:
        front_out.append({
            "sla_violations": objs.sla_violations,
            "cost_per_hr": round(objs.cost_per_hr, 1),
            "power_w": round(objs.power_w, 1),
            "migration_cost": objs.migration_cost,
            "chosen": cand_placement == placement,
            "placement": cand_placement,
        })

    return {
        "timestamp": time.time(),
        "policy": policy,
        "nodes": nodes_out,
        "workloads": workloads_out,
        "front": front_out,
    }


# ---------------------------------------------------------- explanation text

def explain_migration(w_name: str, src: str, dst: str,
                      nodes: Dict[str, Node], workloads: Dict[str, Workload],
                      snapshot: dict, front_size: int, policy: str) -> list:
    w = workloads[w_name]
    wl_out = snapshot["workloads"]

    breached = [n for n, d in wl_out.items() if d["p99_ms"] > d["sla_ms"]]
    trigger = (f"{breached[0]} p99 {wl_out[breached[0]]['p99_ms']}ms "
               f"breaches SLA {wl_out[breached[0]]['sla_ms']}ms"
               if breached else "proactive rebalance, no active breach")

    old_placement = {wn: wk.current_node for wn, wk in workloads.items() if wk.current_node}
    new_placement = old_placement.copy()
    new_placement[w_name] = dst
    
    old_obj = evaluate(old_placement, nodes, workloads)
    new_obj = evaluate(new_placement, nodes, workloads)
    
    d_cost = new_obj.cost_per_hr - old_obj.cost_per_hr
    d_pow = new_obj.power_w - old_obj.power_w

    src_tier = nodes[src].tier if src in nodes else "unknown"
    return [
        f"MIGRATE {w_name}: {src} ({src_tier}) "
        f"-> {dst} ({nodes[dst].tier})",
        f"  trigger  : {trigger}",
        f"  reasoning: {w_name} class={wl_out[w_name]['class']}, "
        f"sla {w.sla_ms}ms -> {nodes[dst].tier} placement feasible",
        f"  deltas   : cost {d_cost:+.1f}/hr | power {d_pow:+.0f}W "
        f"| migration cost {w.size_units:.0f} unit",
        f"  front    : {front_size} non-dominated, policy '{policy}'",
    ]


def get_snapshot() -> dict:
    try:
        telemetry_url = os.getenv("TELEMETRY_URL", "http://localhost:8080/api/v1/snapshot")
        result = subprocess.check_output(
            ["curl", "-s", telemetry_url],
            timeout=10
        )
        snapshot = json.loads(result.decode('utf-8'))
        return snapshot
    except Exception as e:
        print(f"Warning: Could not fetch from API via curl ({e}). Returning empty snapshot.")
        return {}

def get_static_nodes() -> dict:
    try:
        nodes_url = os.getenv("NODES_URL", "http://localhost:8080/api/v1/all-nodes")
        result = subprocess.check_output(
            ["curl", "-s", nodes_url],
            timeout=10
        )
        data = json.loads(result.decode('utf-8'))
        return data.get("nodes", {})
    except Exception as e:
        print(f"Warning: Could not fetch static nodes from /all-nodes ({e}).")
        return {}

def parse_snapshot(snapshot: dict, static_nodes: dict) -> Tuple[Dict[str, Node], Dict[str, Workload]]:
    nodes = {}
    for name, data in snapshot.get("nodes", {}).items():
        static = static_nodes.get(name, {})
        is_edge = "edge" in name.lower()
        tier = static.get("tier") or data.get("tier") or ("edge" if is_edge else "core")
        cores = static.get("cpu_cores") or data.get("cpu_cores") or (4.0 if is_edge else 8.0)
        base_lat = static.get("base_latency_ms") or data.get("base_latency_ms") or (4.0 if is_edge else 40.0)
        cost = static.get("cost_per_hr") or data.get("cost_per_hr") or (9.0 if is_edge else 2.0)
        idle_w = static.get("idle_w") or data.get("idle_w") or (12.0 if is_edge else 25.0)
        max_w = static.get("max_w") or data.get("max_w") or (45.0 if is_edge else 65.0)
        
        n = Node(name=name, raw_hostname=data.get("raw_hostname", ""), tier=tier, cores=cores, base_latency_ms=base_lat, cost_per_hr=cost, idle_w=idle_w, max_w=max_w, ready=data.get("ready", True), current_power_w=data.get("power_w", idle_w))
        n.zone = static.get("zone") or data.get("zone") or tier
        nodes[name] = n
    workloads = {
        name: Workload(
            name,
            data.get("rps", 100),
            data.get("cores_per_rps", 0.001),
            data.get("sla_ms", 100.0),
            tuple(data.get("allowed_tiers", ["edge", "core"])),
            data.get("current_node") or data.get("node"),
            data.get("last_moved", 0.0),
            data.get("size_units", 1.0)
        )
        for name, data in snapshot.get("workloads", {}).items()
    }
    return nodes, workloads

# --------------------------------------------------------------- the loop

def run(collect: Optional[Callable[[], tuple]] = None,
        migrate: Optional[Callable[[str, str, str], None]] = None,
        period: float = CONTROL_PERIOD_S,
        stop: Optional[Callable[[], bool]] = None,
        measured: Optional[Callable] = None,
        **kwargs) -> None:
    static_nodes = {}
    if not collect:
        static_nodes = get_static_nodes()

    while not (stop and stop()):
        try:
            mode = getattr(state, "TARGET_POLICY_MODE", "sla-first")
            raw_snapshot = None
            if collect:
                collect_res = collect()
                if len(collect_res) == 3:
                    nodes, workloads, raw_snapshot = collect_res
                else:
                    nodes, workloads = collect_res
            else:
                raw_snapshot = get_snapshot()
                nodes, workloads = parse_snapshot(raw_snapshot, static_nodes)

            if mode == "auto":
                policy = evaluate_meta_policy(nodes, workloads)
            else:
                policy = mode
            state.ACTIVE_POLICY = policy

            placement, front, objs = optimize(nodes, workloads, policy)
            if not placement:
                time.sleep(period)
                continue

            snapshot = build_snapshot(nodes, workloads, placement,
                                      front, objs, policy, raw_snapshot=raw_snapshot)
            snapshot["policy_mode"] = mode
            if measured:
                try:
                    real_metrics = measured()
                    for w_name, mets in real_metrics.items():
                        if w_name in snapshot["workloads"]:
                            snapshot["workloads"][w_name]["p99_ms"] = mets.get("p99_ms", snapshot["workloads"][w_name]["p99_ms"])
                            snapshot["workloads"][w_name]["rps"] = mets.get("rps", snapshot["workloads"][w_name]["rps"])
                            if "net_rx_bps" in mets:
                                snapshot["workloads"][w_name]["net_rx_bps"] = mets["net_rx_bps"]
                            if "net_tx_bps" in mets:
                                snapshot["workloads"][w_name]["net_tx_bps"] = mets["net_tx_bps"]
                except Exception as e:
                    print(f"Failed to fetch measured telemetry: {e}")

            # diff against reality to find what needs to move
            events = []
            moves = [(w, workloads[w].current_node, n)
                     for w, n in placement.items()
                     if workloads[w].current_node
                     and workloads[w].current_node != n]

            for w_name, src, dst in moves:
                events += explain_migration(w_name, src, dst, nodes,
                                            workloads, snapshot,
                                            len(front), policy)

            # publish BEFORE migrating so the dashboard shows the decision
            # and its justification while the migration is in flight
            state.publish(snapshot, events)

            if migrate:
                for w_name, src, dst in moves:
                    try:
                        migrate(w_name, src, dst)
                        workloads[w_name].last_moved = time.time()
                    except Exception:
                        state.EVENTS.append(
                            f"  ERROR    : migration of {w_name} failed")
                        traceback.print_exc()

        except Exception as exc:
            state.EVENTS.append(f"  ERROR    : control loop -- {exc}")
            traceback.print_exc()

        time.sleep(period)
