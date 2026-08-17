"""
Single-process entrypoint: control loop in a background thread, dashboard
served from the same FastAPI app.

    uvicorn controller.app:app --host 0.0.0.0 --port 8080

Swap SOURCE below between "sim" and "k8s" as the real telemetry and
executor come online. Everything downstream is unchanged.
"""

import os
import threading

from fastapi import FastAPI

from dashboard import router as dashboard_router
from controller import loop

SOURCE = "sim"

app = FastAPI(title="Edge-Core Orchestrator")
app.include_router(dashboard_router)


def _wire():
    if SOURCE == "k8s":
        # from controller.executor import migrate       # P4 builds this
        return None, None

    from controller.simcollect import collect         # synthetic telemetry
    return collect, None                              # observe-only


@app.on_event("startup")
def _start():
    collect, migrate = _wire()
    threading.Thread(
        target=loop.run,
        kwargs={"collect": collect, "migrate": migrate},
        daemon=True,
    ).start()
