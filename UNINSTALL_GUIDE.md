# Uninstallation Guide for PS4 Edge-Cloud Orchestrator Stack

This guide details the exact steps to completely uninstall and clean up all software components deployed during the setup and testing of the K3s Edge-Core Cloud cluster.

---

## 1. Uninstall Test Workloads & Deployments

To remove any dummy pods, deployments, or services created during testing:

```bash
# Delete test deployments
sudo k3s kubectl delete deployment web-dummy --ignore-not-found
sudo k3s kubectl delete deployment cpu-workload --ignore-not-found

# Delete test pods
sudo k3s kubectl delete pod arch-cpu-test --ignore-not-found
sudo k3s kubectl delete pod fedora-web-test --ignore-not-found
sudo k3s kubectl delete pod edge-specific-workload --ignore-not-found
```

---

## 2. Uninstall Prometheus & Grafana Stack (`kube-prometheus-stack`)

To completely delete Prometheus, Grafana, Node Exporters, and the `monitoring` namespace from the cluster:

```bash
# 1. Uninstall Prometheus Helm release
helm uninstall prometheus --namespace monitoring

# 2. Delete the monitoring namespace
sudo k3s kubectl delete namespace monitoring

# 3. Delete Prometheus Custom Resource Definitions (CRDs) left behind by Helm
sudo k3s kubectl delete crd alertmanagerconfigs.monitoring.coreos.com \
  alertmanagers.monitoring.coreos.com \
  podmonitors.monitoring.coreos.com \
  probes.monitoring.coreos.com \
  prometheusagents.monitoring.coreos.com \
  prometheuses.monitoring.coreos.com \
  prometheusrules.monitoring.coreos.com \
  scrapeconfigs.monitoring.coreos.com \
  servicemonitors.monitoring.coreos.com \
  thanosrulers.monitoring.coreos.com
```

---

## 3. Uninstall Helm (Package Manager)

If Helm was installed using the get-helm script:

```bash
sudo rm -f /usr/local/bin/helm
```

---

## 4. Uninstall K3s Worker / Agent Nodes (`archlinux`, `fedora`)

Run this command directly on each **Worker Node machine** to remove the K3s agent, network bridges, and container runtime:

```bash
sudo /usr/local/bin/k3s-agent-uninstall.sh
```

---

## 5. Uninstall K3s Master / Server (`willson`)

Run this command directly on the **Master machine** to remove the K3s control plane, cluster state, and configuration files:

```bash
sudo /usr/local/bin/k3s-uninstall.sh
```

---

## 6. Optional: Clean Firewall Rules (`firewalld`)

If temporary firewall rules were added for port 6443:

```bash
# Remove temporary session rule
sudo firewall-cmd --remove-port=6443/tcp

# Reload firewall to revert to default state
sudo firewall-cmd --reload
```
