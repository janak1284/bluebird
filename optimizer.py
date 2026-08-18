"""
Multi-objective placement optimizer for the edge-core orchestrator.

Input : a telemetry snapshot (nodes + workloads + current placement)
Output: a Placement dict {workload_name: node_name}, plus the Pareto front
        and a human-readable explanation of why that point was chosen.

No weights anywhere. Objectives stay separate; selection is an explicit policy.
"""

from dataclasses import dataclass
from itertools import product
from typing import Dict, List, Optional, Tuple
import time

# ---------------------------------------------------------------- data model

@dataclass
class Node:
    name: str
    tier: str                # "edge" | "core"
    cores: float             # allocatable
    base_latency_ms: float   # injected via tc netem
    cost_per_hr: float
    idle_w: float
    max_w: float
    ready: bool = True
    service_ms: float = 2.0  # mean per-request service time on this node
    raw_hostname: str = ""

@dataclass
class Workload:
    name: str
    rps: float               # measured request rate
    cores_per_rps: float     # measured demand coefficient
    sla_ms: float
    allowed_tiers: Tuple[str, ...]   # ("edge",) | ("edge","core")
    current_node: Optional[str] = None
    last_moved: float = 0.0
    size_units: float = 1.0  # migration weight (state size / image size)

Placement = Dict[str, str]

DWELL_SECONDS = 30.0


# ------------------------------------------------------- physics / cost model

def node_load(placement: Placement, nodes: Dict[str, Node],
              workloads: Dict[str, Workload]) -> Dict[str, float]:
    """Total core demand landing on each node under this placement."""
    load = {n: 0.0 for n in nodes}
    for w_name, n_name in placement.items():
        w = workloads[w_name]
        load[n_name] += w.rps * w.cores_per_rps
    return load


def predicted_p99(w: Workload, node: Node, utilization: float) -> float:
    """
    Base network latency + queuing delay.

    Queuing uses an M/M/1 approximation: as utilization -> 1, delay explodes.
    This is the term that makes an overloaded 3ms edge node worse than a
    lightly loaded 40ms core node -- i.e. the whole point of the system.
    """
    u = min(utilization, 0.99)
    queue_ms = node.service_ms * (u / (1.0 - u))
    # p99 tail factor over the mean; crude but defensible and monotone
    return node.base_latency_ms + 3.0 * queue_ms


def node_power(node: Node, utilization: float) -> float:
    return node.idle_w + (node.max_w - node.idle_w) * min(utilization, 1.0)


# ------------------------------------------------------------ hard constraints

def feasible(placement: Placement, nodes: Dict[str, Node],
             workloads: Dict[str, Workload], now: float) -> bool:
    load = node_load(placement, nodes, workloads)

    for w_name, n_name in placement.items():
        w, n = workloads[w_name], nodes[n_name]

        if not n.ready:                       # node down
            return False
        if n.tier not in w.allowed_tiers:     # class forbids this tier
            return False
        if (n_name != w.current_node
                and now - w.last_moved < DWELL_SECONDS):   # dwell floor
            return False

    for n_name, used in load.items():         # capacity
        if used > nodes[n_name].cores:
            return False

    return True


# --------------------------------------------------------------- objectives

@dataclass
class Objectives:
    sla_violations: int      # minimise
    total_breach_ms: float   # minimise (tiebreak within violation count)
    cost_per_hr: float       # minimise
    power_w: float           # minimise
    migration_cost: float    # minimise

    def vector(self) -> Tuple[float, ...]:
        return (self.sla_violations, self.cost_per_hr,
                self.power_w, self.migration_cost)


def evaluate(placement: Placement, nodes: Dict[str, Node],
             workloads: Dict[str, Workload]) -> Objectives:
    load = node_load(placement, nodes, workloads)
    util = {n: load[n] / nodes[n].cores for n in nodes}

    violations, breach_ms = 0, 0.0
    for w_name, n_name in placement.items():
        w = workloads[w_name]
        p99 = predicted_p99(w, nodes[n_name], util[n_name])
        if p99 > w.sla_ms:
            violations += 1
            breach_ms += p99 - w.sla_ms

    # only nodes actually carrying load are billed / powered up
    active = [n for n in nodes if load[n] > 0]
    cost = sum(nodes[n].cost_per_hr for n in active)
    power = sum(node_power(nodes[n], util[n]) for n in active)

    migration = sum(workloads[w].size_units
                    for w, n in placement.items()
                    if workloads[w].current_node not in (None, n))

    return Objectives(violations, breach_ms, cost, power, migration)


# --------------------------------------------------------------- Pareto front

def dominates(a: Tuple[float, ...], b: Tuple[float, ...]) -> bool:
    """a dominates b: no worse on every objective, strictly better on one."""
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


def pareto_front(cands: List[Tuple[Placement, Objectives]]
                 ) -> List[Tuple[Placement, Objectives]]:
    front = []
    for p_i, o_i in cands:
        v_i = o_i.vector()
        if not any(dominates(o_j.vector(), v_i)
                   for p_j, o_j in cands if p_j is not p_i):
            front.append((p_i, o_i))
    return front


def crowding_distance(front: List[Tuple[Placement, Objectives]]
                      ) -> List[float]:
    """NSGA-II crowding distance: prefer points in sparse regions."""
    n = len(front)
    dist = [0.0] * n
    if n <= 2:
        return [float("inf")] * n

    for k in range(len(front[0][1].vector())):
        order = sorted(range(n), key=lambda i: front[i][1].vector()[k])
        lo = front[order[0]][1].vector()[k]
        hi = front[order[-1]][1].vector()[k]
        dist[order[0]] = dist[order[-1]] = float("inf")
        span = (hi - lo) or 1.0
        for r in range(1, n - 1):
            prev = front[order[r - 1]][1].vector()[k]
            nxt = front[order[r + 1]][1].vector()[k]
            dist[order[r]] += (nxt - prev) / span
    return dist


# ------------------------------------------------------------ policy selection

POLICIES = {
    # ordered objective keys, applied lexicographically
    "sla-first":  ("sla_violations", "total_breach_ms",
                   "migration_cost", "cost_per_hr", "power_w"),
    "cost-first": ("cost_per_hr", "sla_violations",
                   "migration_cost", "power_w"),
    "green":      ("power_w", "sla_violations",
                   "migration_cost", "cost_per_hr"),
}

EPS = 0.05  # values within 5% are treated as tied -> fall through


def approx_equal(a: float, b: float) -> bool:
    return abs(a - b) <= EPS * max(abs(a), abs(b), 1e-9)


def select(front: List[Tuple[Placement, Objectives]],
           policy: str = "sla-first") -> Tuple[Placement, Objectives]:
    keys = POLICIES[policy]
    pool = list(range(len(front)))

    for key in keys:
        best = min(getattr(front[i][1], key) for i in pool)
        pool = [i for i in pool
                if approx_equal(getattr(front[i][1], key), best)]
        if len(pool) == 1:
            return front[pool[0]]

    dists = crowding_distance(front)
    return front[max(pool, key=lambda i: dists[i])]


# ------------------------------------------------------------------- the loop

def optimize(nodes: Dict[str, Node], workloads: Dict[str, Workload],
             policy: str = "sla-first", now: Optional[float] = None):
    now = now if now is not None else time.time()

    names = list(workloads)
    options = [[n for n in nodes if nodes[n].tier in workloads[w].allowed_tiers]
               for w in names]

    candidates = []
    for combo in product(*options):
        placement = dict(zip(names, combo))
        if feasible(placement, nodes, workloads, now):
            candidates.append((placement, evaluate(placement, nodes, workloads)))

    if not candidates:                       # nothing legal -> hold position
        current = {w: workloads[w].current_node for w in names
                   if workloads[w].current_node}
        return current, [], None

    front = pareto_front(candidates)
    chosen, objs = select(front, policy)
    return chosen, front, objs


# ------------------------------------------------------------- explainability

def explain(chosen: Placement, objs: Objectives,
            front: List[Tuple[Placement, Objectives]],
            workloads: Dict[str, Workload], policy: str) -> List[str]:
    lines = []
    for w_name, n_name in chosen.items():
        old = workloads[w_name].current_node
        if old and old != n_name:
            lines.append(f"MIGRATE {w_name}: {old} -> {n_name}")
    lines.append(
        f"  objectives : violations={objs.sla_violations} "
        f"cost={objs.cost_per_hr:.1f}/hr power={objs.power_w:.0f}W "
        f"migrations={objs.migration_cost:.0f}"
    )
    lines.append(f"  front      : {len(front)} non-dominated, policy '{policy}'")
    return lines
