#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Renew kubeadm control-plane certificates and optionally export kubeconfig.

Usage:
  ./scripts/renew-k8s-certs.sh [--kubeconfig-out PATH] [--restart-kubelet]

Options:
  --kubeconfig-out PATH  Write a non-root-readable copy of /etc/kubernetes/admin.conf to PATH.
                         Useful for copying to your local machine with scp.
  --restart-kubelet      Restart kubelet after cert renewal.
  -h, --help             Show this help.

Examples:
  # Renew all control-plane certs and restart kubelet
  ./scripts/renew-k8s-certs.sh --restart-kubelet

  # Renew certs and export kubeconfig for scp
  ./scripts/renew-k8s-certs.sh --restart-kubelet --kubeconfig-out /tmp/admin.conf
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: required command not found: $cmd" >&2
    exit 1
  fi
}

KUBECONFIG_OUT=""
RESTART_KUBELET=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --kubeconfig-out)
      [[ $# -ge 2 ]] || { echo "Error: --kubeconfig-out requires a path" >&2; exit 1; }
      KUBECONFIG_OUT="$2"
      shift 2
      ;;
    --restart-kubelet)
      RESTART_KUBELET=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd kubeadm
require_cmd openssl

if [[ $EUID -ne 0 ]]; then
  echo "Error: run as root (or with sudo)." >&2
  exit 1
fi

echo "==> Current certificate expiration"
kubeadm certs check-expiration

echo
echo "==> Renewing all kubeadm certificates"
kubeadm certs renew all

if [[ $RESTART_KUBELET -eq 1 ]]; then
  echo
  echo "==> Restarting kubelet"
  systemctl restart kubelet
fi

echo
echo "==> Updated API server certificate dates"
openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -dates

if [[ -n "$KUBECONFIG_OUT" ]]; then
  echo
  echo "==> Exporting kubeconfig to $KUBECONFIG_OUT"
  install -m 600 /etc/kubernetes/admin.conf "$KUBECONFIG_OUT"
fi

echo
echo "Done."
