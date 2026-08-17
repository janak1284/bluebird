# telemetry_api_server.py
import os
import sys
import time
import glob
import json
import urllib.request
import urllib.parse
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

# Node Profiles Configuration (PS-S04 Blueprint Section 4)
NODE_PROFILES = {
    "willson":       {"alias": "core-master",  "tier": "core", "zone": "core-1", "base_latency_ms": 40, "cost_per_hr": 2.0, "idle_w": 7.5, "max_w": 30,  "cpu_cores": 16, "total_mem_gb": 16},
    "archlinux":    {"alias": "core-node-01", "tier": "core", "zone": "core-2", "base_latency_ms": 42, "cost_per_hr": 2.1, "idle_w": 25.0, "max_w": 135, "cpu_cores": 12, "total_mem_gb": 8},
    "fedora":       {"alias": "edge-node-01", "tier": "edge", "zone": "edge-1", "base_latency_ms": 4,  "cost_per_hr": 9.0, "idle_w": 12.0, "max_w": 45,  "cpu_cores": 8,  "total_mem_gb": 8},
    "desktop-prnd0ve": {"alias": "edge-node-02", "tier": "edge", "zone": "edge-2", "base_latency_ms": 3,  "cost_per_hr": 9.0, "idle_w": 8.0,  "max_w": 35,  "cpu_cores": 8,  "total_mem_gb": 16},
}

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"

_last_cpu_sample = {"idle": 0.0, "total": 0.0}
_cache = {
    "nodes_ts": 0, "nodes_data": {},
    "pods_ts": 0, "pods_data": {}
}

def get_local_proc_stat_cpu_util():
    """Reads real-time CPU utilization for local host directly from /proc/stat (exact Conky math)."""
    global _last_cpu_sample
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
            parts = line.split()[1:]
            values = [float(x) for x in parts]
            idle = values[3] + values[4]  # idle + iowait
            total = sum(values)
            
            dt_idle = idle - _last_cpu_sample["idle"]
            dt_total = total - _last_cpu_sample["total"]
            
            _last_cpu_sample["idle"] = idle
            _last_cpu_sample["total"] = total
            
            if dt_total > 0 and _last_cpu_sample["total"] > 0:
                util = 1.0 - (dt_idle / dt_total)
                return min(max(util, 0.0), 1.0)
    except Exception:
        pass
    return None

def get_real_sysfs_power_w():
    """Reads actual physical power draw directly from Linux sysfs (exact interface Conky uses)."""
    try:
        power_files = glob.glob('/sys/class/power_supply/BAT*/power_now')
        for pf in power_files:
            with open(pf, 'r') as f:
                val = float(f.read().strip())
                if val > 0:
                    return round(val / 1e6, 2)
        
        curr_files = glob.glob('/sys/class/power_supply/BAT*/current_now')
        volt_files = glob.glob('/sys/class/power_supply/BAT*/voltage_now')
        if curr_files and volt_files:
            with open(curr_files[0], 'r') as fc, open(volt_files[0], 'r') as fv:
                c = float(fc.read().strip()) / 1e6
                v = float(fv.read().strip()) / 1e6
                if c > 0 and v > 0:
                    return round(c * v, 2)
    except Exception:
        pass
    return None

def get_kube_env():
    env = os.environ.copy()
    if 'KUBECONFIG' not in env and os.path.exists(os.path.expanduser('~/.kube/config')):
        env['KUBECONFIG'] = os.path.expanduser('~/.kube/config')
    return env

def get_live_k3s_node_info():
    """Queries K3s via kubectl (with 2s TTL cache for fast HTTP performance)."""
    now = time.time()
    if now - _cache["nodes_ts"] < 2.0 and _cache["nodes_data"]:
        return _cache["nodes_data"]
        
    nodes = {}
    try:
        cmd = ["kubectl", "get", "nodes", "-o", "json"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, env=get_kube_env())
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for item in data.get('items', []):
                name = item['metadata']['name']
                is_ready = False
                for cond in item['status'].get('conditions', []):
                    if cond['type'] == 'Ready' and cond['status'] == 'True':
                        is_ready = True
                        break
                
                internal_ip = None
                for addr in item['status'].get('addresses', []):
                    if addr['type'] == 'InternalIP':
                        internal_ip = addr['address']
                        break
                        
                nodes[name] = {"ready": is_ready, "ip": internal_ip}
            _cache["nodes_data"] = nodes
            _cache["nodes_ts"] = now
    except Exception:
        pass
    return _cache["nodes_data"] if _cache["nodes_data"] else nodes

def query_promql(query):
    try:
        url = f"{PROMETHEUS_URL}?{urllib.parse.urlencode({'query': query})}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            out = {}
            for r in data.get('data', {}).get('result', []):
                metric = r['metric']
                val = float(r['value'][1])
                
                instance = metric.get('instance', '')
                node = metric.get('node', '') or metric.get('nodename', '')
                ip = instance.split(':')[0] if instance else ''
                
                if node:
                    out[node] = val
                if ip:
                    out[ip] = val
            return out
    except Exception:
        return {}

def get_live_k3s_pods(node_cpu_map, live_nodes_info):
    """Queries K3s for live workload pods and cross-checks node readiness status."""
    now = time.time()
    if now - _cache["pods_ts"] < 2.0 and _cache["pods_data"]:
        pods = _cache["pods_data"]
        for p_name, p_info in pods.items():
            disp_node = p_info.get("node")
            raw_node = p_info.get("raw_node")
            is_node_ready = live_nodes_info.get(raw_node, {}).get("ready", False)
            
            if not is_node_ready:
                p_info["status"] = "NodeOffline"
                p_info["p99_ms"] = 0.0
                p_info["rps"] = 0
            else:
                profile = NODE_PROFILES.get(raw_node, {"base_latency_ms": 10})
                base_lat = profile.get("base_latency_ms", 10)
                cpu_u = node_cpu_map.get(disp_node, 0.05)
                rps = p_info.get("rps", 250)
                
                queuing_delay = (rps / 100.0) * (1.0 / max(1.0 - min(cpu_u, 0.95), 0.05))
                p_info["p99_ms"] = round(base_lat + queuing_delay, 1)
        return pods

    pods = {}
    try:
        cmd = ["kubectl", "get", "pods", "-n", "default", "-o", "json"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=2, env=get_kube_env())
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for item in data.get('items', []):
                pod_name = item['metadata']['name']
                node_name = item['spec'].get('nodeName', 'unassigned')
                phase = item['status'].get('phase', 'Unknown')
                
                is_node_ready = live_nodes_info.get(node_name, {}).get("ready", False)
                
                profile = NODE_PROFILES.get(node_name, {"alias": node_name, "base_latency_ms": 10})
                display_node = profile.get("alias", node_name)
                base_lat = profile.get("base_latency_ms", 10)
                
                labels = item['metadata'].get('labels', {})
                app_class = labels.get('workload-class')
                if not app_class:
                    if "checkout" in pod_name or "critical" in pod_name:
                        app_class = "latency-critical"
                    elif "std" in pod_name or "profile" in pod_name:
                        app_class = "standard"
                    else:
                        app_class = "batch-analytics"
                
                sla_map = {
                    "latency-critical": 20,
                    "standard": 100,
                    "batch-analytics": 500
                }
                sla_ms = sla_map.get(app_class, 100)
                
                if not is_node_ready:
                    effective_status = "NodeOffline"
                    p99_ms = 0.0
                    rps = 0
                else:
                    effective_status = phase
                    cpu_u = node_cpu_map.get(display_node, 0.05)
                    rps = 250 if phase == "Running" else 0
                    queuing_delay = (rps / 100.0) * (1.0 / max(1.0 - min(cpu_u, 0.95), 0.05))
                    p99_ms = round(base_lat + queuing_delay, 1) if phase == "Running" else 0.0
                
                pods[pod_name] = {
                    "pod_name": pod_name,
                    "node": display_node,
                    "raw_node": node_name,
                    "status": effective_status,
                    "class": app_class,
                    "sla_ms": sla_ms,
                    "p99_ms": p99_ms,
                    "rps": rps,
                    "last_moved": int(time.time()) - 120
                }
            _cache["pods_data"] = pods
            _cache["pods_ts"] = now
    except Exception:
        pass
    return _cache["pods_data"] if _cache["pods_data"] else pods

def get_base_node_profiles():
    """Returns ONLY the static base data for all 4 nodes."""
    nodes_base = {}
    for hostname, profile in NODE_PROFILES.items():
        display_name = profile["alias"]
        nodes_base[display_name] = {
            "node_name": display_name,
            "raw_hostname": hostname,
            "tier": profile["tier"],
            "zone": profile["zone"],
            "base_latency_ms": profile["base_latency_ms"],
            "cost_per_hr": profile["cost_per_hr"],
            "idle_w": profile["idle_w"],
            "max_w": profile["max_w"],
            "cpu_cores": profile["cpu_cores"],
            "total_mem_gb": profile["total_mem_gb"]
        }
    return {
        "total_nodes": len(nodes_base),
        "nodes": nodes_base
    }

def generate_telemetry_snapshot():
    """Generates real-time live telemetry snapshot."""
    live_nodes_info = get_live_k3s_node_info()
    
    cpu_query = '100 - (avg by (instance, node) (rate(node_cpu_seconds_total{mode="idle"}[6s])) * 100)'
    mem_avail_query = 'node_memory_MemAvailable_bytes'
    mem_total_query = 'node_memory_MemTotal_bytes'
    
    cpus = query_promql(cpu_query)
    mems_avail = query_promql(mem_avail_query)
    mems_total = query_promql(mem_total_query)
    
    real_sys_power = get_real_sysfs_power_w()
    local_proc_cpu = get_local_proc_stat_cpu_util()
    
    nodes_data = {}
    node_cpu_map = {}
    for node_name, profile in NODE_PROFILES.items():
        n_info = live_nodes_info.get(node_name, {})
        is_ready = n_info.get("ready", False)
        node_ip = n_info.get("ip", "")
        
        if not is_ready:
            continue
            
        display_name = profile["alias"]
        
        raw_cpu = cpus.get(node_ip, cpus.get(node_name, cpus.get(display_name, None)))
        if raw_cpu is not None:
            cpu_u = min(max(raw_cpu / 100.0, 0.0), 1.0)
        elif node_name == "willson" and local_proc_cpu is not None:
            cpu_u = local_proc_cpu
        else:
            cpu_u = 0.05
        
        node_cpu_map[display_name] = cpu_u
        
        avail_b = mems_avail.get(node_ip, mems_avail.get(node_name, 0))
        total_b = mems_total.get(node_ip, mems_total.get(node_name, profile["total_mem_gb"] * 1073741824))
        
        if total_b > 0 and avail_b > 0:
            mem_u = min(max(1.0 - (avail_b / total_b), 0.0), 1.0)
        else:
            mem_u = 0.56 if node_name == "willson" else 0.35
            
        if node_name == "willson" and real_sys_power is not None:
            power_w = real_sys_power
        else:
            power_w = round(profile["idle_w"] + (profile["max_w"] - profile["idle_w"]) * cpu_u, 1)

        nodes_data[display_name] = {
            "node_name": display_name,
            "raw_hostname": node_name,
            "ip": node_ip,
            "ready": is_ready,
            "cpu_util": round(cpu_u, 2),
            "mem_util": round(mem_u, 2),
            "power_w": power_w
        }
        
    live_pods = get_live_k3s_pods(node_cpu_map, live_nodes_info)
    
    return {
        "timestamp": int(time.time()),
        "total_active_nodes": len(nodes_data),
        "nodes": nodes_data,
        "workloads": live_pods
    }

class TelemetryAPIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/snapshot", "/api/v1/snapshot"]:
            snapshot = generate_telemetry_snapshot()
            self._send_json(snapshot)
        elif self.path in ["/all-nodes", "/api/v1/all-nodes"]:
            base_data = get_base_node_profiles()
            self._send_json(base_data)
        elif self.path in ["/pods", "/api/v1/pods"]:
            live_nodes_info = get_live_k3s_node_info()
            node_cpu_map = {}
            pods = get_live_k3s_pods(node_cpu_map, live_nodes_info)
            self._send_json({"pods": pods})
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Endpoint not found. Use /api/v1/snapshot or /api/v1/all-nodes")
            
    def _send_json(self, data):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, indent=2).encode('utf-8'))
        except (ConnectionResetError, BrokenPipeError):
            pass  # Suppress clean socket disconnections

    def log_message(self, format, *args):
        # Print HTTP GET request logs to terminal console
        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format % args))

def run_server(port=8080):
    server = HTTPServer(('0.0.0.0', port), TelemetryAPIHandler)
    print(f"🚀 Telemetry REST API Server listening at http://0.0.0.0:{port}/api/v1/snapshot")
    server.serve_forever()

if __name__ == "__main__":
    target_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(target_port)
