# Kubernetes Certificate Renewal Runbook

Use this when `kubectl` fails with x509 expiry errors such as:

- `x509: certificate has expired or is not yet valid`
- `You must be logged in to the server (Unauthorized)` after rotating some certs

## Script

Run on a kubeadm control-plane node:

```bash
sudo ./scripts/renew-k8s-certs.sh --restart-kubelet --kubeconfig-out /tmp/admin.conf
```

What this does:

- runs `kubeadm certs check-expiration`
- renews all kubeadm-managed certificates
- optionally restarts kubelet so static pods pick up updates
- optionally exports `/etc/kubernetes/admin.conf` to a readable path for `scp`

## Copy refreshed kubeconfig to your workstation

From your local machine:

```bash
scp <control-plane-host>:/tmp/admin.conf ~/.kube/config
chmod 600 ~/.kube/config
```

Then verify:

```bash
kubectl config view --raw -o jsonpath='{.users[0].user.client-certificate-data}' \
| base64 --decode | openssl x509 -noout -dates

kubectl get nodes
```

## Troubleshooting

- If local `kubectl` still fails TLS verification after renewal, make sure your local `~/.kube/config` was replaced with the new `admin.conf`.
- If control-plane access fails after renewal, verify static pod restart and inspect:
  - `sudo crictl ps | grep kube-apiserver`
  - `sudo journalctl -u kubelet -n 200 --no-pager`
- Clean up temporary kubeconfig export:

```bash
ssh <control-plane-host> 'rm -f /tmp/admin.conf'
```
