"""
Synthetic telemetry in optimizer-native types.

Stand-in for controller/telemetry.py (P2) so the full chain --
collect -> optimize -> publish -> dashboard -- runs end to end before the
cluster or load generator exist.

P2's real collect() must return the same two dicts. Nothing else changes.
"""

import math
import time
from typing import Dict, Tuple

from optimizer import Node, Workload

_ORIGIN = time.time()
CYCLE_S = 60.0

_NODES = {
    "laptop-1": Node("laptop-1", "core", 8, 40, 2.0, 25, 65, service_ms=1.2),
    "laptop-2": Node("laptop-2", "edge", 4, 3,  9.0, 12, 45, service_ms=2.0),
    "laptop-3": Node("laptop-3", "edge", 2, 4,  9.0, 12, 45, service_ms=2.5),
    "laptop-4": Node("laptop-4", "core", 8, 38, 2.2, 25, 65, service_ms=1.2),
}
for _n, _z in [("laptop-1", "core-1"), ("laptop-2", "edge-1"),
               ("laptop-3", "edge-2"), ("laptop-4", "core-2")]:
    _NODES[_n].zone = _z

# where things are actually running right now; updated by the executor
CURRENT = {
    "checkout":  "laptop-2",
    "recommend": "laptop-3",
    "analytics": "laptop-2",
}
LAST_MOVED = {w: 0.0 for w in CURRENT}


def _rps(name: str, p: float) -> float:
    if name == "checkout":
        if p < 15:  return 120
        if p < 30:  return 120 + (p - 15) * 42
        if p < 50:  return 640
        return max(120, 640 - (p - 50) * 52)
    if name == "recommend":
        return 200 + 60 * math.sin(p / 6.0)
    return 300 + 40 * math.sin(p / 9.0)


def collect() -> Tuple[Dict[str, Node], Dict[str, Workload]]:
    p = (time.time() - _ORIGIN) % CYCLE_S

    workloads = {
        "checkout": Workload("checkout", _rps("checkout", p), 0.004, 20,
                             ("edge",), CURRENT["checkout"],
                             LAST_MOVED["checkout"]),
        "recommend": Workload("recommend", _rps("recommend", p), 0.004, 80,
                              ("edge", "core"), CURRENT["recommend"],
                              LAST_MOVED["recommend"]),
        "analytics": Workload("analytics", _rps("analytics", p), 0.004, 500,
                              ("edge", "core"), CURRENT["analytics"],
                              LAST_MOVED["analytics"]),
    }
    return _NODES, workloads


def migrate(workload: str, src: str, dst: str) -> None:
    """Stand-in executor: instant, always succeeds."""
    CURRENT[workload] = dst
    LAST_MOVED[workload] = time.time()
