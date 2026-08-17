"""
Dashboard backend for the PS-S04 edge-core orchestrator.

Two ways to run it:

  1. Standalone with synthetic data (build the frontend before the
     controller exists):
         DASHBOARD_FAKE=1 uvicorn dashboard:app --host 0.0.0.0 --port 8080

  2. Mounted onto the real controller app:
         from dashboard import router
         app.include_router(router)
     ...and make sure controller/state.py exposes LATEST, HISTORY, EVENTS.

This module is read-only. It never touches the Kubernetes API and never
influences placement.
"""

import os
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI
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
            "policy": snapshot.get("policy", "sla-first"),
            "nodes": snapshot.get("nodes", {}),
            "workloads": snapshot.get("workloads", {}),
            "front": snapshot.get("front", []),
            "events": list(state.EVENTS)[-50:],
            "history": _history_window(now),
            "totals": _totals(snapshot),
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


class PolicyUpdate(BaseModel):
    policy: str

@router.post("/api/policy")
def api_update_policy(payload: PolicyUpdate):
    if payload.policy not in ["sla-first", "cost-first", "green", "auto"]:
        return JSONResponse({"error": "invalid policy"}, status_code=400)
    state.TARGET_POLICY_MODE = payload.policy
    return {"status": "ok", "policy": payload.policy}


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
