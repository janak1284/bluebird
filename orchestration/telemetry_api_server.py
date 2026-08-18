# telemetry_api_server.py
import os
import sys
import time
import glob
import json
import threading
import urllib.request
import urllib.parse
import subprocess
import random
from http.server import HTTPServer, BaseHTTPRequestHandler



# Node Profiles Configuration (PS-S04 Blueprint Section 4)
NODE_PROFILES = {
    "willson":       {"alias": "core-master",  "tier": "core", "zone": "core-1", "base_latency_ms": 40, "cost_per_hr": 2.0, "idle_w": 7.5, "max_w": 30,  "cpu_cores": 16, "total_mem_gb": 16},
    "archlinux":    {"alias": "edge-node-03", "tier": "edge", "zone": "edge-3", "base_latency_ms": 5, "cost_per_hr": 12.0, "idle_w": 25.0, "max_w": 135, "cpu_cores": 12, "total_mem_gb": 8},
    "fedora":       {"alias": "edge-node-01", "tier": "edge", "zone": "edge-1", "base_latency_ms": 4,  "cost_per_hr": 9.0, "idle_w": 12.0, "max_w": 45,  "cpu_cores": 8,  "total_mem_gb": 8},
    "desktop-prnd0ve": {"alias": "edge-node-02", "tier": "edge", "zone": "edge-2", "base_latency_ms": 3,  "cost_per_hr": 6.0, "idle_w": 8.0,  "max_w": 35,  "cpu_cores": 8,  "total_mem_gb": 16},
}

PROMETHEUS_URL = "http://localhost:9090/api/v1/query"

_last_cpu_sample = {"idle": 0.0, "total": 0.0}
_pf_process = None

# Global In-Memory Pre-Rendered Cache
_cached_snapshot_bytes = b"{}"
_cached_all_nodes_bytes = b"{}"
_cached_pods_bytes = b"{}"

def get_kube_env():
    env = os.environ.copy()
    if 'KUBECONFIG' not in env and os.path.exists(os.path.expanduser('~/.kube/config')):
        env['KUBECONFIG'] = os.path.expanduser('~/.kube/config')
    return env

def ensure_prometheus_connection():
    """Self-healing manager: automatically starts & maintains Prometheus port-forward on 9090."""
    global _pf_process
    try:
        req = urllib.request.Request(f"{PROMETHEUS_URL}?{urllib.parse.urlencode({'query': 'up'})}")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    # Port 9090 down or unresponsive: auto-spawn background port-forward
    if _pf_process is None or _pf_process.poll() is not None:
        try:
            cmd = ["kubectl", "port-forward", "-n", "monitoring", "svc/prometheus-operated", "9090:9090"]
            _pf_process = subprocess.Popen(cmd, env=get_kube_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        except Exception:
            pass
    return False

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
                return min(max(util, 0.01), 1.0)
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

def fetch_k3s_node_info():
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
    except Exception:
        pass
    return nodes

def fetch_k3s_pods(node_cpu_map, live_nodes_info):
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
                
                if item['metadata'].get('deletionTimestamp'):
                    phase = "Terminating"
                
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
                    cpu_u = node_cpu_map.get(display_node, 0.01)
                    if phase == "Running":
                        rps = 250
                    else:
                        rps = 0
                    queuing_delay = (rps / 100.0) * (1.0 / max(1.0 - cpu_u, 0.05))
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
                    "net_rx_bps": int(rps * 1250 * random.uniform(0.9, 1.1)) if phase == "Running" else 0,
                    "net_tx_bps": int(rps * 45000 * random.uniform(0.9, 1.1)) if phase == "Running" else 0,
                    "last_moved": int(time.time()) - 120
                }
    except Exception:
        pass
    return pods

def query_promql(query):
    ensure_prometheus_connection()
    try:
        url = f"{PROMETHEUS_URL}?{urllib.parse.urlencode({'query': query})}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
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

def get_base_node_profiles():
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

def background_telemetry_collector():
    """Asynchronous background worker with self-healing Prometheus port-forward connection."""
    global _cached_snapshot_bytes, _cached_all_nodes_bytes, _cached_pods_bytes
    
    base_data = get_base_node_profiles()
    _cached_all_nodes_bytes = json.dumps(base_data, indent=2).encode('utf-8')

    while True:
        try:
            live_nodes_info = fetch_k3s_node_info()
            
            cpu_query = '100 - (avg by (instance, node) (rate(node_cpu_seconds_total{mode="idle"}[6s])) * 100)'
            mem_avail_query = 'node_memory_MemAvailable_bytes'
            mem_total_query = 'node_memory_MemTotal_bytes'
            net_rx_query = 'sum by (instance, node) (rate(node_network_receive_bytes_total{device!~"veth.*|lo|flannel.*|cni.*|docker.*|br-.*"}[6s]))'
            net_tx_query = 'sum by (instance, node) (rate(node_network_transmit_bytes_total{device!~"veth.*|lo|flannel.*|cni.*|docker.*|br-.*"}[6s]))'
            
            cpus = query_promql(cpu_query)
            mems_avail = query_promql(mem_avail_query)
            mems_total = query_promql(mem_total_query)
            net_rxs = query_promql(net_rx_query)
            net_txs = query_promql(net_tx_query)
            
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
                    cpu_u = min(max(raw_cpu / 100.0, 0.01), 1.0)
                elif node_name == "willson" and local_proc_cpu is not None:
                    cpu_u = min(max(local_proc_cpu, 0.01), 1.0)
                else:
                    cpu_u = 0.01
                
                node_cpu_map[display_name] = cpu_u
                
                avail_b = mems_avail.get(node_ip, mems_avail.get(node_name, 0))
                total_b = mems_total.get(node_ip, mems_total.get(node_name, profile["total_mem_gb"] * 1073741824))
                
                if total_b > 0 and avail_b > 0:
                    mem_u = min(max(1.0 - (avail_b / total_b), 0.01), 1.0)
                else:
                    mem_u = 0.56 if node_name == "willson" else 0.35
                    
                if node_name == "willson" and real_sys_power is not None:
                    power_w = real_sys_power
                else:
                    power_w = round(profile["idle_w"] + (profile["max_w"] - profile["idle_w"]) * cpu_u, 1)

                raw_rx = net_rxs.get(node_ip, net_rxs.get(node_name, net_rxs.get(display_name, None)))
                raw_tx = net_txs.get(node_ip, net_txs.get(node_name, net_txs.get(display_name, None)))

                nodes_data[display_name] = {
                    "node_name": display_name,
                    "raw_hostname": node_name,
                    "ip": node_ip,
                    "ready": is_ready,
                    "cpu_util": round(cpu_u, 2),
                    "mem_util": round(mem_u, 2),
                    "power_w": power_w,
                    "prom_net_rx": raw_rx,
                    "prom_net_tx": raw_tx
                }
                
            live_pods = fetch_k3s_pods(node_cpu_map, live_nodes_info)
            
            for n_name, n_data in nodes_data.items():
                if not n_data["ready"]:
                    n_data["net_rx_bps"] = 0
                    n_data["net_tx_bps"] = 0
                    continue
                
                # If Prometheus actually gave us real hardware data, use it!
                if n_data.get("prom_net_rx") is not None and n_data.get("prom_net_tx") is not None:
                    n_data["net_rx_bps"] = int(n_data["prom_net_rx"])
                    n_data["net_tx_bps"] = int(n_data["prom_net_tx"])
                else:
                    # Fallback to mathematically simulating it from the pods
                    n_rx = 5200 # 5.2 KB/s background noise
                    n_tx = 8400
                    for p in live_pods.values():
                        if p["node"] == n_name:
                            n_rx += p["net_rx_bps"]
                            n_tx += p["net_tx_bps"]
                    
                    n_data["net_rx_bps"] = int(n_rx * random.uniform(0.95, 1.05))
                    n_data["net_tx_bps"] = int(n_tx * random.uniform(0.95, 1.05))
                
                # Clean up temp keys so they don't leak into the API response
                n_data.pop("prom_net_rx", None)
                n_data.pop("prom_net_tx", None)
            
            snapshot = {
                "timestamp": int(time.time()),
                "total_active_nodes": len(nodes_data),
                "nodes": nodes_data,
                "workloads": live_pods
            }
            
            _cached_snapshot_bytes = json.dumps(snapshot, indent=2).encode('utf-8')
            _cached_pods_bytes = json.dumps({"pods": live_pods}, indent=2).encode('utf-8')
        except Exception:
            pass
            
        time.sleep(0.8)

class TelemetryAPIHandler(BaseHTTPRequestHandler):


    def do_GET(self):
        if self.path in ["/", "/snapshot", "/api/v1/snapshot"]:
            self._send_bytes(_cached_snapshot_bytes)
        elif self.path in ["/all-nodes", "/api/v1/all-nodes"]:
            self._send_bytes(_cached_all_nodes_bytes)
        elif self.path in ["/pods", "/api/v1/pods"]:
            self._send_bytes(_cached_pods_bytes)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Endpoint not found. Use /api/v1/snapshot or /api/v1/all-nodes")
            
    def _send_json(self, data):
        self._send_bytes(json.dumps(data, indent=2).encode('utf-8'))

    def _send_bytes(self, payload_bytes):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload_bytes)))
            self.end_headers()
            self.wfile.write(payload_bytes)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format % args))

def run_server(port=8080):
    t = threading.Thread(target=background_telemetry_collector, daemon=True)
    t.start()
    
    server = HTTPServer(('0.0.0.0', port), TelemetryAPIHandler)
    print(f"🚀 Telemetry REST API Server listening at http://0.0.0.0:{port}/api/v1/snapshot (Self-Healing Mode)")
    server.serve_forever()

if __name__ == "__main__":
    target_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    run_server(target_port)
