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

```bash
cd ansible

# Check connectivity
ansible all -m ping

# Run full provisioning (base -> k8s_node -> gpu)
ansible-playbook playbooks/site.yml

# Run only base role on all hosts
ansible-playbook playbooks/site.yml --tags base

# Limit to one host (e.g. when adding a new node)
ansible-playbook playbooks/site.yml --limit gpu-node
```

## Roles

| Role       | Applied to   | Purpose |
|-----------|--------------|--------|
| **base**  | all          | Swap off, SSH, common packages. |
| **k8s_node** | k8s_nodes  | containerd, kubeadm/kubelet/kubectl, kernel modules, sysctls. Does not run `kubeadm init`/`join`. |
| **gpu**   | gpu_nodes    | NVIDIA driver, container toolkit, containerd NVIDIA runtime; optional nerdctl and headless mode. |

## Variables

- **k8s_node**: `roles/k8s_node/defaults/main.yml` — `k8s_apt_version` (e.g. `v1.31`), `kube_hold_packages`.
- **gpu**: `roles/gpu/defaults/main.yml` — `nvidia_driver_version`, `nerdctl_version`, `gpu_headless` (set `true` for dedicated GPU nodes to switch to multi-user target and disable gdm3).

## After provisioning

- Run `kubeadm join` (or init on the control plane) manually or via a separate playbook; this playbook only prepares the OS.
- After installing the NVIDIA driver, reboot the GPU node to load the driver.
- If you set `gpu_headless: true`, reboot the GPU node to complete headless mode.
