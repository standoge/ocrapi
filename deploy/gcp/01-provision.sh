#!/usr/bin/env bash
# Provisions all GCP infrastructure for ocrapi on a single GCE VM.
# Run from any machine with `gcloud` authenticated and the target project set.
#
#   gcloud config set project YOUR_PROJECT_ID
#   bash 01-provision.sh
#
# Idempotent: re-running skips resources that already exist.

set -euo pipefail

############################
# Configuration (edit me)  #
############################
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
VM_NAME="${VM_NAME:-ocrapi-vm}"
MACHINE_TYPE="${MACHINE_TYPE:-c4-standard-16}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-50GB}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-hyperdisk-balanced}"
DATA_DISK_NAME="${DATA_DISK_NAME:-ocrapi-data}"
DATA_DISK_SIZE="${DATA_DISK_SIZE:-500GB}"
DATA_DISK_TYPE="${DATA_DISK_TYPE:-hyperdisk-balanced}"
SA_NAME="${SA_NAME:-ocrapi-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="${AR_REPO:-ocrapi}"
NETWORK_TAG="${NETWORK_TAG:-ocrapi}"
ALLOWED_SSH_CIDR="${ALLOWED_SSH_CIDR:-0.0.0.0/0}" # tighten this to your office IP

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: PROJECT_ID is empty. Run 'gcloud config set project <id>' first." >&2
  exit 1
fi

echo "==> Project: ${PROJECT_ID}"
echo "==> Region/Zone: ${REGION} / ${ZONE}"

############################
# 1. Enable required APIs  #
############################
echo "==> Enabling required APIs..."
gcloud services enable \
  compute.googleapis.com \
  documentai.googleapis.com \
  drive.googleapis.com \
  artifactregistry.googleapis.com \
  iam.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  --project="${PROJECT_ID}"

############################
# 2. Service account       #
############################
echo "==> Ensuring service account ${SA_EMAIL}..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="ocrapi runtime" \
    --project="${PROJECT_ID}"
fi

echo "==> Granting roles to ${SA_EMAIL}..."
# Document AI calls
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/documentai.apiUser" \
  --condition=None >/dev/null
# Pull image from Artifact Registry
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/artifactregistry.reader" \
  --condition=None >/dev/null
# Write app logs to Cloud Logging
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/logging.logWriter" \
  --condition=None >/dev/null
# Write metrics
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/monitoring.metricWriter" \
  --condition=None >/dev/null

cat <<EOF
==> Drive access is NOT granted via IAM. Share the Shared Drive folder
    (DRIVE_SHARED_FOLDER_ID) with this service account as Content Manager:
        ${SA_EMAIL}
EOF

############################
# 3. Artifact Registry     #
############################
echo "==> Ensuring Artifact Registry repo '${AR_REPO}' in ${REGION}..."
if ! gcloud artifacts repositories describe "${AR_REPO}" \
      --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="ocrapi container images" \
    --project="${PROJECT_ID}"
fi

############################
# 4. Firewall              #
############################
echo "==> Configuring firewall..."
# Plain HTTP: the app is served directly on port 8000 (no TLS/reverse proxy).
# WARNING: traffic (including PDF uploads) is unencrypted. Restrict
# APP_SOURCE_CIDR to your own IP for anything beyond throwaway testing.
APP_SOURCE_CIDR="${APP_SOURCE_CIDR:-0.0.0.0/0}"
if ! gcloud compute firewall-rules describe ocrapi-allow-http \
      --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create ocrapi-allow-http \
    --network=default \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:8000 \
    --source-ranges="${APP_SOURCE_CIDR}" \
    --target-tags="${NETWORK_TAG}" \
    --project="${PROJECT_ID}"
fi

if ! gcloud compute firewall-rules describe ocrapi-allow-ssh \
      --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create ocrapi-allow-ssh \
    --network=default \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges="${ALLOWED_SSH_CIDR}" \
    --target-tags="${NETWORK_TAG}" \
    --project="${PROJECT_ID}"
fi

############################
# 5. Data disk             #
############################
echo "==> Ensuring data disk '${DATA_DISK_NAME}' (${DATA_DISK_SIZE} ${DATA_DISK_TYPE})..."
if ! gcloud compute disks describe "${DATA_DISK_NAME}" \
      --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute disks create "${DATA_DISK_NAME}" \
    --zone="${ZONE}" \
    --size="${DATA_DISK_SIZE}" \
    --type="${DATA_DISK_TYPE}" \
    --project="${PROJECT_ID}"
fi

############################
# 6. VM                    #
############################
echo "==> Ensuring VM '${VM_NAME}' (${MACHINE_TYPE})..."
if ! gcloud compute instances describe "${VM_NAME}" \
      --zone="${ZONE}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute instances create "${VM_NAME}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size="${BOOT_DISK_SIZE}" \
    --boot-disk-type="${BOOT_DISK_TYPE}" \
    --service-account="${SA_EMAIL}" \
    --scopes=cloud-platform \
    --tags="${NETWORK_TAG}" \
    --metadata=enable-oslogin=TRUE \
    --project="${PROJECT_ID}"
fi

echo "==> Attaching data disk to VM (if not already)..."
ATTACHED=$(gcloud compute instances describe "${VM_NAME}" \
  --zone="${ZONE}" --project="${PROJECT_ID}" \
  --format="value(disks[].source)" | tr ';' '\n' | grep -c "${DATA_DISK_NAME}" || true)
if [[ "${ATTACHED}" -eq 0 ]]; then
  gcloud compute instances attach-disk "${VM_NAME}" \
    --disk="${DATA_DISK_NAME}" \
    --zone="${ZONE}" \
    --device-name="${DATA_DISK_NAME}" \
    --project="${PROJECT_ID}"
fi

EXT_IP=$(gcloud compute instances describe "${VM_NAME}" \
  --zone="${ZONE}" --project="${PROJECT_ID}" \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)")

cat <<EOF

============================================================
Provisioning complete.

  VM:              ${VM_NAME} (${MACHINE_TYPE}) in ${ZONE}
  External IP:     ${EXT_IP}
  Service account: ${SA_EMAIL}
  Data disk:       ${DATA_DISK_NAME} (${DATA_DISK_SIZE})
  Artifact Repo:   ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}

Next steps:
  1) Share the Drive folder with ${SA_EMAIL} (Content Manager).
  2) SSH into the VM:
        gcloud compute ssh ${VM_NAME} --zone=${ZONE} --project=${PROJECT_ID}
  3) On the VM, run:  deploy/gcp/02-vm-setup.sh
  4) From your dev machine, run:  deploy/gcp/03-build-and-push.sh
  5) On the VM, run:  deploy/gcp/04-run.sh

Once running, the API is served over plain HTTP at:
        http://${EXT_IP}:8000   (Swagger UI: http://${EXT_IP}:8000/docs)
NOTE: traffic is unencrypted. Set APP_SOURCE_CIDR to your own IP before
re-running to restrict who can reach port 8000.
============================================================
EOF
