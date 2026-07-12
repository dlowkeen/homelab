# 2026-07-11 - Control Plane SSH Outage (Firewall/Netfilter Conflict)

**Date:** 2026-07-11  
**Tags:** kubernetes, kubeadm, ssh, firewall, nftables, ufw, netfilter, networking

This document captures a full incident summary for a control-plane outage where SSH and `kubectl` access failed intermittently while some application traffic still appeared healthy.

---

## TL;DR

- SSH and `kubectl` failures were caused by host-level netfilter rules, not by a dead `sshd` daemon.
- `sshd` was listening and SYN packets reached the control-plane NIC, but TCP handshake did not complete.
- `nft flush ruleset` restored SSH immediately, proving nftables ruleset interference.
- Multiple firewall managers/restorers caused recurrence until they were disabled/masked.
- Final stable state: SSH survived reboot after disabling/masking conflicting firewall services.

---

## Quick Incident Checklist

Use this when a node pings but SSH times out.

### A. Confirm scope

- `ping <node-ip>` works?
- `nc -vz <node-ip> 22` times out?
- `sshd` listening on `0.0.0.0:22`?

If yes to all three, suspect packet filtering path (not SSH daemon startup).

### B. Validate packet path

- Run `tcpdump` on node interface for `tcp port 22`.
- Trigger `nc` from client.
- If SYN arrives but handshake fails, host-side filtering/policy is likely blocking.

### C. Fast recovery

- Temporarily flush ruleset (`nft flush ruleset`) to restore access.
- Verify `nc` and SSH from client immediately.

### D. Prevent recurrence

- Disable/mask all overlapping firewall restorers not in use.
- Use one firewall framework only.
- Reboot-test SSH before declaring resolved.

---

## 1. Initial Symptoms

Observed behavior:

- `kubectl` could not connect to the cluster API server
- SSH to control-plane (`192.168.1.246`) timed out
- Worker node remained reachable
- App ingress (`immich.donovanlowkeen.com`) still worked in browser

At first glance, this looked contradictory: user-facing app traffic partly worked while control-plane access failed.

---

## 2. Why App Traffic Still Worked

This was a control-plane host path issue, not a total cluster network collapse.

- Ingress/data-plane traffic can continue on worker nodes
- Existing workloads may keep serving traffic
- But management plane (`kubectl` -> API server, SSH -> control-plane host) can still fail

This explains why ingress visibility did not imply control-plane health.

---

## 3. Diagnostic Timeline

### 3.1 Confirmed API target and failure mode

- Kubeconfig API endpoint: `https://192.168.1.246:6443`
- `kubectl` hung/failed with unreachable/timeout behavior
- Direct API probes timed out

Commands used:

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' && echo
kubectl cluster-info
kubectl --request-timeout=5s get nodes
nc -vz 192.168.1.246 6443
curl -k --max-time 5 https://192.168.1.246:6443/livez?verbose
```

### 3.2 Compared worker vs control-plane reachability

- Worker node reachable and responsive
- Control-plane path unstable/unreachable at times
- Neighbor/ARP state appeared inconsistent during the incident

Commands used:

```bash
ping -c 2 192.168.1.246
ping -c 2 192.168.1.116
arp -an | grep -E '192\.168\.1\.(246|116)'
nc -vz -w 4 192.168.1.246 22
nc -vz -w 4 192.168.1.116 22
```

### 3.3 Recovered base host networking incrementally

- Verified interface link state (`eno1` up, link detected)
- Corrected netplan file permissions (netplan apply had been blocked)
- Restored gateway ping at points in the troubleshooting cycle

Commands used on control-plane console:

```bash
ip -br a
ip route
ip -4 addr show dev eno1
sudo ethtool eno1 | grep -E 'Link detected|Speed|Duplex'
sudo chmod 600 /etc/netplan/01-network-manager-all.yaml
sudo chown root:root /etc/netplan/01-network-manager-all.yaml
sudo netplan generate
sudo netplan apply
ping -c 3 192.168.1.1
```

### 3.4 Identified config-management drift risk

Referenced file: `/etc/netplan/01-network-manager-all.yaml`

Command used:

```bash
sudo cat /etc/netplan/01-network-manager-all.yaml
sudo netplan get
```

File content at the time:

```yaml
network:
  version: 2
  renderer: NetworkManager
```

No explicit static interface config in that file meant behavior could drift depending on runtime state and tools in control.

### 3.5 Proved SSH daemon was not the primary failure

Key tests:

- `sshd` listening on `0.0.0.0:22`
- `ping` to control-plane succeeded
- `nc` to port 22 timed out
- `tcpdump` showed inbound TCP SYN (`S`) reaching `eno1`

Conclusion: traffic reached host, but handshake did not complete.

Commands used:

```bash
sudo ss -lntp | grep ':22'
ping -c 2 192.168.1.246
nc -vz 192.168.1.246 22
ssh -vvv donovan@192.168.1.246
sudo tcpdump -ni eno1 'tcp port 22'
```

### 3.6 Narrowed root cause to nftables/netfilter ruleset

Important validation step:

- `nft flush ruleset` immediately restored SSH connectivity

That was the decisive proof that host firewall/ruleset state (not sshd availability) was dropping/breaking connection establishment.

Commands used:

```bash
sudo nft list tables
sudo nft flush ruleset
nc -vz 192.168.1.246 22
ssh donovan@192.168.1.246
```

### 3.7 Prevented recurrence

Disabled/masked services that could reapply stale/conflicting firewall state:

- `nftables`
- `ufw`
- `netfilter-persistent`
- `iptables-persistent` (where present)

After reboot, SSH remained reachable, confirming startup ruleset restoration was a major recurrence trigger.

Commands used:

```bash
sudo systemctl disable --now nftables ufw netfilter-persistent iptables-persistent 2>/dev/null || true
sudo systemctl mask nftables ufw netfilter-persistent iptables-persistent 2>/dev/null || true
sudo reboot
```

---

## 4. Root Cause

Primary root cause:

- Conflicting/stale host firewall rules in nftables/netfilter path blocked SSH connection establishment on control-plane.

Contributing factors:

- Multiple overlapping network/firewall management layers
- Attempts across NetworkManager/networkd/manual `ip` changes during recovery
- Boot-time rule restoration services reintroducing broken state

---

## 5. The SSH SYN Rule Behavior (What Was Happening)

### 5.1 TCP handshake refresher

SSH over TCP needs a 3-way handshake:

1. Client sends `SYN`
2. Server responds `SYN-ACK`
3. Client responds `ACK`
4. SSH session starts

### 5.2 Observed failure pattern

- We saw inbound `SYN` packets on `eno1` via `tcpdump`
- `sshd` was listening on port 22
- But client still timed out (no successful handshake)

This means packet filtering in kernel network path prevented normal handshake completion (either by dropping the inbound flow before proper acceptance or blocking reply path behavior).

### 5.3 Why this is important

When SYN/SYN-ACK flow is interrupted:

- SSH appears "down" to clients
- No auth prompt appears
- Key/password exchange never starts
- Service-level checks can look healthy locally while remote access fails

So a host can have a running `sshd` process but still be operationally inaccessible.

---

## 6. Why It Recurred After Partial Fixes

Individual commands (UFW rule tweaks, iptables changes) temporarily altered behavior, but recurrence happened because other boot/runtime services reloaded rules later.

General lesson:

- If multiple firewall managers are active, one can silently override another.
- A seemingly "fixed" host can break again on restart or service reload.

---

## 7. Current Known-Good Outcome

- Control-plane SSH is reachable and remains reachable after reboot.
- Problematic firewall restorer services are disabled/masked.
- Immediate lockout loop is resolved.

---

## 8. Operational Lessons Learned

1. Validate packet path first (`tcpdump`) when service listens but clients time out.
2. Avoid mixed firewall control planes (UFW + nftables + persistent restorers simultaneously).
3. Use one network authority per host role and make persistence explicit.
4. For control-plane nodes, prioritize deterministic, server-style network configuration.
5. Keep console access available during firewall/network migrations to avoid lockout.

---

## 9. Follow-up Recommendations

- Reintroduce hardening later with **one** firewall framework only.
- Add explicit SSH allow from trusted LAN/admin IPs before any default deny policy.
- Apply and test Kubernetes control-plane port rules (`6443` and other required control-plane/node flows) in staged order.
- Reboot-test after each policy milestone to ensure persistence and no lockout regression.

---

## 10. Why Ubuntu Has Multiple Firewall Tools (And What Kubernetes Adds)

In this incident, multiple rule-management layers existed at once, which increased conflict risk.

### 10.1 Host-level tools on Ubuntu

- `iptables`: legacy CLI used widely by older tooling and scripts.
- `nftables`: modern netfilter framework in Linux.
- `ufw`: user-friendly frontend that applies firewall policy underneath.
- `netfilter-persistent` / `iptables-persistent`: boot-time rule save/restore helpers.

Why they coexist:

- Backward compatibility with older scripts/docs/packages.
- Gradual migration from iptables-era workflows to nftables-era workflows.
- Different operator preferences (simple frontend vs low-level control).

### 10.2 Kubernetes-managed networking rules

On kubeadm nodes, Kubernetes components also manipulate packet-filtering/NAT state:

- `kube-proxy` writes service-routing/load-balancing rules.
- CNI plugins (Flannel/Calico/Cilium, etc.) write forwarding/NAT rules needed for pod traffic.
- Runtime/network components may create additional chains/rules as pods/services change.

Important implication:

- Even with a host firewall strategy, cluster networking will still add dynamic rule state.
- The goal is not "no dynamic rules"; the goal is one clear host-firewall authority and no conflicting restorers.

---

## 11. How To Tell Who Is Writing/Overriding Rules

Use this as a practical attribution checklist.

### 11.1 Check which services are active/enabled

```bash
sudo systemctl status ufw nftables netfilter-persistent iptables-persistent --no-pager
sudo systemctl is-enabled ufw nftables netfilter-persistent iptables-persistent
```

If a service is enabled, it can reapply policy at boot or reload.

### 11.2 Identify backend and current ruleset owners

```bash
sudo iptables --version
sudo update-alternatives --display iptables
sudo nft list tables
sudo nft list ruleset
sudo iptables-save
```

Notes:

- On modern Ubuntu, `iptables` may use nft backend (`iptables-nft`) via compatibility layer.
- Seeing both `iptables` output and nft tables is normal; attribution depends on which services/tools are writing.

### 11.3 Attribute Kubernetes-managed chains

Look for recognizable chain/table markers:

- `KUBE-*` (kube-proxy/Kubernetes service routing)
- CNI-specific markers (plugin dependent)

Commands:

```bash
sudo iptables-save | grep -E 'KUBE-|CNI|FLANNEL|CALICO|CILIUM'
sudo nft list ruleset | grep -E 'KUBE-|CNI|FLANNEL|CALICO|CILIUM'
```

### 11.4 Detect boot-time restorers and custom scripts

```bash
sudo systemctl list-unit-files | grep -E 'ufw|nft|netfilter|iptables'
sudo grep -R "iptables\|nft\|ufw" /etc/systemd /etc/init.d /etc/rc.local /usr/local/sbin /usr/local/bin 2>/dev/null
```

This finds other units/scripts that might silently reapply rules.

### 11.5 Observe live rule changes over time

```bash
sudo journalctl -u ufw -u nftables -u netfilter-persistent -b --no-pager
sudo watch -n 2 "sudo nft list ruleset | head -n 120"
```

If rules change after a service restart/reload, that service is a writer.

---

## 12. Precedence and "Which Rule Wins?"

Conceptually, precedence is determined by packet path + rule order + chain policy.

- First matching terminal action (`accept`/`drop`/`reject`) in the evaluated chain/path decides fate.
- Chain default policy (for example, `DROP`) applies if no prior accept matched.
- Earlier hooks/chains can prevent later rules from ever being reached.
- Mixed frameworks can be deceptive when one tool writes via compatibility layers into another backend.

Operationally:

1. A rule that matches earlier in packet traversal can override later expectations.
2. A restorer service that runs later can overwrite prior runtime edits.
3. Kubernetes dynamic chains can coexist with host policy, but misordered host policy can break cluster/control-plane traffic.

Best practice:

- Use one host firewall authority.
- Keep only one persistent restorer path.
- Validate with packet capture (`tcpdump`) plus current ruleset dumps when behavior is ambiguous.
