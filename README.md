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

