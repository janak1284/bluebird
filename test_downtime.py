#!/usr/bin/env python3
import time
import os
import threading
import urllib.request
import urllib.error
import socket

ROUTER_IP = os.environ.get("ROUTER_IP", "10.243.176.77")
ROUTER_PORT = os.environ.get("ROUTER_PORT", "8000")

ENDPOINTS = {
    "checkout": f"http://{ROUTER_IP}:{ROUTER_PORT}/checkout",
    "profile":  f"http://{ROUTER_IP}:{ROUTER_PORT}/user-profile",
    "analytics": f"http://{ROUTER_IP}:{ROUTER_PORT}/analytics"
}

stats = {
    ep: {"200_OK": 0, "502_Gateway_Error": 0, "Timeouts": 0, "Other_Errors": 0} for ep in ENDPOINTS
}

def query_worker(name, url):
    """Continuously queries the endpoint and tallies the HTTP status code."""
    while True:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=2.0) as res:
                if res.status == 200:
                    stats[name]["200_OK"] += 1
                else:
                    stats[name]["Other_Errors"] += 1
        except urllib.error.HTTPError as e:
            if e.code == 502:
                stats[name]["502_Gateway_Error"] += 1
            else:
                stats[name]["Other_Errors"] += 1
        except urllib.error.URLError as e:
            if isinstance(e.reason, socket.timeout) or "timeout" in str(e.reason).lower() or "timed out" in str(e.reason).lower():
                stats[name]["Timeouts"] += 1
            else:
                stats[name]["Other_Errors"] += 1
        except Exception:
            stats[name]["Other_Errors"] += 1
        
        # Fire roughly 1 request per second per workload to prevent overloading the proxy
        time.sleep(1.0)

def display_stats():
    """Prints a clean updating table of the results."""
    while True:
        os.system('clear' if os.name == 'posix' else 'cls')
        print(f"🚀 Zero-Downtime Migration Tester (Hitting Router: {ROUTER_IP}:{ROUTER_PORT})")
        print("   If Make-Before-Break is working, only 200 OK should climb!")
        print("-" * 75)
        print(f"{'Workload Route':<18} | {'200 OK ✅':<12} | {'502 Bad Gateway ❌':<18} | {'Timeouts ⚠️':<12} | {'Errors 💀':<10}")
        print("-" * 75)
        for ep in ENDPOINTS:
            s = stats[ep]
            # Highlight errors in red if they happen
            err_502 = f"\033[91m{s['502_Gateway_Error']}\033[0m" if s['502_Gateway_Error'] > 0 else s['502_Gateway_Error']
            err_time = f"\033[93m{s['Timeouts']}\033[0m" if s['Timeouts'] > 0 else s['Timeouts']
            err_oth = f"\033[91m{s['Other_Errors']}\033[0m" if s['Other_Errors'] > 0 else s['Other_Errors']
            
            print(f"{ep:<18} | {s['200_OK']:<12} | {err_502:<27} | {err_time:<21} | {err_oth:<10}")
        print("-" * 75)
        print("\nPress CTRL+C to stop.")
        time.sleep(1)

if __name__ == "__main__":
    print("Starting query workers...")
    for name, url in ENDPOINTS.items():
        t = threading.Thread(target=query_worker, args=(name, url), daemon=True)
        t.start()

    try:
        display_stats()
    except KeyboardInterrupt:
        print("\nTest stopped by user.")
