#!/bin/bash

# ==============================================================================
# Edge-Core Orchestrator - End-to-End unified launcher
# ==============================================================================

# Ensure the script stops on errors
set -e

# Configuration
export SOURCE="k8s"
export TELEMETRY_URL="http://localhost:8080/api/v1/snapshot"
export NODES_URL="http://localhost:8080/api/v1/all-nodes"
export ROUTER_IP="10.243.176.77"
export ROUTER_PORT="8000"
export ROUTER_UPDATE_URL="http://${ROUTER_IP}:${ROUTER_PORT}/api/v1/router/update"

echo "======================================================="
echo "🚀 Starting MODW Edge-Core Orchestrator Pipeline..."
echo "======================================================="

# 1. Start Telemetry API Server in the background (Port 8080)
echo "[1/2] Spinning up Telemetry API Server (Port 8080)..."
python orchestration/telemetry_api_server.py &
TELEMETRY_PID=$!

# Wait for telemetry to boot
sleep 2

# 2. Start the Controller & Dashboard UI (Port 8081)
echo "[2/2] Spinning up Orchestrator Controller & Dashboard (Port 8081)..."
/home/mukes/dev/python/.venv/bin/uvicorn controller.app:app --host 0.0.0.0 --port 8081 &
DASHBOARD_PID=$!

echo "======================================================="
echo "✅ Pipeline is LIVE!"
echo "➡️ Dashboard URL: http://localhost:8081"
echo "➡️ Router Target: ${ROUTER_UPDATE_URL}"
echo "======================================================="
echo "Press CTRL+C to safely shut down the entire pipeline."

# Trap CTRL+C to cleanly kill both servers
trap "echo 'Shutting down pipeline...'; kill $TELEMETRY_PID $DASHBOARD_PID; exit" SIGINT SIGTERM

# Wait indefinitely
wait
