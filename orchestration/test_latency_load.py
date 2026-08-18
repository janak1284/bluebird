#!/usr/bin/env python3
# test_latency_load.py
# Real-Time Latency Tester & Load Generator for PS4 Cluster

import time
import json
import urllib.request
import argparse
import subprocess

def test_live_telemetry_latency(api_url):
    print(f"📡 Testing real-time HTTP Latency against {api_url}...\n")
    latencies = []
    
    for i in range(10):
        t_start = time.perf_counter()
        try:
            req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                t_end = time.perf_counter()
                
                rtt_ms = (t_end - t_start) * 1000.0
                latencies.append(rtt_ms)
                
                # Extract p99_ms and node power from snapshot
                nodes = data.get('nodes', {})
                workloads = data.get('workloads', {})
                
                fedora_p99 = workloads.get('checkout-critical-01', {}).get('p99_ms', 0)
                fedora_sla = workloads.get('checkout-critical-01', {}).get('sla_ms', 20)
                fedora_cpu = nodes.get('edge-node-01', {}).get('cpu_util', 0.0)
                
                print(f"Request #{i+1:02d} | Real HTTP RTT: {rtt_ms:6.2f} ms | Fedora CPU: {fedora_cpu*100:4.1f}% | Checkout p99: {fedora_p99:5.1f} ms (SLA: {fedora_sla} ms)")
        except Exception as e:
            print(f"Request #{i+1:02d} | Error: {e}")
            
        time.sleep(0.5)

    if latencies:
        avg_lat = sum(latencies) / len(latencies)
        p99_measured = sorted(latencies)[int(len(latencies) * 0.95)]
        print("\n" + "="*60)
        print(f"📊 SUMMARY OF REAL-LIFE HTTP LATENCY TEST:")
        print(f"   • Average Real HTTP RTT : {avg_lat:.2f} ms")
        print(f"   • 95th Percentile RTT   : {p99_measured:.2f} ms")
        print("="*60 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PS4 Cluster Latency Tester")
    parser.add_argument("--url", default="http://localhost:8080/api/v1/snapshot", help="Telemetry API Endpoint")
    args = parser.parse_args()
    
    test_live_telemetry_latency(args.url)
