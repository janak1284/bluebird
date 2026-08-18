# MODW: Edge-Core Orchestrator

The Multi-Objective Dynamic Workload (MODW) Orchestrator is an intelligent placement engine designed to dynamically schedule workloads across Edge and Core Kubernetes clusters. It utilizes an exhaustive Pareto frontier solver to balance latency SLAs, hourly cost, cluster power consumption, and migration friction in real-time.

---

## 🏗️ Architecture Overview

The project is split into several interconnected components:
- **`optimizer.py`**: The core mathematical brain. It takes a telemetry snapshot and evaluates all possible permutations of workload placements, generating a non-dominated Pareto front of solutions based on strict physical constraints (CPU/Memory).
- **`controller/loop.py`**: The background controller loop. It bridges the gap between Kubernetes telemetry and the optimizer. It polls live metrics, feeds them to the optimizer, evaluates the configured policy, and executes pod migrations.
- **`dashboard.py` & `static/index.html`**: A lightweight FastAPI web server and a vanilla JS/Tailwind CSS frontend that visualizes the "Cockpit" telemetry, Pareto decisions, and latency streams in real-time.
- **`fake_state.py`**: A fully functional local simulation/chaos engine used for developing and testing the orchestrator without needing an active Kubernetes cluster.

---

## 🚀 Running the Orchestrator

There are two primary ways to run the project depending on whether you are developing UI features locally or connecting to the live Kubernetes cluster.

### Mode 1: Local Simulation & Chaos Engineering (Dev Mode)
Use this mode if you want to test the orchestrator logic, UI, and policy engine locally with synthetic workloads and simulated traffic.

```bash
# Start the dashboard with the fake telemetry generator injected
DASHBOARD_FAKE=1 uvicorn dashboard:app --host 0.0.0.0 --port 8080
```
**Features in this mode:**
- The background controller runs using `fake_state.py`.
- You can use the "Chaos & Simulator" features in the UI to intentionally inject traffic spikes (e.g., 750 RPS on Checkout) or failover hardware nodes to watch the orchestrator dynamically re-balance workloads in real-time.

### Mode 2: Production / Kubernetes Telemetry Mode
Use this mode when you are running on the actual cluster. The orchestrator will attempt to fetch live pod/node telemetry from the designated telemetry aggregator (`10.243.176.184`).

```bash
# Start the unified controller and dashboard app
uvicorn controller.app:app --host 0.0.0.0 --port 8080
```
**Features in this mode:**
- `controller/app.py` spins up the background `loop.py` thread which curls the real `/api/v1/snapshot` k8s endpoint.
- Hardware node names (`raw_hostname`) and real live RPS metrics are piped directly to the Dashboard Cockpit.
- *Note: Chaos toggles in the UI will be disabled as the system operates in read/observe mode over the real environment.*

---

## 🎛️ The Dashboard Cockpit

Once the application is running, navigate to `http://localhost:8080` to access the command center.

1. **Active Policy Dropdown**: Switch the optimizer's target policy on-the-fly (e.g., from *SLA-First* to *Cost-First* or *Green/Eco-Friendly*). The orchestrator will immediately evaluate the new constraints and trigger migrations on the next cycle.
2. **Cluster Topology**: View all discovered nodes (Edge vs. Core), their live hardware utilizations, and exactly which workloads are pinned to them.
3. **Live Decision Stream**: An audited terminal window that streams the orchestrator's step-by-step reasoning behind every workload migration, including expected deltas in Cost and Power.
4. **Mini p99 Stream**: A real-time charting of latency (modeled via M/M/1 queue physics) to ensure Edge-bound workloads are not breaching their SLAs.

---

## 🛠️ Development

- **Theme Modification**: The frontend uses a custom "Metallic Chic" color palette implemented via Tailwind config directly inside `static/index.html`. Modifying `extend.colors` within the `<script>` tag will globally alter the UI theme.
- **Adding Objectives**: If you wish to add a new optimization metric (e.g., Carbon Intensity), you must update the `evaluate()` multi-objective scoring tuple in `optimizer.py`.