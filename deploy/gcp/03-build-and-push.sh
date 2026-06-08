#!/usr/bin/env bash
# Builds the ocrapi container image and pushes it to Artifact Registry.
# Run from the REPO ROOT on your dev machine (Docker required).
#
#   bash deploy/gcp/03-build-and-push.sh
#
# Outputs the fully qualified image tag; feed it to 04-run.sh on the VM.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
AR_REPO="${AR_REPO:-ocrapi}"
IMAGE_NAME="${IMAGE_NAME:-ocrapi}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is empty. Run 'gcloud config set project <id>' first." >&2
  exit 1
fi

# Run from repo root regardless of cwd.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# Tag with date + short git SHA when available; falls back to date+random.
DATE_TAG="$(date -u +%Y-%m-%d)"
if git rev-parse --short HEAD >/dev/null 2>&1; then
  SHA_TAG="$(git rev-parse --short HEAD)"
else
  SHA_TAG="$(date -u +%H%M%S)"
fi
TAG="${DATE_TAG}-${SHA_TAG}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
FULL_TAG="${REGISTRY}/${IMAGE_NAME}:${TAG}"
LATEST_TAG="${REGISTRY}/${IMAGE_NAME}:latest"

echo "==> Authenticating Docker with Artifact Registry..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

echo "==> Building ${FULL_TAG} (and :latest) for linux/amd64..."
# c4 VMs are amd64; explicit --platform avoids surprises when building on arm64 (Apple Silicon).
docker buildx build \
  --platform=linux/amd64 \
  -t "${FULL_TAG}" \
  -t "${LATEST_TAG}" \
  --push \
  .

cat <<EOF

============================================================
Build & push complete.

  Image: ${FULL_TAG}
  Also tagged as: ${LATEST_TAG}

On the VM, write the image tag and (re)start the service:

  sudo bash -c 'echo "IMAGE=${FULL_TAG}" > /etc/ocrapi.image'
  sudo systemctl restart ocrapi.service
  sudo systemctl status ocrapi.service --no-pager
============================================================
EOF
