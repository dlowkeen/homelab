# 2026-01-24 - Flannel CrashLoop on New GPU Worker Node

**Date:** 2026-01-24  
**Tags:** flannel, networking, worker-node, debugging

## Context

Added a new worker node (`donovan-xps-8930`) to the cluster for GPU workloads. The flannel daemonset pod is crashlooping on this node.

## Symptoms

- Flannel pod `kube-flannel-ds-zhrzt` in `CrashLoopBackOff`
- Container exits with code 1
- Cannot retrieve logs: `dial tcp 192.168.1.34:10250: i/o timeout`
- Node shows as `Ready` in Kubernetes
- Node has podCIDR assigned: `10.244.2.0/24`
- Other nodes' flannel pods are running fine

## Root Cause Analysis

The kubelet connection timeout suggests network connectivity issues between the control plane and the new node. Based on previous experience adding worker nodes, common causes are:

1. **Multiple default routes** (WiFi + Ethernet)
2. **iptables conflicts** from previous CNI installations
3. **Missing or incorrect CNI binaries**
4. **Network interface misconfiguration**

## Debugging Steps

### 1. Check Network Interfaces on New Node

SSH to `donovan-xps-8930` and check for multiple default routes:

```bash
ip route | grep default
```

**Expected:** Single default route via Ethernet  
**Problem:** Multiple default routes (WiFi + Ethernet) can cause flaky overlay routing

### 2. Check Network Interface Flannel Will Use

```bash
ip addr show
```

Flannel needs to identify the correct interface. If multiple interfaces exist, flannel might bind to the wrong one.

### 3. Check iptables State

```bash
sudo iptables -L -n -v
sudo iptables -t nat -L -n -v
```

Look for conflicting rules or remnants from previous CNI installations.

### 4. Check CNI Binaries

```bash
ls -la /opt/cni/bin/
```

Should contain flannel CNI plugins. If missing or incorrect, flannel won't work.

### 5. Get Actual Container Logs (CRITICAL)

Since kubectl logs times out, get logs directly on the node:

```bash
# On donovan-xps-8930, get the container ID
sudo crictl ps -a | grep flannel

# Get logs from the container (replace with actual container ID)
sudo crictl logs <container-id>

# Or get logs from the most recent flannel container
sudo crictl logs $(sudo crictl ps -a --name kube-flannel --quiet | tail -1)
```

This will show the actual error message causing the crash.

### 6. Check Network Connectivity

From control plane node, test connectivity:

```bash
# Test kubelet API
curl -k https://192.168.1.34:10250/healthz

# Test node-to-node connectivity
ping 192.168.1.34
```

## Common Fixes

### Fix 1: Disable WiFi (if multiple interfaces)

```bash
# On the new node
sudo ip link set wlp2s0 down  # Replace with actual WiFi interface name
```

### Fix 2: Flush iptables (if conflicts)

```bash
# On the new node - BE CAREFUL, this will break active connections
sudo iptables -F
sudo iptables -X
sudo iptables -t nat -F
sudo iptables -t mangle -F
sudo iptables -t raw -F
```

Then restart kubelet:
```bash
sudo systemctl restart kubelet
```

### Fix 3: Reinstall CNI Binaries

```bash
# Remove old CNI binaries
sudo rm -rf /opt/cni/bin/*

# Flannel daemonset will reinstall them via init containers
# Or manually install if needed
```

### Fix 4: Check Flannel Interface Binding

Flannel might be trying to bind to the wrong interface. Check flannel config:

```bash
kubectl get configmap kube-flannel-cfg -n kube-flannel -o yaml
```

If needed, you can specify the interface in the daemonset args.

## Current Status

**Node:** `donovan-xps-8930` (192.168.1.34)  
**PodCIDR:** `10.244.2.0/24`  
**Network:** Single default route via `enp4s0` ✓  
**WiFi:** Down (NO-CARRIER) ✓  
**Kubelet:** Running ✓  
**Issue:** Both flannel and metallb speaker crashlooping

**Next Steps:** Get actual container logs using `crictl logs` on the node to see the root cause.

## Root Cause Found

**Error from flannel logs:**
```
Failed to create SubnetManager: error retrieving pod spec for 'kube-flannel/kube-flannel-ds-zhrzt': 
Get "https://10.96.0.1:443/api/v1/namespaces/kube-flannel/pods/kube-flannel-ds-zhrzt": 
dial tcp 10.96.0.1:443: i/o timeout
```

**Problem:** Flannel cannot reach the Kubernetes API server at `10.96.0.1:443`

**Root Cause:** UFW (Uncomplicated Firewall) is enabled with a DROP policy on the INPUT chain:
```
Chain INPUT (policy DROP 764 packets, 45840 bytes)
```

UFW and Kubernetes conflict because:
- Kubernetes manages its own iptables rules for pod networking
- UFW's DROP policy blocks Kubernetes API server traffic
- This creates a chicken-and-egg problem: flannel needs API access to work, but UFW blocks it

## Resolution

### Fix: Disable UFW on Kubernetes Nodes

UFW should not be used on Kubernetes worker nodes. Kubernetes manages firewall rules via iptables.

```bash
# On donovan-xps-8930
sudo ufw disable

# Verify it's disabled
sudo ufw status
```

**After disabling UFW:**
1. Restart kubelet to pick up the new iptables state:
   ```bash
   sudo systemctl restart kubelet
   ```

2. Delete the flannel pod to force a restart:
   ```bash
   kubectl delete pod kube-flannel-ds-zhrzt -n kube-flannel
   ```

3. Verify flannel starts successfully:
   ```bash
   kubectl get pods -n kube-flannel -w
   ```

### Why This Happens

Kubernetes nodes should not run UFW or firewalld because:
- Kubernetes components (kubelet, kube-proxy, CNI plugins) need to manage iptables
- Pod networking requires specific iptables rules
- UFW/firewalld can interfere with these rules
- The default DROP policy blocks necessary Kubernetes traffic

### Prevention

When adding new nodes to a Kubernetes cluster:
1. **Do not enable UFW or firewalld**
2. If UFW is already enabled, disable it before joining the cluster
3. Use Kubernetes NetworkPolicies for pod-level firewall rules instead

## Related Issues

The same UFW issue likely affects the metallb speaker pod, which is also crashlooping. After disabling UFW, both should recover.

---

## Final Resolution: br_netfilter Kernel Module

After fixing UFW and the node becoming Ready, flannel was still crashlooping with:
```
Failed to check br_netfilter: stat /proc/sys/net/bridge/bridge-nf-call-iptables: no such file or directory
```

**Solution:**
```bash
# Load the br_netfilter module
sudo modprobe br_netfilter

# Make it persistent across reboots
echo 'br_netfilter' | sudo tee -a /etc/modules-load.d/k8s.conf
echo 'net.bridge.bridge-nf-call-iptables = 1' | sudo tee -a /etc/sysctl.d/k8s.conf
sudo sysctl --system
```

After loading the module, flannel started successfully.

**Final Status:**
- Node `donovan-xps-8930` is Ready ✓
- Flannel pod `kube-flannel-ds-nvf92` is Running ✓
- All daemonset pods (kube-proxy, metallb speaker, node-exporter) are Running ✓
- Pod networking is functional ✓

---

## Key Learning: Node Prerequisites for CNI Plugins

**Important:** Just joining a node to a Kubernetes cluster is not enough. The node must have proper prerequisites configured for the CNI plugin (Flannel) to work.

### Required Prerequisites for Flannel

1. **Kernel Modules**
   - `br_netfilter` - Required for bridge networking
   - Load with: `sudo modprobe br_netfilter`
   - Make persistent: Add to `/etc/modules-load.d/k8s.conf`

2. **Sysctl Settings**
   - `net.bridge.bridge-nf-call-iptables = 1`
   - `net.bridge.bridge-nf-call-ip6tables = 1`
   - `net.ipv4.ip_forward = 1`
   - Configure in `/etc/sysctl.d/k8s.conf`

3. **Firewall Configuration**
   - **UFW must be disabled** on Kubernetes nodes
   - Kubernetes manages iptables rules
   - UFW conflicts with Kubernetes networking

4. **Network Configuration**
   - Single default route (no WiFi + Ethernet conflicts)
   - Proper network interface configuration
   - No conflicting iptables rules

### Pre-Join Checklist for New Nodes

Before joining a node to the cluster, ensure:

```bash
# 1. Disable UFW
sudo ufw disable
sudo ufw --force reset

# 2. Load required kernel modules
sudo modprobe br_netfilter
echo 'br_netfilter' | sudo tee -a /etc/modules-load.d/k8s.conf

# 3. Configure sysctl
cat <<EOF | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
sudo sysctl --system

# 4. Verify network configuration
ip route | grep default  # Should show single default route
ip link show  # Check interfaces

# 5. Ensure no conflicting firewall rules
sudo iptables -F  # Only if needed, be careful
```

### Why This Matters

CNI plugins like Flannel need:
- **Kernel modules** to create and manage network bridges
- **Sysctl settings** to allow iptables rules on bridges
- **Clean iptables** state so Kubernetes can manage rules
- **Network connectivity** to reach the API server

Without these, the CNI plugin pods will crashloop even if the node joins successfully.

---

## Troubleshooting: Node NotReady After UFW Disable

### Symptoms
- Node shows `NotReady` status
- Node conditions show "Kubelet stopped posting node status"
- Some pods running (kube-proxy, speaker) but flannel stuck in Pending
- Last heartbeat was several minutes ago

### Diagnosis Steps (Run on the node: donovan-xps-8930)

#### 1. Check if kubelet is running
```bash
sudo systemctl status kubelet
```

**Expected:** `Active: active (running)`  
**If not running:** `sudo systemctl start kubelet`

#### 2. Check kubelet logs for errors
```bash
sudo journalctl -u kubelet -n 100 --no-pager | tail -50
```

Look for:
- Connection errors to API server
- Certificate errors
- Network errors

#### 3. Test API server connectivity
```bash
# Get the API server endpoint from kubeconfig
grep server /etc/kubernetes/kubelet.conf

# Test connectivity (replace with your API server IP)
curl -k https://<API_SERVER_IP>:6443/healthz
```

#### 4. Check kubelet configuration
```bash
# Check kubelet config
cat /var/lib/kubelet/config.yaml | grep -A 5 "server"

# Check kubelet kubeconfig
cat /etc/kubernetes/kubelet.conf
```

#### 5. Verify network connectivity to control plane
```bash
# Ping control plane node
ping 192.168.1.246  # Replace with your control plane IP

# Test API server port
telnet 192.168.1.246 6443  # Or use nc
```

#### 6. Check if iptables rules are blocking
```bash
# Check if there are blocking rules
sudo iptables -L -n -v | grep -i drop
sudo iptables -L -n -v | grep -i reject

# Check FORWARD chain (important for pod networking)
sudo iptables -L FORWARD -n -v
```

#### 7. Verify UFW is actually disabled
```bash
sudo ufw status
# Should show: Status: inactive

# If still active, check what's blocking
sudo ufw status verbose
```

### Common Issues After UFW Disable

1. **Kubelet can't reach API server**
   - Check firewall rules: `sudo iptables -L -n -v`
   - Verify API server is accessible: `curl -k https://<API_SERVER>:6443/healthz`
   - Check kubelet kubeconfig has correct server address

2. **Kubelet certificate issues**
   - Check certificates: `ls -la /var/lib/kubelet/pki/`
   - Verify kubelet.conf has correct CA

3. **Network interface issues**
   - Verify default route: `ip route | grep default`
   - Check DNS resolution: `nslookup kubernetes.default`

### Quick Fixes to Try

```bash
# 1. Restart kubelet
sudo systemctl restart kubelet

# 2. Check if it starts
sudo systemctl status kubelet

# 3. If kubelet won't start, check logs
sudo journalctl -u kubelet -n 50

# 4. Verify network connectivity
ping <control-plane-ip>
curl -k https://<control-plane-ip>:6443/healthz
```

---

## Issue: TLS Handshake Timeout After UFW Disable

### Symptoms
- Kubelet running but can't connect to API server
- Errors: `net/http: TLS handshake timeout` to `https://192.168.1.246:6443`
- Node stuck in `NotReady` state
- All API calls timing out during TLS handshake

### Root Cause
Even after disabling UFW, there may be:
1. **Remaining iptables rules** from UFW that weren't cleared
2. **UFW rules still active** despite showing "inactive"
3. **Network connectivity issues** preventing TLS handshake completion

### Diagnosis (Run on donovan-xps-8930)

#### 1. Test basic connectivity to API server
```bash
# Test if you can reach the API server IP
ping 192.168.1.246

# Test if port 6443 is open (should timeout or connect, not "connection refused")
timeout 5 bash -c "</dev/tcp/192.168.1.246/6443" && echo "Port open" || echo "Port closed/timeout"
```

#### 2. Test TLS handshake manually
```bash
# Try to connect and see what happens
openssl s_client -connect 192.168.1.246:6443 -showcerts < /dev/null
```

#### 3. Check if UFW rules are actually gone
```bash
# Check UFW status
sudo ufw status verbose

# Check if UFW is still managing iptables
sudo iptables -L -n -v | grep -i ufw

# Check for any DROP rules in INPUT chain
sudo iptables -L INPUT -n -v | grep DROP
```

#### 4. Check for other firewall rules
```bash
# Check all iptables chains
sudo iptables -L -n -v
sudo iptables -t nat -L -n -v
sudo iptables -t filter -L -n -v

# Look for rules blocking 6443
sudo iptables -L -n -v | grep 6443
```

#### 5. Check if there are other firewalls
```bash
# Check for firewalld
sudo systemctl status firewalld

# Check for nftables
sudo nft list ruleset
```

### Fixes to Try

#### Fix 1: Completely remove UFW and flush iptables
```bash
# Disable and remove UFW
sudo ufw disable
sudo ufw --force reset

# Flush all iptables rules (BE CAREFUL - this will break active connections)
sudo iptables -F
sudo iptables -X
sudo iptables -t nat -F
sudo iptables -t nat -X
sudo iptables -t mangle -F
sudo iptables -t mangle -X

# Restart kubelet to let it rebuild iptables
sudo systemctl restart kubelet
```

**Warning:** Flushing iptables will break active connections. You may need to reconnect via SSH.

#### Fix 2: Check if API server is actually listening
From the control plane node (`donovan-optiplex-9020m`):
```bash
# Check if API server is listening
sudo netstat -tlnp | grep 6443
# or
sudo ss -tlnp | grep 6443
```

#### Fix 3: Check network routing
```bash
# On donovan-xps-8930, check routing
ip route get 192.168.1.246
```

#### Fix 4: Temporarily allow API server port
If you need to keep some firewall rules:
```bash
# Allow API server port (if you have a firewall)
sudo iptables -I INPUT -p tcp --dport 6443 -j ACCEPT
sudo iptables -I OUTPUT -p tcp --sport 6443 -j ACCEPT
```

#### Fix 5: Test TLS Connection Directly
Since port is open but TLS handshake times out, test the TLS connection:

```bash
# Test TLS handshake manually
timeout 10 openssl s_client -connect 192.168.1.246:6443 -showcerts < /dev/null

# If that times out, check if there's something intercepting TLS
curl -v -k https://192.168.1.246:6443/healthz --max-time 10
```

#### Fix 6: Check for Network-Level Issues
```bash
# Check MTU size (should match network)
ip link show enp4s0 | grep mtu

# Check for packet loss
ping -c 100 192.168.1.246 | grep loss

# Check routing
ip route get 192.168.1.246

# Check if there are any proxy settings interfering
env | grep -i proxy
```

#### Fix 7: Check API Server from Control Plane
The issue might be on the API server side. From the control plane node:

```bash
# Check if API server is actually listening
sudo ss -tlnp | grep 6443

# Check API server logs
sudo journalctl -u kube-apiserver -n 50 | grep -i error

# Check if API server is rate-limiting this node
sudo journalctl -u kube-apiserver -n 100 | grep "192.168.1.34"
```

## Related Resources

- Previous worker node addition: `./2026-01-01-add-worker-node.md`
- Flannel documentation: https://github.com/flannel-io/flannel
