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

SOURCE = os.getenv("SOURCE", "k8s")

app = FastAPI(title="Edge-Core Orchestrator")
app.include_router(dashboard_router)


def _wire():
    if SOURCE == "k8s":
        from controller.telemetry import collect, measured
        import orchestration.scheduler as executor
        print("[APP] Wired to real k8s telemetry and old orchestration scheduler")
        
        def migrate_wrapper(workload: str, src: str, dst: str):
            executor.execute_make_before_break_migration(workload, dst)
            
        return collect, migrate_wrapper, measured

    from controller.simcollect import collect         # synthetic telemetry
    print("[APP] Wired to synthetic simcollect telemetry")
    return collect, None, None                        # observe-only


@app.on_event("startup")
def _start():
    collect, migrate, measured = _wire()
    threading.Thread(
        target=loop.run,
        kwargs={"collect": collect, "migrate": migrate, "measured": measured},
        daemon=True,
    ).start()
