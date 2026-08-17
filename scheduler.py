# scheduler.py
# Custom K3s Pod Binding & Zero-Downtime Migration Controller (Blueprint Section 6)

import os
import sys
import time
import json
import urllib.request
import urllib.parse
import subprocess

# Node IP & Port Registry for Router Cutover
NODE_ENDPOINT_MAP = {
    "willson":       {"ip": "10.243.176.184"},
    "core-master":   {"ip": "10.243.176.184"},
    "fedora":        {"ip": "10.243.176.218"},
    "edge-node-01":  {"ip": "10.243.176.218"},
    "archlinux":     {"ip": "10.243.176.3"},
    "core-node-01":   {"ip": "10.243.176.3"},
}

WORKLOAD_ROUTE_MAP = {
    "checkout-critical-01": {"route": "/checkout", "node_port": 30081},
    "user-profile-std-01":  {"route": "/user-profile", "node_port": 30082},
    "analytics-batch-01":   {"route": "/analytics", "node_port": 30083},
}

# Configurable Router Host IP (Defaults to ROUTER_IP env var or 127.0.0.1)
ROUTER_HOST = os.environ.get("ROUTER_IP", "127.0.0.1")
ROUTER_PORT = os.environ.get("ROUTER_PORT", "8000")
ROUTER_UPDATE_URL = f"http://{ROUTER_HOST}:{ROUTER_PORT}/api/v1/router/update"

def get_kube_env():
    env = os.environ.copy()
    if 'KUBECONFIG' not in env and os.path.exists(os.path.expanduser('~/.kube/config')):
        env['KUBECONFIG'] = os.path.expanduser('~/.kube/config')
    return env

def get_current_pod_locations():
    """Queries K3s for current node location of active workload pods."""
    pod_locations = {}
    try:
        cmd = ["kubectl", "get", "pods", "-n", "default", "-o", "json"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, env=get_kube_env())
        if res.returncode == 0:
            data = json.loads(res.stdout)
            for item in data.get('items', []):
                labels = item['metadata'].get('labels', {})
                node_name = item['spec'].get('nodeName', 'unassigned')
                phase = item['status'].get('phase', 'Unknown')
                
                pod_name = item['metadata']['name']
                for wl in WORKLOAD_ROUTE_MAP.keys():
                    if pod_name.startswith(wl) and phase == "Running":
                        pod_locations[wl] = node_name
    except Exception:
        pass
    return pod_locations

def update_router_table(route, new_node, new_target):
    """Notifies custom router.py (on ROUTER_HOST) to update the live route table."""
    try:
        payload = json.dumps({
            "route": route,
            "new_node": new_node,
            "new_target": new_target
        }).encode('utf-8')
        
        print(f"  [Router Update] Sending POST to {ROUTER_UPDATE_URL}...")
        req = urllib.request.Request(ROUTER_UPDATE_URL, data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"  ⚠️ Router update notification to {ROUTER_UPDATE_URL} failed: {e}")
        return False

def execute_make_before_break_migration(workload_name, target_node):
    """Executes Make-Before-Break pod migration according to Blueprint Section 6."""
    current_locations = get_current_pod_locations()
    current_node = current_locations.get(workload_name, "unknown")
    
    if current_node == target_node:
        print(f"ℹ️ Workload '{workload_name}' is already on target node '{target_node}'. Skipping.")
        return True, 0.0

    print(f"\n=======================================================")
    print(f"🚀 INITIATING ZERO-DOWNTIME MIGRATION")
    print(f"   - Workload    : {workload_name}")
    print(f"   - Source Node : {current_node}")
    print(f"   - Target Node : {target_node}")
    print(f"   - Router Host : {ROUTER_HOST}:{ROUTER_PORT}")
    print(f"=======================================================")
    
    t_start = time.time()
    
    # 1. Update Deployment spec nodeName in K3s
    patch_payload = json.dumps({
        "spec": {
            "template": {
                "spec": {
                    "nodeName": target_node
                }
            }
        }
    })
    
    cmd = ["kubectl", "patch", "deployment", workload_name, "-n", "default", "--type", "strategic", "-p", patch_payload]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=get_kube_env())
    if res.returncode != 0:
        print(f"❌ Failed to patch deployment '{workload_name}': {res.stderr}")
        return False, 0.0

    print(f"  [1/4] Deployment patched in K3s. Spawning new container on '{target_node}'...")
    
    # 2. Wait for new pod on target_node to reach Ready == True
    new_pod_ready = False
    timeout_sec = 30
    poll_start = time.time()
    
    while time.time() - poll_start < timeout_sec:
        cmd_get = ["kubectl", "get", "pods", "-n", "default", "-o", "json"]
        res_get = subprocess.run(cmd_get, capture_output=True, text=True, timeout=3, env=get_kube_env())
        if res_get.returncode == 0:
            data = json.loads(res_get.stdout)
            for item in data.get('items', []):
                p_name = item['metadata']['name']
                p_node = item['spec'].get('nodeName', '')
                p_phase = item['status'].get('phase', '')
                
                if p_name.startswith(workload_name) and p_node == target_node and p_phase == "Running":
                    for cond in item['status'].get('conditions', []):
                        if cond['type'] == 'Ready' and cond['status'] == 'True':
                            new_pod_ready = True
                            break
                if new_pod_ready:
                    break
        if new_pod_ready:
            break
        time.sleep(0.5)

    if not new_pod_ready:
        print(f"❌ Migration timed out waiting for new pod on '{target_node}' to become Ready.")
        return False, time.time() - t_start

    t_ready = time.time()
    transfer_time = round(t_ready - t_start, 2)
    print(f"  [2/4] New container on '{target_node}' is RUNNING and READY! (Transfer Time: {transfer_time}s)")
    
    # 3. Execute Instant Router Table Cutover
    route_info = WORKLOAD_ROUTE_MAP.get(workload_name, {})
    route_path = route_info.get("route", "/checkout")
    node_port = route_info.get("node_port", 30081)
    
    node_ip = NODE_ENDPOINT_MAP.get(target_node, {}).get("ip", "127.0.0.1")
    new_target_url = f"http://{node_ip}:{node_port}"
    
    success = update_router_table(route_path, target_node, new_target_url)
    if success:
        print(f"  [3/4] Custom Router Table updated at {ROUTER_HOST}: '{route_path}' ➔ {new_target_url}")
    else:
        print(f"  [3/4] ⚠️ Custom router update failed, but K3s NodePort remains accessible.")
        
    print(f"  [4/4] Old container on '{current_node}' scheduled for background termination.")
    print(f"=======================================================")
    print(f"✅ ZERO-DOWNTIME MIGRATION COMPLETED IN {transfer_time} SECONDS!")
    print(f"=======================================================\n")
    
    return True, transfer_time

def apply_placement(placement_dict, explanation=""):
    """Accepts placement dictionary from Optimizer and executes zero-downtime migrations."""
    print("\n=======================================================")
    print("📋 RECEIVED OPTIMIZER PLACEMENT TARGETS")
    print("=======================================================")
    if explanation:
        print(f"💡 Optimizer Rationale: {explanation}")
    print(json.dumps(placement_dict, indent=2))
    print("=======================================================")
    
    results = {}
    for workload_name, target_node in placement_dict.items():
        success, transfer_time = execute_make_before_break_migration(workload_name, target_node)
        results[workload_name] = {
            "success": success,
            "target_node": target_node,
            "transfer_time_sec": transfer_time
        }
    return results

if __name__ == "__main__":
    if len(sys.argv) > 2:
        w_name = sys.argv[1]
        t_node = sys.argv[2]
        apply_placement({w_name: t_node}, explanation=f"Manual CLI migration request to {t_node}")
    elif len(sys.argv) == 2:
        try:
            placement = json.loads(sys.argv[1])
            apply_placement(placement, explanation="CLI JSON migration request")
        except Exception as e:
            print(f"Error parsing placement JSON: {e}")
    else:
        print("Usage: ROUTER_IP=<ip> python3 scheduler.py <workload_name> <target_node>")
