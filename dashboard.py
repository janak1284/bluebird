"""
Dashboard backend for the PS-S04 edge-core orchestrator.

Two ways to run it:

  1. Standalone with synthetic data & interactive simulator:
         DASHBOARD_FAKE=1 uvicorn dashboard:app --host 0.0.0.0 --port 8080

  2. Mounted onto the real controller app:
         from dashboard import router
         app.include_router(router)
     ...and make sure controller/state.py exposes LATEST, HISTORY, EVENTS.
"""

import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"
HISTORY_WINDOW_S = 60          # seconds of p99 history sent to the chart
STALE_AFTER_S = 3.0            # header connection indicator threshold

USE_FAKE = os.getenv("DASHBOARD_FAKE") == "1"

if USE_FAKE:
    import fake_state as state
else:
    from controller import state   # LATEST, HISTORY, EVENTS

router = APIRouter()


# ------------------------------------------------------------------ helpers

def _empty_state() -> dict:
    """Shape returned before the controller has published anything."""
    return {
        "timestamp": 0.0,
        "policy": "unknown",
        "nodes": {},
        "workloads": {},
        "front": [],
        "events": [],
        "totals": {
            "cost_per_hr": 0.0,
            "power_w": 0.0,
            "sla_violations": 0,
            "nodes_ready": 0,
            "nodes_total": 0,
        },
        "connected": False,
        "waiting": True,
    }


def _totals(snapshot: dict) -> dict:
    nodes = snapshot.get("nodes", {}) or {}
    workloads = snapshot.get("workloads", {}) or {}

    # only nodes actually carrying a workload are billed / powered up
    active = {w.get("node") for w in workloads.values() if w.get("node")}

    cost = sum(n.get("cost_per_hr", 0.0)
               for name, n in nodes.items() if name in active)
    power = sum(n.get("power_w", 0.0)
                for name, n in nodes.items() if name in active)
    violations = sum(1 for w in workloads.values()
                     if w.get("p99_ms") is not None
                     and w.get("sla_ms") is not None
                     and w["p99_ms"] > w["sla_ms"])

    return {
        "cost_per_hr": round(cost, 2),
        "power_w": round(power, 1),
        "sla_violations": violations,
        "nodes_ready": sum(1 for n in nodes.values() if n.get("ready")),
        "nodes_total": len(nodes),
    }


def _history_window(now: float) -> list:
    cutoff = now - HISTORY_WINDOW_S
    rows = [h for h in list(state.HISTORY) if h.get("t", 0) >= cutoff]
    return rows


# ------------------------------------------------------------------- routes

@router.get("/api/state")
def api_state():
    """
    Full render payload. Never raises: on any inconsistency it returns the
    'waiting' shape so the frontend shows a message instead of blanking.
    """
    try:
        snapshot = dict(state.LATEST or {})
        if not snapshot or not snapshot.get("nodes"):
            return JSONResponse(_empty_state())

        now = time.time()
        ts = snapshot.get("timestamp", 0.0)

        payload = {
            "timestamp": ts,
            "policy": snapshot.get("policy", getattr(state, "CURRENT_POLICY", "sla-first")),
            "nodes": snapshot.get("nodes", {}),
            "workloads": snapshot.get("workloads", {}),
            "front": snapshot.get("front", []),
            "events": list(state.EVENTS)[-100:],
            "history": _history_window(now),
            "totals": snapshot.get("totals") or _totals(snapshot),
            "simulation": snapshot.get("simulation", {}),
            "connected": (now - ts) < STALE_AFTER_S,
            "age_s": round(now - ts, 2),
            "waiting": False,
        }
        return JSONResponse(payload)

    except Exception as exc:                      # never take the UI down
        payload = _empty_state()
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse(payload, status_code=200)


@router.get("/api/history")
def api_history():
    return JSONResponse({"history": _history_window(time.time())})


@router.get("/api/health")
def api_health():
    ts = (state.LATEST or {}).get("timestamp", 0.0)
    return {"ok": True, "controller_age_s": round(time.time() - ts, 2)}


@router.get("/api/policies")
def api_policies():
    return JSONResponse({
        "policies": [
            {
                "id": "sla-first",
                "name": "SLA-First (Reliability)",
                "description": "Prioritizes zero SLA breaches & minimal tail latency, then optimizes migrations, cost, and power.",
                "badge": "Highest Reliability",
                "color": "emerald",
            },
            {
                "id": "cost-first",
                "name": "Cost-First (Economic)",
                "description": "Minimizes monetary cloud & compute expenditure per hour while satisfying hard constraints.",
                "badge": "Lowest Cost",
                "color": "amber",
            },
            {
                "id": "green",
                "name": "Green (Energy-Efficient)",
                "description": "Minimizes cluster power draw (Watts), packing workloads into low-power compute tiers.",
                "badge": "Eco-Friendly",
                "color": "teal",
            },
            {
                "id": "auto",
                "name": "Auto (Metapolicy)",
                "description": "Orchestration engine selects the optimal policy based on cluster state.",
                "badge": "Dynamic",
                "color": "blue",
            },
        ],
        "current": getattr(state, "TARGET_POLICY_MODE", "sla-first"),
    })


@router.post("/api/policy")
async def api_set_policy(request: Request):
    """Switch active optimization policy."""
    try:
        body = await request.json()
        policy = body.get("policy")
        if not policy:
            return JSONResponse({"ok": False, "error": "Missing policy parameter"}, status_code=400)

        if policy not in ["sla-first", "cost-first", "green", "auto"]:
            return JSONResponse({"error": "invalid policy"}, status_code=400)

        if hasattr(state, "set_policy"):
            success = state.set_policy(policy)
            if success:
                return JSONResponse({"ok": True, "policy": policy, "message": f"Policy updated to {policy}"})
        
        # Fallback attribute set
        state.TARGET_POLICY_MODE = policy
        return JSONResponse({"ok": True, "policy": policy, "message": f"Policy set to {policy}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/simulate/spike")
async def api_simulate_spike(request: Request):
    """Inject traffic spike on a workload."""
    try:
        body = await request.json()
        workload = body.get("workload", "checkout")
        rps = float(body.get("rps", 750))
        
        if hasattr(state, "set_traffic_spike"):
            state.set_traffic_spike(workload, rps)
            return JSONResponse({"ok": True, "workload": workload, "rps": rps, "message": f"Spike injected on {workload}: {rps} rps"})
        
        return JSONResponse({"ok": False, "error": "Traffic injection not supported in current mode"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/simulate/node")
async def api_simulate_node(request: Request):
    """Toggle node ready/down state."""
    try:
        body = await request.json()
        node_name = body.get("node")
        ready = body.get("ready")  # None = toggle, True/False = explicit
        
        if hasattr(state, "toggle_node"):
            new_state = state.toggle_node(node_name, ready)
            return JSONResponse({"ok": True, "node": node_name, "ready": new_state})
        
        return JSONResponse({"ok": False, "error": "Node chaos not supported in current mode"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/simulate/reset")
async def api_simulate_reset():
    """Reset simulation overrides back to automatic cycle."""
    try:
        if hasattr(state, "reset_simulation"):
            state.reset_simulation()
            return JSONResponse({"ok": True, "message": "Simulation reset to nominal automatic cycle"})
        return JSONResponse({"ok": True, "message": "Reset not applicable"})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/workload/config")
async def api_workload_config(request: Request):
    """Adjust workload SLA threshold or RPS demand."""
    try:
        body = await request.json()
        workload = body.get("workload")
        sla_ms = body.get("sla_ms")
        rps = body.get("rps")
        
        if hasattr(state, "set_workload_config"):
            state.set_workload_config(workload, sla_ms=sla_ms, rps=rps)
            return JSONResponse({"ok": True, "workload": workload, "sla_ms": sla_ms, "rps": rps})
        
        return JSONResponse({"ok": False, "error": "Workload tuning not supported in current mode"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/placement/apply")
async def api_placement_apply(request: Request):
    """Manually apply a Pareto candidate or custom placement."""
    try:
        body = await request.json()
        placement = body.get("placement")
        if not placement:
            return JSONResponse({"ok": False, "error": "Missing placement"}, status_code=400)
        
        if hasattr(state, "apply_manual_placement"):
            state.apply_manual_placement(placement)
            return JSONResponse({"ok": True, "placement": placement, "message": "Custom placement applied"})
        
        return JSONResponse({"ok": False, "error": "Manual placement not supported in current mode"}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/")
def index():
    path = STATIC_DIR / "index.html"
    if not path.exists():
        return JSONResponse(
            {"error": "static/index.html missing"}, status_code=500
        )
    return FileResponse(path)


# ----------------------------------------------------- standalone entrypoint

app = FastAPI(title="Edge-Core Orchestrator Dashboard")
app.include_router(router)

if USE_FAKE and hasattr(state, "start"):
    @app.on_event("startup")
    def _start_fake():
        state.start()
