# 2026-07-11 - Ansible Plan: Standardize Node Networking and Firewall

**Date:** 2026-07-11  
**Status:** Draft execution plan  
**Scope:** control-plane, worker-1, gpu-node (may be offline)

This plan standardizes host networking/firewall behavior across all nodes using Ansible, while keeping rollout safe and learn-as-you-go.

---

## Goal

Create one consistent host baseline for Kubernetes nodes:

- one network management model across nodes
- one host firewall strategy (no conflicting restorers)
- reproducible service state after reboot
- Ansible as source of truth for host config

---

## Current Repository Context

Existing assets already in repo:

- Inventory: `ansible/inventory/hosts.yml`
- Main playbook: `ansible/playbooks/site.yml`
- Roles:
  - `ansible/roles/base`
  - `ansible/roles/k8s_node`
  - `ansible/roles/gpu`

This plan builds on those roles instead of creating a parallel provisioning path.

---

## Target Standard (Decision Baseline)

For all Kubernetes nodes in this homelab:

1. Use deterministic server networking (netplan + `systemd-networkd`) for primary NIC.
2. Keep host firewall management singular and explicit.
3. Disable conflicting firewall restorers (`ufw`, `nftables`, `netfilter-persistent`, `iptables-persistent`) unless intentionally reintroduced.
4. Preserve Kubernetes-required runtime networking (kube-proxy/CNI chains) by avoiding multi-manager rule conflicts.

Note: if you later want host firewall hardening, do it in one framework only and test from console access first.

---

## Decision Record

### DR-001: Node IP assignment strategy

**Decision**

- Control-plane uses static on-host network configuration.
- Worker and GPU nodes use DHCP with router reservations (UDM), resulting in stable fixed leases.

**Why**

- Control-plane is the highest criticality node; static on-host config avoids DHCP dependency at boot.
- Ubiquiti Dream Machine reservations are generally reliable and operationally simple for worker-class nodes.
- This split balances reliability and maintainability while standardization work is in progress.

**Implications**

- Control-plane netplan/networkd config is managed explicitly in Ansible.
- Worker/GPU inventory still uses fixed `ansible_host` values that match reserved leases.
- Router reservation configuration becomes part of infrastructure documentation/checklists.

**Revisit Criteria**

- Revisit after 30 days of stable operation, or sooner if DHCP lease anomalies appear.
- If any worker/GPU reservation instability is observed, migrate those nodes to static on-host config.

**Validation Commands (DR-001)**

Run from `ansible/`:

```bash
# Confirm control-plane route origin is static and worker route origin is dhcp
ansible all --limit control-plane,worker-1 -b -m command -a "ip route"

# Confirm interface/IP assignments on reachable nodes
ansible all --limit control-plane,worker-1 -b -m command -a "ip -br a"

# Confirm NetworkManager/networkd service state per node
ansible all --limit control-plane,worker-1 -b -m command -a "systemctl is-enabled systemd-networkd NetworkManager"
ansible all --limit control-plane,worker-1 -b -m command -a "systemctl is-active systemd-networkd NetworkManager"
```

Expected interpretation:

- Control-plane LAN default route should show `proto static`.
- Worker LAN default route should show `proto dhcp` with expected reserved IP.
- No unexpected interface drift (for example, Wi-Fi unintentionally up on worker).

---

## Phase Plan (3-5 Days)

## Day 1 - Discovery and Baseline Capture (No Risk Changes)

### Objectives

- Understand current per-node drift.
- Capture state before changing anything.

### Tasks

1. Run ad-hoc state collection from Ansible control host:
   ```bash
   cd ansible
   ansible all -m ping
   ansible all -b -m command -a "ip -br a"
   ansible all -b -m command -a "ip route"
   ansible all -b -m command -a "systemctl is-enabled systemd-networkd NetworkManager ufw nftables netfilter-persistent iptables-persistent"
   ansible all -b -m command -a "systemctl is-active systemd-networkd NetworkManager ufw nftables netfilter-persistent iptables-persistent"
   ```
2. Save output into a dated notes file in `docs/learnings` or local scratch.
3. Confirm per-node intended network settings:
   - interface name (likely `eno1`)
   - static/reserved IP
   - gateway
   - DNS servers

### Deliverable

- A simple table (node -> interface/IP/gateway/manager states).

| Node | Primary NIC | LAN IP | Default Gateway Route | Route Origin | Network Manager State | Firewall Manager State | Notes |
|---|---|---|---|---|---|---|---|
| `control-plane` | `eno1` | `192.168.1.246/24` | `default via 192.168.1.1 dev eno1` | `static` | `systemd-networkd` enabled/active; `NetworkManager` disabled/inactive | `ufw`, `nftables`, `netfilter-persistent`, `iptables-persistent` masked/inactive | CNI interfaces present (`flannel.1`, `cni0`, `veth*`). |
| `worker-1` | `enp0s31f6` | `192.168.1.116/24` | `default via 192.168.1.1 dev enp0s31f6` | `dhcp` | `NetworkManager` enabled/active; `systemd-networkd` disabled/inactive | `ufw` enabled/active; `nftables` disabled/inactive; `netfilter-persistent` and `iptables-persistent` not found | Wi-Fi interface `wlp2s0` is down intentionally; CNI interfaces present. |

---

## Day 2 - Add Ansible Variables and Role Scaffolding

### Objectives

- Add structure without applying disruptive changes yet.

### Tasks

1. Add group/host variables (recommended):
   - `ansible/group_vars/all.yml` for shared defaults
   - `ansible/host_vars/control-plane.yml`, `worker-1.yml`, `gpu-node.yml` for per-node addressing
2. Extend `base` role to manage network/firewall policy declaratively (idempotent tasks).
3. Add a netplan template file in role, for example:
   - `ansible/roles/base/templates/netplan-eno1.yaml.j2`
4. Add tasks in `ansible/roles/base/tasks/main.yml` to:
   - render netplan config
   - set secure perms (`0600`, root:root)
   - enable/start `systemd-networkd` and `systemd-resolved`
   - disable/mask conflicting firewall restorers
5. Keep risky tasks behind a variable gate first, for example:
   - `manage_network_stack: false` initially
   - `manage_firewall_stack: false` initially

Status: Completed. Gates are currently set in `ansible/group_vars/all.yml`, so network/firewall standardization tasks are no-op until intentionally enabled.

### Deliverable

- PR/commit-ready Ansible changes that are still mostly gated off.

---

## Day 3 - Dry Run and Worker-First Rollout

### Objectives

- Validate automation behavior on lowest-risk node before control-plane.

### Tasks

1. Dry run syntax and task preview:
   ```bash
   cd ansible
   ansible-playbook playbooks/site.yml --check --diff --limit worker-1
   ```
2. Apply to worker only:
   ```bash
   ansible-playbook playbooks/site.yml --limit worker-1
   ```
3. Validate worker:
   ```bash
   ansible worker-1 -b -m command -a "ip -br a"
   ansible worker-1 -b -m command -a "ip route"
   ansible worker-1 -b -m command -a "systemctl is-enabled systemd-networkd ufw nftables netfilter-persistent"
   ansible worker-1 -b -m command -a "systemctl is-active kubelet containerd"
   ```
4. Reboot worker and re-validate connectivity and node readiness.

### Rollback

- Keep console/physical access available.
- Re-enable previous manager only if required for emergency recovery.

---

## Worker Migration Runbook (Low-Risk, One Change at a Time)

Use this if you feel unsure about changing worker networking/firewall. The goal is to isolate risk and stop immediately if anything regresses.

### Preconditions

- Worker has known-good console access path (or physical access available).
- Control-plane is healthy before starting:
  ```bash
  kubectl get nodes -o wide
  ```
- Start from `ansible/` directory.

### Step 0 - Baseline snapshot (required)

```bash
ansible worker-1 -m ping
ansible worker-1 -b -m command -a "ip -br a"
ansible worker-1 -b -m command -a "ip route"
ansible worker-1 -b -m command -a "systemctl is-enabled systemd-networkd NetworkManager ufw nftables netfilter-persistent iptables-persistent"
ansible worker-1 -b -m command -a "systemctl is-active systemd-networkd NetworkManager ufw nftables netfilter-persistent iptables-persistent"
```

Save this output before any change.

### Step 1 - Apply only one stack at a time

Prefer this order:

1. Network manager migration (NM -> networkd), validate and reboot test.
2. Firewall manager standardization, validate and reboot test.

Do **not** combine network + firewall changes in the same first pass.

### Step 2 - Apply with strict limit

```bash
ansible-playbook playbooks/site.yml --limit worker-1 --check --diff
ansible-playbook playbooks/site.yml --limit worker-1
```

### Step 3 - Immediate validation gates

Run all of these before moving forward:

```bash
ansible worker-1 -b -m command -a "ip -br a"
ansible worker-1 -b -m command -a "ip route"
ansible worker-1 -b -m command -a "systemctl is-active kubelet containerd"
nc -vz 192.168.1.116 22
kubectl get nodes -o wide
kubectl get pods -A
```

### Step 4 - Reboot gate

```bash
ansible worker-1 -b -m command -a "reboot"
```

After node returns:

```bash
ansible worker-1 -m ping
nc -vz 192.168.1.116 22
ansible worker-1 -b -m command -a "ip route"
kubectl get nodes -o wide
```

### Stop Conditions (do not continue)

Stop immediately if any of the following occurs:

- SSH to worker fails or becomes intermittent.
- Default route/gateway is missing or wrong after apply.
- `kubelet`/`containerd` not active.
- Node remains `NotReady` beyond expected restart window.

### Rollback Guidance

- Use console access and restore previous network manager/service state first.
- Revert only the most recent change set (network or firewall), not unrelated roles.
- Re-run validation gates before attempting next migration step.

---

## Day 4 - Control-Plane Rollout (Conservative)

### Objectives

- Apply same baseline to control-plane with lockout safeguards.

### Tasks

1. Ensure local console access is ready before apply.
2. Run with limit and serial:
   ```bash
   cd ansible
   ansible-playbook playbooks/site.yml --limit control-plane --diff
   ```
3. Immediate validations:
   ```bash
   nc -vz 192.168.1.246 22
   ssh donovan@192.168.1.246
   kubectl get nodes -o wide
   ```
4. Reboot control-plane and repeat validations.

### Rollback

- If lockout risk appears, recover from console and revert only network/firewall stack tasks.

---

## Day 5 (Optional) - GPU Node Standardization

### Objectives

- Bring GPU node in line when it is online, without blocking other progress.

### Tasks

1. When online:
   ```bash
   cd ansible
   ansible -m ping gpu-node
   ansible-playbook playbooks/site.yml --limit gpu-node
   ```
2. Validate network/firewall/service baseline.
3. Reboot and verify persistence.

---

## Implementation Notes (Ansible Best Practices for This Work)

1. Use `serial: 1` for any play that can affect SSH/network.
2. Keep network/firewall tasks tagged:
   - `tags: [network]`
   - `tags: [firewall]`
3. Use handlers for restarts (`systemd-networkd`, etc.) only when templates change.
4. Use `--limit` aggressively during rollout.
5. Use `--check --diff` before each real apply.

---

## Suggested Variable Model

Example shared defaults (`group_vars/all.yml`):

```yaml
network_renderer: networkd
primary_nic: eno1
disable_host_firewall_managers: true
manage_network_stack: false
manage_firewall_stack: false
```

Example host-specific (`host_vars/control-plane.yml`):

```yaml
node_ipv4_address: 192.168.1.246/24
node_ipv4_gateway: 192.168.1.1
node_dns_servers:
  - 192.168.1.1
  - 1.1.1.1
```

Do the same for `worker-1` and `gpu-node`.

---

## Verification Checklist (After Each Phase)

- SSH reachable from admin host:
  - `nc -vz <node-ip> 22`
  - `ssh <user>@<node-ip>`
- Network stable:
  - `ip -br a`
  - `ip route`
  - gateway ping works
- Service states:
  - `systemctl is-active kubelet containerd`
  - `systemctl is-enabled systemd-networkd`
- Cluster health:
  - `kubectl get nodes`
  - `kubectl get pods -A`

---

## What "Done" Looks Like

- All nodes converge to same network/firewall baseline via Ansible.
- Rebooting any node does not break SSH access.
- No conflicting firewall restorer services are active unless explicitly intended.
- Control-plane and workers remain healthy in `kubectl`.
- Config is codified and repeatable for future node additions.
