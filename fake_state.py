"""
Synthetic controller state matching the frozen /api/state contract exactly.

Drives a scripted 60-second cycle so the dashboard can be built and demoed
before the real controller exists:

    0-15s   steady   : everything on edge, well inside SLA
    15-30s  ramp     : checkout traffic climbs, edge-1 saturates, p99 breaches
    30-35s  migrate  : analytics evicted edge-1 -> core-1, checkout recovers
    35-50s  hold     : stable, mixed placement
    50-60s  recover  : load drops, analytics migrates back to edge

Exposes LATEST / HISTORY / EVENTS with the same names and shapes as
controller/state.py, so dashboard.py imports either one interchangeably.
"""

import math
import threading
import time
from collections import deque

LATEST: dict = {}
HISTORY = deque(maxlen=300)
EVENTS = deque(maxlen=50)

CYCLE_S = 60.0

NODES = {
    "laptop-1": dict(tier="core", zone="core-1", base_latency_ms=40,
                     cost_per_hr=2.0, cpu_cores=8, idle_w=25, max_w=65),
    "laptop-2": dict(tier="edge", zone="edge-1", base_latency_ms=3,
                     cost_per_hr=9.0, cpu_cores=4, idle_w=12, max_w=45),
    "laptop-3": dict(tier="edge", zone="edge-2", base_latency_ms=4,
                     cost_per_hr=9.0, cpu_cores=2, idle_w=12, max_w=45),
    "laptop-4": dict(tier="core", zone="core-2", base_latency_ms=38,
                     cost_per_hr=2.2, cpu_cores=8, idle_w=25, max_w=65),
}

WORKLOADS = {
    "checkout":  dict(sla_ms=20,  cls="latency-critical"),
    "recommend": dict(sla_ms=80,  cls="standard"),
    "analytics": dict(sla_ms=500, cls="batch"),
}

_last_placement: dict = {}


_ORIGIN = time.time()


def _phase(t: float) -> float:
    """Phase within the scripted cycle, anchored to process start so the
    demo always begins in the calm phase rather than mid-surge."""
    return (t - _ORIGIN) % CYCLE_S


def _rps(name: str, p: float) -> float:
    if name == "checkout":
        if p < 15:   return 120
        if p < 30:   return 120 + (p - 15) * 42        # ramp to ~750
        if p < 50:   return 640
        return max(120, 640 - (p - 50) * 52)           # decay back
    if name == "recommend":
        return 200 + 60 * math.sin(p / 6.0)
    return 300 + 40 * math.sin(p / 9.0)                # analytics


def _placement(p: float) -> dict:
    if p < 30:
        return {"checkout": "laptop-2", "recommend": "laptop-3",
                "analytics": "laptop-2"}
    if p < 52:
        return {"checkout": "laptop-2", "recommend": "laptop-3",
                "analytics": "laptop-1"}
    return {"checkout": "laptop-2", "recommend": "laptop-3",
            "analytics": "laptop-2"}


def _p99(node_key: str, util: float, base: float) -> float:
    u = min(util, 0.97)
    return round(base + 3.0 * 2.0 * (u / (1.0 - u)), 1)


def _build(now: float) -> dict:
    global _last_placement
    p = _phase(now)
    placement = _placement(p)

    demand = {n: 0.0 for n in NODES}
    rps = {w: _rps(w, p) for w in WORKLOADS}
    for w, n in placement.items():
        demand[n] += rps[w] * 0.004

    nodes_out = {}
    for name, cfg in NODES.items():
        util = min(demand[name] / cfg["cpu_cores"], 1.0)
        nodes_out[name] = {
            "tier": cfg["tier"],
            "zone": cfg["zone"],
            "cpu_util": round(util, 3),
            "mem_util": round(min(0.2 + util * 0.5, 0.95), 3),
            "ready": True,
            "base_latency_ms": cfg["base_latency_ms"],
            "power_w": round(cfg["idle_w"]
                             + (cfg["max_w"] - cfg["idle_w"]) * util, 1),
            "cost_per_hr": cfg["cost_per_hr"],
            "cpu_cores": cfg["cpu_cores"],
        }

    workloads_out = {}
    for w, meta in WORKLOADS.items():
        n = placement[w]
        workloads_out[w] = {
            "node": n,
            "p99_ms": _p99(n, nodes_out[n]["cpu_util"],
                           NODES[n]["base_latency_ms"]),
            "rps": round(rps[w]),
            "sla_ms": meta["sla_ms"],
            "class": meta["cls"],
            "last_moved": now - (p % 22),
        }

    # emit a migration event when placement changes
    if _last_placement and placement != _last_placement:
        for w in placement:
            if _last_placement.get(w) != placement[w]:
                src, dst = _last_placement[w], placement[w]
                EVENTS.append(
                    f"MIGRATE {w}: {src} ({NODES[src]['zone']}) "
                    f"-> {dst} ({NODES[dst]['zone']})"
                )
                EVENTS.append(
                    f"  trigger  : checkout p99 "
                    f"{workloads_out['checkout']['p99_ms']}ms vs SLA 20ms"
                )
                EVENTS.append(
                    f"  reasoning: {w} is class={WORKLOADS[w]['cls']}, "
                    f"sla {WORKLOADS[w]['sla_ms']}ms -> {NODES[dst]['tier']} safe"
                )
                EVENTS.append(
                    f"  deltas   : cost {NODES[dst]['cost_per_hr'] - NODES[src]['cost_per_hr']:+.1f}/hr"
                    f" | migration cost 1 unit"
                )
                EVENTS.append("  front    : 4 non-dominated, policy 'sla-first' selected #2")
    _last_placement = placement

    active = set(placement.values())
    cost = sum(nodes_out[n]["cost_per_hr"] for n in active)
    power = sum(nodes_out[n]["power_w"] for n in active)
    viol = sum(1 for w in workloads_out.values()
               if w["p99_ms"] > w["sla_ms"])

    front = [
        {"sla_violations": viol, "cost_per_hr": round(cost, 1),
         "power_w": round(power, 1), "migration_cost": 0.0,
         "chosen": True, "placement": placement},
        {"sla_violations": viol, "cost_per_hr": round(cost + 7.0, 1),
         "power_w": round(power - 9, 1), "migration_cost": 1.0,
         "chosen": False,
         "placement": {**placement, "analytics": "laptop-3"}},
        {"sla_violations": viol + 1, "cost_per_hr": round(cost - 7.0, 1),
         "power_w": round(power + 4, 1), "migration_cost": 2.0,
         "chosen": False,
         "placement": {**placement, "checkout": "laptop-1"}},
    ]

    return {
        "timestamp": now,
        "policy": "sla-first",
        "nodes": nodes_out,
        "workloads": workloads_out,
        "front": front,
    }


def _loop():
    while True:
        now = time.time()
        snap = _build(now)
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
    threading.Thread(target=_loop, daemon=True).start()
