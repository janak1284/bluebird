# controller/telemetry.py
# Real Telemetry Collector bridging telemetry_api_server.py to Optimizer Control Loop

import os
import time
import json
import urllib.request
from typing import Dict, Tuple
from optimizer import Node, Workload

TELEMETRY_URL = os.getenv("TELEMETRY_URL", "http://localhost:8080/api/v1/snapshot")
NODES_URL = os.getenv("NODES_URL", "http://localhost:8080/api/v1/all-nodes")

def fetch_json(url: str, timeout: float = 2.0) -> dict:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'OrchestratorTelemetryCollector/1.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"⚠️ Telemetry fetch from {url} failed: {e}")
    return {}

def collect() -> Tuple[Dict[str, Node], Dict[str, Workload]]:
    """Polls real Telemetry REST API server and returns (nodes, workloads) dataclass dicts for Optimizer."""
    raw_snap = fetch_json(TELEMETRY_URL)
    raw_nodes = fetch_json(NODES_URL).get("nodes", {})
    
    snap_nodes = raw_snap.get("nodes", {})
    nodes = {}
    
    for name in set(list(snap_nodes.keys()) + list(raw_nodes.keys())):
        n_data = snap_nodes.get(name, {})
        n_static = raw_nodes.get(name, {})
        
        is_edge = "edge" in name.lower() or n_data.get("tier") == "edge" or n_static.get("tier") == "edge"
        tier = n_static.get("tier") or n_data.get("tier") or ("edge" if is_edge else "core")
        cores = n_static.get("cpu_cores") or n_data.get("cpu_cores") or (8.0 if is_edge else 16.0)
        base_lat = n_static.get("base_latency_ms") or n_data.get("base_latency_ms") or (4.0 if is_edge else 40.0)
        cost = n_static.get("cost_per_hr") or n_data.get("cost_per_hr") or (9.0 if is_edge else 2.0)
        idle_w = n_static.get("idle_w") or n_data.get("idle_w") or (12.0 if is_edge else 25.0)
        max_w = n_static.get("max_w") or n_data.get("max_w") or (45.0 if is_edge else 135.0)
        ready = n_data.get("ready", True) if name in snap_nodes else False
        
        node_obj = Node(name, tier, float(cores), float(base_lat), float(cost), float(idle_w), float(max_w), ready)
        node_obj.zone = n_static.get("zone") or n_data.get("zone") or tier
        nodes[name] = node_obj

    # Parse workload SLA profiles
    snap_workloads = raw_snap.get("workloads", {})
    workloads = {}

    for raw_name, data in snap_workloads.items():
        # Strip pod hash suffix to get logical Deployment name
        clean_name = raw_name
        for prefix in ["checkout-critical-01", "user-profile-std-01", "analytics-batch-01"]:
            if raw_name.startswith(prefix):
                clean_name = prefix
                break

        wl_class = data.get("class", "")
        if "critical" in wl_class or "critical" in clean_name:
            allowed_tiers = ("edge",)
        elif "batch" in wl_class or "analytics" in clean_name:
            allowed_tiers = ("core",)
        else:
            allowed_tiers = ("edge", "core")

        current_n = data.get("node") or data.get("raw_node") or data.get("current_node")

        w_obj = Workload(
            name=clean_name,
            rps=float(data.get("rps", 100)),
            cores_per_rps=float(data.get("cores_per_rps", 0.001)),
            sla_ms=float(data.get("sla_ms", 100.0)),
            allowed_tiers=allowed_tiers,
            current_node=current_n,
            last_moved=float(data.get("last_moved", 0.0)),
            size_units=float(data.get("size_units", 1.0))
        )
        workloads[clean_name] = w_obj

    return nodes, workloads

def measured() -> Dict[str, dict]:
    """Returns real measured P99 latency and RPS metrics per workload for dashboard display."""
    raw_snap = fetch_json(TELEMETRY_URL)
    snap_workloads = raw_snap.get("workloads", {})
    metrics = {}
    for raw_name, data in snap_workloads.items():
        clean_name = raw_name
        for prefix in ["checkout-critical-01", "user-profile-std-01", "analytics-batch-01"]:
            if raw_name.startswith(prefix):
                clean_name = prefix
                break
        metrics[clean_name] = {
            "p99_ms": float(data.get("p99_ms", data.get("sla_ms", 50.0))),
            "rps": float(data.get("rps", 100.0))
        }
    return metrics
