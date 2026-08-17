import time
from datetime import datetime
from typing import Dict, Optional, Tuple

from controller import state
from optimizer import Node, Workload, node_load, predicted_p99

# --- Config ---
GREEN_WINDOW: Optional[Tuple[int, int]] = None  # e.g. (23, 6)

SLA_ENTER = 0.80
SLA_EXIT = 0.65

PWR_ENTER = 0.75
PWR_EXIT = 0.60

DWELL_S = 20.0

# --- State ---
_current_policy = "sla-first"  # Default starting state
_pending_policy = "sla-first"
_pending_since = 0.0

def _log(msg: str):
    print(f"[META] {msg}")
    ts = datetime.utcnow().strftime('%H:%M:%S')
    state.EVENTS.appendleft(f"[{ts}] AUTO: {msg}")

def evaluate_meta_policy(nodes: Dict[str, Node], workloads: Dict[str, Workload]) -> str:
    global _current_policy, _pending_policy, _pending_since
    now = time.time()

    # 1. Calculate SLA Pressure
    current_placement = {name: w.current_node for name, w in workloads.items() if w.current_node}
    load = node_load(current_placement, nodes, workloads)

    max_sla_ratio = 0.0
    for name, w in workloads.items():
        n_name = w.current_node
        if n_name and n_name in nodes:
            node = nodes[n_name]
            util = load[n_name] / node.cores if node.cores else 0.0
            p99 = predicted_p99(w, node, util)
            max_sla_ratio = max(max_sla_ratio, p99 / w.sla_ms)
            
    # 2. Calculate Active Power Pressure
    active_capacity = 0.0
    active_power = 0.0
    for n_name, n in nodes.items():
        util = load.get(n_name, 0.0) / n.cores if n.cores else 0.0
        if util > 0:
            active_capacity += n.max_w
            active_power += n.idle_w + (n.max_w - n.idle_w) * min(util, 1.0)
            
    pwr_ratio = (active_power / active_capacity) if active_capacity > 0 else 0.0
    
    # 3. Determine ideal policy without hysteresis bounds
    ideal_policy = "cost-first"
    
    if GREEN_WINDOW:
        start, end = GREEN_WINDOW
        hr = datetime.now().hour
        if (start > end and (hr >= start or hr < end)) or (start <= hr < end):
            ideal_policy = "green"
            
    if pwr_ratio >= PWR_ENTER:
        ideal_policy = "green"
        
    if max_sla_ratio >= SLA_ENTER:
        ideal_policy = "sla-first"

    # 4. Hysteresis (Exit bounds)
    if _current_policy == "sla-first" and ideal_policy != "sla-first":
        if max_sla_ratio > SLA_EXIT:
            ideal_policy = "sla-first"
            
    if _current_policy == "green" and ideal_policy != "green":
        if pwr_ratio > PWR_EXIT and max_sla_ratio < SLA_ENTER:
            ideal_policy = "green"

    # 5. Dwell Timer Logic
    if ideal_policy == _current_policy:
        _pending_policy = _current_policy
        _pending_since = 0.0
        return _current_policy

    if ideal_policy != _pending_policy:
        _pending_policy = ideal_policy
        _pending_since = now
        
    # Exception: Instant escalation to sla-first
    if _pending_policy == "sla-first":
        _log(f"SLA pressure at {max_sla_ratio:.0%}. Escalating to sla-first instantly.")
        _current_policy = "sla-first"
        _pending_policy = "sla-first"
        _pending_since = 0.0
        return _current_policy

    # Normal Dwell
    if now - _pending_since >= DWELL_S:
        _log(f"Dwell complete. Mode changing: {_current_policy} -> {_pending_policy}.")
        _current_policy = _pending_policy
        _pending_since = 0.0

    return _current_policy
