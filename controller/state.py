"""
Shared in-memory state. The single seam between the control loop and the
dashboard.

The control loop WRITES these. The dashboard READS them. Nothing else
touches them. Same names and shapes as fake_state.py, so dashboard.py
imports either one interchangeably.
"""

from collections import deque

# Most recent full snapshot, replaced wholesale each control cycle.
# Shape matches the frozen /api/state contract.
LATEST: dict = {}

# Rolling p99 history for the latency chart.
# Each row: {"t": epoch_float, "workloads": {name: p99_ms}}
HISTORY = deque(maxlen=300)

# Pre-formatted decision log lines, newest last. Rendered verbatim.
EVENTS = deque(maxlen=100)

CURRENT_POLICY = "sla-first"


def set_policy(policy: str) -> bool:
    global CURRENT_POLICY
    CURRENT_POLICY = policy
    EVENTS.append(f"POLICY CHANGE: Requested '{policy}'")
    return True


def publish(snapshot: dict, events: list | None = None) -> None:
    """Atomically swap in a new snapshot and append any new log lines."""
    LATEST.clear()
    LATEST.update(snapshot)

    HISTORY.append({
        "t": snapshot["timestamp"],
        "workloads": {w: d["p99_ms"] for w, d in snapshot["workloads"].items()},
    })

    for line in (events or []):
        EVENTS.append(line)
