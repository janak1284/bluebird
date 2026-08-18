#!/usr/bin/env python3
import time
import random
import subprocess

WORKLOADS = ["checkout-critical-01", "user-profile-std-01", "analytics-batch-01"]
NODES = ["core-master", "edge-node-01", "edge-node-02", "edge-node-03"]

print("🐒 Chaos Monkey Started! Randomly throwing workloads around every 15 seconds...")
print("   (Press CTRL+C to stop)")

while True:
    try:
        wl = random.choice(WORKLOADS)
        target = random.choice(NODES)
        
        print(f"\n[CHAOS MONKEY] Decided to toss '{wl}' onto '{target}'!")
        
        # Call the scheduler script directly with the correct ROUTER_IP
        import os
        env = os.environ.copy()
        env["ROUTER_IP"] = "10.243.176.77"
        subprocess.run(["python3", "orchestration/scheduler.py", wl, target], env=env)
        
        # Sleep for 15 seconds before the next chaos event
        time.sleep(15)
        
    except KeyboardInterrupt:
        print("\n🐒 Chaos Monkey captured and stopped.")
        break
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(5)
