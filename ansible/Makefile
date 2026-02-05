# Homelab Ansible - run from ansible/: make <target>
# Prereqs: ansible-core, SSH key-based access to hosts

PLAYBOOK := playbooks/site.yml
LIMIT_NODES := control-plane,worker-1

.PHONY: ping run run-nodes run-gpu list check check-base check-gpu check-with-password run-base run-with-password help

help:
	@echo "Homelab Ansible targets (run from ansible/):"
	@echo "  make ping               - Test SSH to all hosts"
	@echo "  make list               - List inventory hosts by group"
	@echo "  make run                - Full playbook (all hosts)"
	@echo "  make run-nodes          - Playbook limited to control-plane + worker-1 (skip gpu-node)"
	@echo "  make run-gpu            - Playbook limited to gpu-node only"
	@echo "  make run-base           - Run only base role (all hosts)"
	@echo "  make check              - Dry run (full playbook, no changes)"
	@echo "  make check-base         - Dry run, base role only"
	@echo "  make check-gpu          - Dry run, gpu role only"
	@echo "  make check-with-password - Dry run (full), prompt for sudo password (-K)"
	@echo "  make run-with-password  - Full playbook, prompt for sudo password (-K)"

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

check:
	ansible-playbook $(PLAYBOOK) --check --diff -K

check-base:
	ansible-playbook $(PLAYBOOK) --check --diff --tags base -K

check-nodes:
	ansible-playbook $(PLAYBOOK) --check --diff --limit $(LIMIT_NODES) -K

check-gpu:
	ansible-playbook $(PLAYBOOK) --check --diff --tags gpu -K
