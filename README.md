# homelab

This is IaC for Donovan's homelab k8s cluster.

## Managing Development Environments

### Immich Dev

Immich-dev is scaled down to 0 replicas to save resources. It's used for testing version upgrades before applying to production.

#### To Enable immich-dev:

1. Edit `components/immich/overlays/dev/helmrelease.yaml`
2. In the `postRenderers.kustomize.patches` section, **remove** or **comment out** the replica patch:
   ```yaml
   # Remove these lines:
   - op: replace
     path: /spec/replicas
     value: 0
   ```
3. Commit and push:
   ```bash
   git add components/immich/overlays/dev/helmrelease.yaml
   git commit -m "feat: enable immich-dev"
   git push
   ```
4. Reconcile Flux:
   ```bash
   flux reconcile ks components --with-source
   ```

#### To Disable immich-dev:

1. Edit `components/immich/overlays/dev/helmrelease.yaml`
2. In the `postRenderers.kustomize.patches` section for the `immich-server` deployment, **add** the replica patch as the **first** operation:
   ```yaml
   patches:
     - target:
         kind: Deployment
         name: immich-server
       patch: |-
         - op: replace
           path: /spec/replicas
           value: 0
         # ... rest of patches
   ```
3. Commit, push, and reconcile as above

### ArgoCD Dev

Dev is disabled to save on resources. It is just here to test version upgrades. To enable it, comment in the `notifications` and `redis` resources and set `enabled` to `true`. Then set all `replicas` in the `helmrelease.yaml` to 1.


# Node Configuration

This section describes the node labels, taints, and scheduling preferences for the cluster.

## Node Labels

Apply these labels to nodes:

```bash
# Control plane node
kubectl label node donovan-optiplex-9020m node-role.kubernetes.io/control-plane=true --overwrite
kubectl label node donovan-optiplex-9020m node-type=control-plane --overwrite

# Worker node
kubectl label node worker-01-optiplex-7050 node-type=worker --overwrite

# GPU worker node (Dell XPS 8930)
kubectl label node donovan-xps-8930 node-type=gpu --overwrite
```

## Node Taints (Optional)

To enforce strict scheduling, apply taints:

```bash
# Taint control plane to prevent application workloads
kubectl taint nodes donovan-optiplex-9020m node-role.kubernetes.io/control-plane:NoSchedule --overwrite

# Taint GPU node to prevent non-GPU workloads
kubectl taint nodes donovan-xps-8930 nvidia.com/gpu:NoSchedule --overwrite
```

**Note:** If you apply the control plane taint, ensure all infrastructure pods (Flux, cert-manager, monitoring, etc.) have the appropriate tolerations configured in their HelmRelease values.

## Scheduling Strategy

### Control Plane Node (donovan-optiplex-9020m)
**Should run:**
- All `kube-system` pods (etcd, kube-apiserver, kube-controller-manager, kube-scheduler, kube-proxy, coredns)
- Flux controllers (source-controller, kustomize-controller, helm-controller, notification-controller)
- cert-manager pods
- Monitoring stack (Prometheus, Grafana, Alertmanager, prometheus-operator, kube-state-metrics)
- ArgoCD pods
- MetalLB controller

**Should NOT run:**
- Application workloads

### Worker Node (worker-01-optiplex-7050)
**Should run:**
- Application workloads (immich, karakeep, threadwise)
- Ingress controller (can be on either, but worker is fine)

**Can run:**
- DaemonSets (flannel, node-exporter, metallb-speaker) - run on all nodes

## Applying Configuration

Run the following commands to apply labels:

```bash
# Label control plane
kubectl label node donovan-optiplex-9020m node-role.kubernetes.io/control-plane=true --overwrite
kubectl label node donovan-optiplex-9020m node-type=control-plane --overwrite

# Label worker
kubectl label node worker-01-optiplex-7050 node-type=worker --overwrite
```

If you want to enforce strict scheduling with taints:

```bash
# Taint control plane (requires tolerations on infrastructure pods)
kubectl taint nodes donovan-optiplex-9020m node-role.kubernetes.io/control-plane:NoSchedule --overwrite
```
