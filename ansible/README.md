# Ansible homelab provisioning

Standardizes packages and config across Ubuntu nodes (control plane, workers, GPU node) so adding a new node is "add to inventory, run playbook" instead of copy-pasting from notes.

## Prerequisites

- Python 3 with `ansible-core` (e.g. `pip install -r requirements.txt` in a venv).
- SSH key-based access to all hosts in the inventory.

## Setup

1. Edit `inventory/hosts.yml`: replace placeholder hostnames with your real hostnames or set `ansible_host` to each node's IP or FQDN.
2. Ensure the Dell XPS 8930 (or any GPU node) is listed in **both** `workers` and `gpu_nodes` so it gets the `k8s_node` and `gpu` roles.
3. Run playbooks from the `ansible/` directory.

## Usage

From the `ansible/` directory you can use the **Makefile** or run commands directly:

```bash
cd ansible

# Show all targets
make help

# Check connectivity
make ping

# Run full provisioning (all hosts)
make run

# Run only on control-plane + worker-1 (skip gpu-node when it's unplugged)
make run-nodes

# Run only on gpu-node
make run-gpu

# Run only base role
make run-base

# Dry run (no changes)
make check

# Prompt for sudo password when running playbook
make run-with-password
```

Or run Ansible directly, e.g. `ansible all -m ping`, `ansible-playbook playbooks/site.yml --limit control-plane,worker-1`.

## Roles

| Role       | Applied to   | Purpose |
|-----------|--------------|--------|
| **base**  | all          | Swap off, SSH, common packages. |
| **k8s_node** | k8s_nodes  | containerd, kubeadm/kubelet/kubectl, kernel modules, sysctls. Does not run `kubeadm init`/`join`. |
| **gpu**   | gpu_nodes    | NVIDIA driver, container toolkit, containerd NVIDIA runtime; optional nerdctl and headless mode. |

## Variables

- **base**: defined in `group_vars/all.yml` and optional `host_vars/*.yml`
  - `manage_network_stack` (default `false`) — enable network manager standardization tasks
  - `network_renderer` (default `networkd`) — target renderer when network stack management is enabled
  - `netplan_config_path` (default `/etc/netplan/01-network-manager-all.yaml`) — authoritative netplan file managed by Ansible
  - `cleanup_legacy_netplan` (default `false`) — remove explicitly listed legacy netplan files during migration
  - `legacy_netplan_remove_paths` (default `[]`) — exact file paths to remove when cleanup is enabled
  - `manage_firewall_stack` (default `false`) — enable host firewall manager standardization tasks
  - `disable_host_firewall_managers` (default `true`) — disable/mask `ufw`, `nftables`, `netfilter-persistent`, `iptables-persistent` when firewall management is enabled
  - `primary_nic`, `node_ipv4_address`, `node_ipv4_gateway`, `node_dns_servers` — per-node addressing inputs for future templated network config
- **k8s_node**: `roles/k8s_node/defaults/main.yml` — `k8s_apt_version` (e.g. `v1.31`), `kube_hold_packages`.
- **gpu**: `roles/gpu/defaults/main.yml` — `nvidia_driver_version`, `nerdctl_version`, `gpu_headless` (set `true` for dedicated GPU nodes to switch to multi-user target and disable gdm3).

## After provisioning

- Run `kubeadm join` (or init on the control plane) manually or via a separate playbook; this playbook only prepares the OS.
- After installing the NVIDIA driver, reboot the GPU node to load the driver.
- If you set `gpu_headless: true`, reboot the GPU node to complete headless mode.
