# telemetry_producer.py
import os
import time
import json
import urllib.request
import urllib.parse
import subprocess

# Load Node Static Profiles (PS-S04 Blueprint Section 4)
NODE_PROFILES = {
    "willson":       {"alias": "core-master",  "tier": "core", "base_latency_ms": 40, "cost_per_hr": 2.0, "idle_w": 25, "max_w": 65, "cpu_cores": 8},
    "archlinux":    {"alias": "edge-node-01", "tier": "edge", "base_latency_ms": 3,  "cost_per_hr": 9.0, "idle_w": 12, "max_w": 45, "cpu_cores": 4},
    "fedora":       {"alias": "edge-node-02", "tier": "edge", "base_latency_ms": 4,  "cost_per_hr": 9.0, "idle_w": 12, "max_w": 45, "cpu_cores": 4},
    "desktop-prnd0ve": {"alias": "edge-node-03", "tier": "core", "base_latency_ms": 38, "cost_per_hr": 2.2, "idle_w": 25, "max_w": 65, "cpu_cores": 8},
}

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"

def get_live_k3s_nodes():
    """Queries K3s via kubectl to check which nodes are genuinely Ready."""
    try:
        env = os.environ.copy()
        if 'KUBECONFIG' not in env and os.path.exists(os.path.expanduser('~/.kube/config')):
            env['KUBECONFIG'] = os.path.expanduser('~/.kube/config')

        cmd = ["kubectl", "get", "nodes", "-o", "json"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, env=env)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            ready_nodes = {}
            for item in data.get('items', []):
                name = item['metadata']['name']
                is_ready = False
                for cond in item['status'].get('conditions', []):
                    if cond['type'] == 'Ready' and cond['status'] == 'True':
                        is_ready = True
                        break
                ready_nodes[name] = is_ready
            return ready_nodes
    except Exception:
        pass
    return {}

def query_promql(query):
    try:
        url = f"{PROMETHEUS_URL}?{urllib.parse.urlencode({'query': query})}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            out = {}
            for r in data.get('data', {}).get('result', []):
                node = r['metric'].get('node') or r['metric'].get('instance')
                if node:
                    out[node] = float(r['value'][1])
            return out
    except Exception:
        return {}

def generate_snapshot():
    # 1. Fetch live node readiness from K3s
    live_ready_status = get_live_k3s_nodes()
    
    # 2. Fetch live metrics from Prometheus
    cpu_query = '100 - (avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    mem_query = '1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
    
    cpus = query_promql(cpu_query)
    mems = query_promql(mem_query)
    
    nodes_snapshot = {}
    for node_name, profile in NODE_PROFILES.items():
        # Check if node is genuinely online in K3s (checking both hostname & alias)
        is_node_ready = live_ready_status.get(node_name, False) or live_ready_status.get(profile["alias"], False)
        
        display_name = profile["alias"]
        if is_node_ready:
            raw_cpu = cpus.get(node_name, cpus.get(profile["alias"], 5.0))
            cpu_u = min(max(raw_cpu / 100.0, 0.0), 1.0)
            
            raw_mem = mems.get(node_name, mems.get(profile["alias"], 0.20))
            mem_u = min(max(raw_mem, 0.0), 1.0)
            
            power_w = round(profile["idle_w"] + (profile["max_w"] - profile["idle_w"]) * cpu_u, 1)
        else:
            # Offline node state
            cpu_u = 0.0
            mem_u = 0.0
            power_w = 0.0
        
        nodes_snapshot[display_name] = {
            "tier": profile["tier"],
            "cpu_util": round(cpu_u, 2),
            "mem_util": round(mem_u, 2),
            "ready": is_node_ready,
            "base_latency_ms": profile["base_latency_ms"],
            "power_w": power_w,
            "cost_per_hr": profile["cost_per_hr"] if is_node_ready else 0.0,
            "cpu_cores": profile["cpu_cores"]
        }
        
    snapshot = {
        "timestamp": int(time.time()),
        "nodes": nodes_snapshot,
        "workloads": {
            "checkout": {
                "node": "core-master",
                "p99_ms": 14.2,
                "rps": 250,
                "sla_ms": 20,
                "class": "latency-critical",
                "last_moved": int(time.time()) - 100
            }
        }
    }
    
    return snapshot

if __name__ == "__main__":
    snapshot = generate_snapshot()
    print(json.dumps(snapshot, indent=2))
