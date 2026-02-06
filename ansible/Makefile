# Homelab Ansible - run from ansible/: make <target>
# Prereqs: ansible-core, SSH key-based access to hosts

PLAYBOOK := playbooks/site.yml
LIMIT_NODES := control-plane,worker-1

.PHONY: ping run run-nodes run-gpu run-gpu-base run-gpu-k8s run-gpu-role list check check-base check-gpu check-with-password run-base run-with-password fetch-containerd-config diff-containerd-config help

BACKUPS := backups
CONTAINERD_BASE := $(BACKUPS)/control-plane-containerd-config.toml
CONTAINERD_GPU := $(BACKUPS)/gpu-node-containerd-config.toml

help:
	@echo "Homelab Ansible targets (run from ansible/):"
	@echo "  make ping                 - Test SSH to all hosts"
	@echo "  make list                 - List inventory hosts by group"
	@echo "  make run                  - Full playbook (all hosts)"
	@echo "  make run-nodes            - Playbook limited to control-plane + worker-1 (skip gpu-node)"
	@echo "  make run-gpu              - Full playbook on gpu-node only (base + k8s_node + gpu)"
	@echo "  make run-gpu-base         - base role only on gpu-node (incremental)"
	@echo "  make run-gpu-k8s          - k8s_node role only on gpu-node (incremental)"
	@echo "  make run-gpu-role         - gpu role only on gpu-node (incremental)"
	@echo "  make run-base             - Run only base role (all hosts)"
	@echo "  make check                - Dry run (full playbook, no changes)"
	@echo "  make check-base           - Dry run, base role only"
	@echo "  make check-gpu            - Dry run, gpu role only"
	@echo "  make check-with-password  - Dry run (full), prompt for sudo password (-K)"
	@echo "  make run-with-password    - Full playbook, prompt for sudo password (-K)"
	@echo "  make fetch-containerd-config - Copy containerd config.toml from all k8s nodes to backups/"
	@echo "  make diff-containerd-config  - Diff gpu-node vs control-plane containerd config (run fetch first)"

ping:
	ansible all -m ping

list:
	ansible-inventory -i inventory/hosts.yml --list
	@echo "---"
	ansible all --list-hosts

run:
	ansible-playbook $(PLAYBOOK) -K

run-base:
	ansible-playbook $(PLAYBOOK) --tags base -K

run-nodes:
	ansible-playbook $(PLAYBOOK) --limit $(LIMIT_NODES) -K

run-gpu:
	ansible-playbook $(PLAYBOOK) --limit gpu-node -K

run-gpu-base:
	ansible-playbook $(PLAYBOOK) --limit gpu-node --tags base -K

run-gpu-k8s:
	ansible-playbook $(PLAYBOOK) --limit gpu-node --tags k8s_node -K

run-gpu-role:
	ansible-playbook $(PLAYBOOK) --limit gpu-node --tags gpu -K

check:
	ansible-playbook $(PLAYBOOK) --check --diff -K

check-base:
	ansible-playbook $(PLAYBOOK) --check --diff --tags base -K

check-nodes:
	ansible-playbook $(PLAYBOOK) --check --diff --limit $(LIMIT_NODES) -K

check-gpu:
	ansible-playbook $(PLAYBOOK) --check --diff --limit gpu-node -K

check-gpu-base:
	ansible-playbook $(PLAYBOOK) --check --diff --limit gpu-node --tags base -K

check-gpu-k8s:
	ansible-playbook $(PLAYBOOK) --check --diff --limit gpu-node --tags k8s_node -K

check-gpu-role:
	ansible-playbook $(PLAYBOOK) --check --diff --limit gpu-node --tags gpu -K

fetch-containerd-config:
	ansible-playbook playbooks/fetch-containerd-config.yml -K

diff-containerd-config:
	@if [ ! -f $(CONTAINERD_GPU) ] || [ ! -f $(CONTAINERD_BASE) ]; then \
		echo "Run 'make fetch-containerd-config' first to create backups/"; exit 1; fi
	diff -u $(CONTAINERD_BASE) $(CONTAINERD_GPU) || true