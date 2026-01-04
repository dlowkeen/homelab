# 2026-01-01 - Adding Worker Node

**Date:** 2026-01-01  
**Tags:** tag1, tag2, tag3


This document captures a detailed breakdown of a real-world failure and recovery scenario encountered while expanding a bare‑metal Kubernetes homelab. The goal is long‑term recall: *what broke, why it broke, and how it was fixed*.

---

## 1. Initial Change: Adding a Second Node

A new **OptiPlex Ubuntu worker node** was added to an existing **single‑node control plane** Kubernetes cluster.

* `kubeadm join` succeeded
* Node registered correctly
* Pods began scheduling across both nodes

At face value, the cluster appeared healthy.

---

## 2. Early Symptoms: Control Plane Instability

Shortly after adding the worker node, the **control plane node became unreliable**:

* SSH connections would hang or drop
* Remote access felt unstable or impossible
* Kubernetes components *appeared* to be running

However, once a monitor and keyboard were connected directly to the control plane:

* The system was **not rebooting**
* No kernel panics
* No actual crashes
* System was responsive locally

**Key insight:** the machine itself was stable — *network connectivity to it was not*.

---

## 3. Initial Hypothesis: Resource Exhaustion

The first theory was classic node overload:

* OOMKills
* CPU starvation
* Disk IO saturation

Reality:

* CPU and memory were sufficient
* Disk IO was **not being actively monitored**
* Adding a second node increased:

  * etcd writes
  * kube‑apiserver state churn
  * controller activity

This increased load **amplified an existing problem**, but was not the root cause.

---

## 4. Root Cause: Bare‑Metal Networking + Cilium

Further investigation revealed the true issue:

### Unstable networking on the control plane node

Factors involved:

* Running **Cilium on bare metal**
* Multiple network interfaces (ethernet + wifi)
* Dynamic routing
* iptables rules partially managed by Cilium (eBPF + fallback)

Effects:

* Conflicting routes
* Multiple default gateways
* iptables rules intercepting or breaking SSH
* Node reachable locally but unreliable over the network

> Cilium is extremely powerful, but **unforgiving** on bare‑metal systems with imperfect networking.

---

## 5. Attempted Stabilization with Cilium

Several remediation attempts were made:

* Inspecting iptables rules
* Restarting networking
* Reviewing interfaces and routes
* Restarting kubelet

These did not resolve the issue because:

* iptables state was already polluted
* Cilium had installed deep networking rules
* Debugging became nonlinear and fragile

At this point, continuing with Cilium had diminishing returns.

---

## 6. Strategic Decision: Replace Cilium with Flannel

To reduce complexity, the decision was made to:

* Remove Cilium entirely
* Install **Flannel** instead

This aligns well with:

* Small clusters
* Bare‑metal environments
* Stability‑first homelabs

---

## 7. New Issue: Cilium Remnants Broke Flannel

After installing Flannel:

* Pods were scheduled
* Pods received IPs
* Pods were stuck in `Pending` / `ContainerCreating`

### Root Cause

Cilium does **not clean up fully** when removed.

Leftovers included:

* iptables rules
* NAT chains
* CNI binaries

These remnants conflicted with Flannel.

---

## 8. Fix: Full Network Reset on Nodes

A full reset of networking state was required.

### Flush iptables

```bash
sudo iptables -F
sudo iptables -X
sudo iptables -t nat -F
sudo iptables -t mangle -F
sudo iptables -t raw -F
```

### Remove old CNI binaries

```bash
sudo rm -rf /opt/cni/bin/*
```

After this:

* Flannel was reinstalled cleanly
* Correct plugins were restored

---

## 9. Secondary Issue: Missing Flannel Bridge Plugin

After cleanup:

* Pods scheduled
* Containers failed to start
* No pod networking

### Root Cause

The **Flannel CNI bridge plugin** was missing on the control plane node.

Without it:

* Pods could not attach to `cni0`
* No L2 bridge
* No network namespace connectivity

Once installed correctly:

* Containers started
* Pod networking worked

---

## 10. Final Networking Issue: Multiple Default Routes (WiFi)

On the worker node:

```bash
ip route | grep default
```

Two default routes were present:

* Ethernet (lower metric)
* WiFi (higher metric)

Even with metrics, this caused:

* Flaky overlay routing
* Unpredictable VXLAN behavior

### Fix

WiFi was disabled entirely:

```bash
sudo ip link set wlp2s0 down
```

Result:

* Single default route
* Deterministic traffic
* Stable Flannel overlay

---

## 11. Final State

After all fixes:

* SSH stable
* Control plane reliable
* Pods schedule and start
* Pod‑to‑pod networking works
* Cross‑node traffic works
* No intermittent failures

The cluster now runs with:

* One interface per node
* Clean iptables
* Flannel CNI
* Predictable routing

---

## Key Takeaways

1. **Bare‑metal networking matters more than Kubernetes config**

   * Kubernetes will appear broken if node networking is unstable

2. **Cilium is powerful but unforgiving**

   * Great for cloud
   * Risky for small bare‑metal labs

3. **Always suspect**:

   * Multiple default routes
   * WiFi + Ethernet
   * Leftover CNI state

4. **When switching CNIs**:

   > Always flush iptables and remove old CNI binaries

---

## Disk I/O Saturation: Optiplex (SATA SSD) vs Production Hardware (NVMe + PCIe)

In my homelab, the control plane node runs on a Dell Optiplex with a SATA SSD, which is fundamentally limited in both throughput and IOPS compared to production-grade storage. SATA SSDs are capped at ~550 MB/s and handle far fewer concurrent I/O operations, especially under mixed small, random writes, which is exactly the workload Kubernetes control plane components generate.

When I added a second node and ran Cilium, disk I/O pressure spiked significantly. Node joins are some of the most disk-intensive events in Kubernetes: etcd must persist new cluster state, watches fire across the control plane, Cilium programs BPF state and iptables equivalents, and kubelet activity increases sharply. On a SATA SSD, this burst of sync-heavy, metadata-heavy I/O can saturate the disk, causing system-level side effects like stalled SSH sessions, delayed scheduling, and the appearance of instability—even when CPU and memory are not constrained.

On production-grade hardware, Cilium is normally fine because the storage stack is very different. Servers typically use NVMe SSDs connected directly over PCIe lanes, delivering orders of magnitude higher IOPS and much lower latency. NVMe excels at parallel, low-latency I/O, which allows etcd, kube-apiserver, and Cilium to absorb bursts of state changes without backpressure. In many setups, etcd even runs on dedicated NVMe volumes, isolating control plane write amplification from the rest of the system.

The issue wasn’t that Cilium is “too heavy” — it’s that the SATA-based Optiplex became the bottleneck during high-churn control plane events. Switching to a simpler CNI (Flannel) reduced disk write pressure and brought the system back within the capabilities of the hardware.

If you want, next I can:

Rewrite this in an even shorter TL;DR version

Add a “lessons learned” section

Or map this directly to what cloud providers do differently under the hood