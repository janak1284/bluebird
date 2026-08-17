"""
Synthetic controller state matching the frozen /api/state contract exactly,
powered by the real multi-objective optimizer.

Supports interactive controls:
  - Runtime policy switching (sla-first, cost-first, green)
  - Traffic surges & custom load injection
  - Chaos engineering: node failure / recovery toggles
  - Workload SLA & RPS tuning
  - Manual placement enforcement & Pareto exploration

Exposes LATEST / HISTORY / EVENTS with the same names and shapes as
controller/state.py, so dashboard.py imports either one interchangeably.
"""

import math
import threading
import time
from collections import deque
from typing import Dict, Optional

from optimizer import Node, Workload, optimize, evaluate, explain, POLICIES

LATEST: dict = {}
HISTORY = deque(maxlen=300)
EVENTS = deque(maxlen=100)

CYCLE_S = 60.0
CURRENT_POLICY = "sla-first"

# Simulation controls
AUTO_CYCLE = True
MANUAL_PLACEMENT: Optional[Dict[str, str]] = None
RPS_OVERRIDES: Dict[str, float] = {}
SLA_OVERRIDES: Dict[str, float] = {}
NODE_STATE_OVERRIDES: Dict[str, dict] = {}

_DEFAULT_NODES = {
    "laptop-1": dict(tier="core", zone="core-1", base_latency_ms=40.0,
                     cost_per_hr=2.0, cpu_cores=8.0, idle_w=25.0, max_w=65.0, service_ms=1.2),
    "laptop-2": dict(tier="edge", zone="edge-1", base_latency_ms=3.0,
                     cost_per_hr=9.0, cpu_cores=4.0, idle_w=12.0, max_w=45.0, service_ms=2.0),
    "laptop-3": dict(tier="edge", zone="edge-2", base_latency_ms=4.0,
                     cost_per_hr=9.0, cpu_cores=2.0, idle_w=12.0, max_w=45.0, service_ms=2.5),
    "laptop-4": dict(tier="core", zone="core-2", base_latency_ms=38.0,
                     cost_per_hr=2.2, cpu_cores=8.0, idle_w=25.0, max_w=65.0, service_ms=1.2),
}

_DEFAULT_WORKLOADS = {
    "checkout":  dict(sla_ms=20.0,  cls="latency-critical", allowed_tiers=("edge",),      cores_per_rps=0.004, size_units=1.0),
    "recommend": dict(sla_ms=80.0,  cls="standard",         allowed_tiers=("edge", "core"), cores_per_rps=0.004, size_units=1.0),
    "analytics": dict(sla_ms=500.0, cls="batch",            allowed_tiers=("edge", "core"), cores_per_rps=0.004, size_units=1.0),
}

_current_placement: Dict[str, str] = {
    "checkout": "laptop-2",
    "recommend": "laptop-3",
    "analytics": "laptop-2",
}
_last_moved: Dict[str, float] = {w: 0.0 for w in _DEFAULT_WORKLOADS}
_ORIGIN = time.time()
_last_logged_placement: Optional[Dict[str, str]] = None
_last_logged_policy: str = CURRENT_POLICY


def _phase(t: float) -> float:
    return (t - _ORIGIN) % CYCLE_S


def _calc_rps(name: str, now: float) -> float:
    if name in RPS_OVERRIDES:
        return float(RPS_OVERRIDES[name])
    
    if not AUTO_CYCLE:
        if name == "checkout": return 140.0
        if name == "recommend": return 200.0
        return 300.0

    p = _phase(now)
    if name == "checkout":
        if p < 15:   return 120.0
        if p < 30:   return 120.0 + (p - 15.0) * 42.0   # ramp to ~750
        if p < 50:   return 640.0
        return max(120.0, 640.0 - (p - 50.0) * 52.0)
    if name == "recommend":
        return 200.0 + 60.0 * math.sin(p / 6.0)
    return 300.0 + 40.0 * math.sin(p / 9.0)            # analytics


def _build_nodes() -> Dict[str, Node]:
    nodes = {}
    for name, cfg in _DEFAULT_NODES.items():
        overrides = NODE_STATE_OVERRIDES.get(name, {})
        ready = overrides.get("ready", True)
        base_lat = overrides.get("base_latency_ms", cfg["base_latency_ms"])
        cost = overrides.get("cost_per_hr", cfg["cost_per_hr"])
        idle_w = overrides.get("idle_w", cfg["idle_w"])
        max_w = overrides.get("max_w", cfg["max_w"])
        cores = overrides.get("cpu_cores", cfg["cpu_cores"])

        node = Node(
            name=name,
            tier=cfg["tier"],
            cores=cores,
            base_latency_ms=base_lat,
            cost_per_hr=cost,
            idle_w=idle_w,
            max_w=max_w,
            ready=ready,
            service_ms=cfg["service_ms"],
        )
        node.zone = cfg["zone"]
        nodes[name] = node
    return nodes


def _build_workloads(now: float) -> Dict[str, Workload]:
    workloads = {}
    for name, cfg in _DEFAULT_WORKLOADS.items():
        rps = _calc_rps(name, now)
        sla = SLA_OVERRIDES.get(name, cfg["sla_ms"])
        workloads[name] = Workload(
            name=name,
            rps=rps,
            cores_per_rps=cfg["cores_per_rps"],
            sla_ms=sla,
            allowed_tiers=cfg["allowed_tiers"],
            current_node=_current_placement.get(name),
            last_moved=_last_moved.get(name, 0.0),
            size_units=cfg["size_units"],
        )
    return workloads


def _build_snapshot(now: float) -> dict:
    global _current_placement, _last_moved, _last_logged_placement, _last_logged_policy

    nodes = _build_nodes()
    workloads = _build_workloads(now)

    # Run optimizer or use manual placement
    if MANUAL_PLACEMENT:
        chosen = dict(MANUAL_PLACEMENT)
        eval_objs = evaluate(chosen, nodes, workloads)
        chosen_objs = eval_objs
        front_cands = [(chosen, chosen_objs)]
    else:
        chosen, front_cands, chosen_objs = optimize(nodes, workloads, policy=CURRENT_POLICY, now=now)
        if not chosen:
            chosen = {w: workloads[w].current_node for w in workloads if workloads[w].current_node}
            chosen_objs = evaluate(chosen, nodes, workloads)
            front_cands = [(chosen, chosen_objs)]

    # Compute node utilizations under chosen placement
    load = {n: 0.0 for n in nodes}
    for w_name, n_name in chosen.items():
        w = workloads[w_name]
        load[n_name] += w.rps * w.cores_per_rps

    nodes_out = {}
    for name, n in nodes.items():
        util = min(load[name] / max(n.cores, 0.1), 1.0) if n.ready else 0.0
        nodes_out[name] = {
            "tier": n.tier,
            "zone": getattr(n, "zone", n.tier),
            "cpu_util": round(util, 3),
            "mem_util": round(min(0.18 + util * 0.55, 0.95), 3) if n.ready else 0.0,
            "ready": n.ready,
            "base_latency_ms": n.base_latency_ms,
            "power_w": round(n.idle_w + (n.max_w - n.idle_w) * util, 1) if n.ready else 0.0,
            "cost_per_hr": n.cost_per_hr,
            "cpu_cores": int(n.cores),
        }

    workloads_out = {}
    for w_name, n_name in chosen.items():
        w = workloads[w_name]
        n = nodes[n_name]
        u = nodes_out[n_name]["cpu_util"]
        # M/M/1 queuing approximation
        q_u = min(u, 0.98)
        queue_ms = n.service_ms * (q_u / max(1.0 - q_u, 0.02))
        p99 = round(n.base_latency_ms + 3.0 * queue_ms, 1) if n.ready else 999.0

        workloads_out[w_name] = {
            "node": n_name,
            "p99_ms": p99,
            "rps": round(w.rps),
            "sla_ms": w.sla_ms,
            "class": _DEFAULT_WORKLOADS[w_name]["cls"],
            "last_moved": _last_moved.get(w_name, now),
        }

    # Log policy change event
    if CURRENT_POLICY != _last_logged_policy:
        EVENTS.append(f"POLICY CHANGE: '{_last_logged_policy}' -> '{CURRENT_POLICY}' active")
        _last_logged_policy = CURRENT_POLICY

    # Detect migrations
    if _last_logged_placement and chosen != _last_logged_placement:
        for w_name, new_node in chosen.items():
            old_node = _last_logged_placement.get(w_name)
            if old_node and old_node != new_node:
                _last_moved[w_name] = now
                breached = [name for name, meta in workloads_out.items() if meta["p99_ms"] > meta["sla_ms"]]
                trigger_txt = (f"{breached[0]} p99 {workloads_out[breached[0]]['p99_ms']}ms breaches SLA {workloads_out[breached[0]]['sla_ms']}ms"
                               if breached else f"policy '{CURRENT_POLICY}' optimization rebalance")
                d_cost = nodes_out[new_node]["cost_per_hr"] - nodes_out[old_node]["cost_per_hr"]
                d_power = nodes_out[new_node]["power_w"] - nodes_out[old_node]["power_w"]

                EVENTS.append(f"MIGRATE {w_name}: {old_node} ({nodes[old_node].zone}) -> {new_node} ({nodes[new_node].zone})")
                EVENTS.append(f"  trigger  : {trigger_txt}")
                EVENTS.append(f"  reasoning: {w_name} class={_DEFAULT_WORKLOADS[w_name]['cls']}, sla {workloads[w_name].sla_ms}ms -> {nodes[new_node].tier} safe")
                EVENTS.append(f"  deltas   : cost {d_cost:+.1f}/hr | power {d_power:+.0f}W | migration cost 1 unit")
                EVENTS.append(f"  front    : {len(front_cands)} non-dominated, policy '{CURRENT_POLICY}' selected")

    _last_logged_placement = dict(chosen)
    _current_placement = dict(chosen)

    # Format Pareto front for API
    front_out = []
    for cand_p, objs in front_cands:
        front_out.append({
            "sla_violations": objs.sla_violations,
            "cost_per_hr": round(objs.cost_per_hr, 1),
            "power_w": round(objs.power_w, 1),
            "migration_cost": round(objs.migration_cost, 1),
            "chosen": cand_p == chosen,
            "placement": cand_p,
        })

    active_nodes = {w["node"] for w in workloads_out.values() if w["node"]}
    cost = sum(nodes_out[n]["cost_per_hr"] for n in active_nodes if n in nodes_out)
    power = sum(nodes_out[n]["power_w"] for n in active_nodes if n in nodes_out)
    viol = sum(1 for w in workloads_out.values() if w["p99_ms"] > w["sla_ms"])

    return {
        "timestamp": now,
        "policy": CURRENT_POLICY,
        "nodes": nodes_out,
        "workloads": workloads_out,
        "front": front_out,
        "totals": {
            "cost_per_hr": round(cost, 2),
            "power_w": round(power, 1),
            "sla_violations": viol,
            "nodes_ready": sum(1 for n in nodes_out.values() if n["ready"]),
            "nodes_total": len(nodes_out),
        },
        "simulation": {
            "auto_cycle": AUTO_CYCLE,
            "cycle_phase": round(_phase(now), 1),
            "policy": CURRENT_POLICY,
            "overrides_active": bool(RPS_OVERRIDES or SLA_OVERRIDES or NODE_STATE_OVERRIDES or MANUAL_PLACEMENT),
        }
    }


def _loop():
    while True:
        now = time.time()
        snap = _build_snapshot(now)
        LATEST.clear()
        LATEST.update(snap)
        HISTORY.append({
            "t": now,
            "workloads": {w: d["p99_ms"] for w, d in snap["workloads"].items()},
        })
        time.sleep(1.0)


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    EVENTS.append("SYSTEM: Real-time MODW Optimizer engine initialized")
    EVENTS.append(f"SYSTEM: Active policy '{CURRENT_POLICY}' | Control seam active")
    threading.Thread(target=_loop, daemon=True).start()


# Interactive API helper methods
def set_policy(policy: str) -> bool:
    global CURRENT_POLICY
    if policy in POLICIES:
        CURRENT_POLICY = policy
        return True
    return False


def set_traffic_spike(workload: str, rps: float, auto_cycle: bool = False) -> None:
    global AUTO_CYCLE
    AUTO_CYCLE = auto_cycle
    RPS_OVERRIDES[workload] = rps
    EVENTS.append(f"CHAOS INJECT: Traffic surge on '{workload}' set to {rps:.0f} req/s")


def toggle_node(node_name: str, ready: Optional[bool] = None) -> bool:
    if node_name not in _DEFAULT_NODES:
        return False
    if node_name not in NODE_STATE_OVERRIDES:
        NODE_STATE_OVERRIDES[node_name] = {}
    current = NODE_STATE_OVERRIDES[node_name].get("ready", True)
    new_ready = (not current) if ready is None else ready
    NODE_STATE_OVERRIDES[node_name]["ready"] = new_ready
    status_str = "ONLINE (Healthy)" if new_ready else "OFFLINE (Failed)"
    EVENTS.append(f"CHAOS INJECT: Node '{node_name}' status set to {status_str}")
    return new_ready


def set_workload_config(workload: str, sla_ms: Optional[float] = None, rps: Optional[float] = None) -> None:
    if sla_ms is not None:
        SLA_OVERRIDES[workload] = float(sla_ms)
        EVENTS.append(f"CONFIG UPDATE: Workload '{workload}' SLA target adjusted to {sla_ms}ms")
    if rps is not None:
        RPS_OVERRIDES[workload] = float(rps)
        EVENTS.append(f"CONFIG UPDATE: Workload '{workload}' demand set to {rps} req/s")


def apply_manual_placement(placement: Dict[str, str]) -> None:
    global MANUAL_PLACEMENT
    MANUAL_PLACEMENT = dict(placement)
    EVENTS.append(f"MANUAL OVERRIDE: Applied custom placement {placement}")


def reset_simulation() -> None:
    global AUTO_CYCLE, MANUAL_PLACEMENT, CURRENT_POLICY
    AUTO_CYCLE = True
    MANUAL_PLACEMENT = None
    RPS_OVERRIDES.clear()
    SLA_OVERRIDES.clear()
    NODE_STATE_OVERRIDES.clear()
    CURRENT_POLICY = "sla-first"
    EVENTS.append("SIMULATION RESET: Restored nominal automatic 60-second telemetry cycle")
