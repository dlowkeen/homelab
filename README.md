# homelab

This is IaC for Donovan's homelab k8s cluster.


# ArgoCD

Dev is disabled to save on resources. It is just here to test version upgrades. To enable it, comment in the `notications` and `redis` resources and set `enabled` to `true`. Then set all `replicas` in the `helmrelease.yaml` to 1.

