#!/usr/bin/env bash
# Run ON the VM after 02-vm-setup.sh and 03-build-and-push.sh.
# Pins /etc/ocrapi.image to the desired tag and (re)starts the systemd unit.
#
# Pass the image tag explicitly:
#   sudo IMAGE=us-central1-docker.pkg.dev/PROJECT/ocrapi/ocrapi:2026-06-07-abc1234 \
#        bash 04-run.sh
#
# Or fall back to :latest (not recommended for prod, but handy in test):
#   sudo bash 04-run.sh

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-ocrapi}"

if [[ -z "${IMAGE:-}" ]]; then
  if [[ -z "${PROJECT_ID}" ]]; then
    echo "ERROR: pass IMAGE=..., or set PROJECT_ID so we can default to :latest." >&2
    exit 1
  fi
  IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/ocrapi:latest"
  echo "==> No IMAGE provided; defaulting to ${IMAGE}"
fi

echo "==> Pinning /etc/ocrapi.image -> ${IMAGE}"
echo "IMAGE=${IMAGE}" > /etc/ocrapi.image
chmod 644 /etc/ocrapi.image

echo "==> Enabling + (re)starting ocrapi.service..."
systemctl daemon-reload
systemctl enable ocrapi.service
systemctl restart ocrapi.service

sleep 3
systemctl status ocrapi.service --no-pager || true

echo
echo "==> Health check..."
for i in 1 2 3 4 5 6 7 8 9 10; do
  if curl -sf http://127.0.0.1:8000/healthz >/dev/null; then
    echo "OK: /healthz responded."
    exit 0
  fi
  sleep 2
done

echo "WARN: /healthz did not respond within 20s. Recent logs:" >&2
journalctl -u ocrapi.service -n 80 --no-pager >&2
exit 1
