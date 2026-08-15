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
MACHINE_TYPE="${MACHINE_TYPE:-c4-standard-4}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-50GB}"
BOOT_DISK_TYPE="${BOOT_DISK_TYPE:-hyperdisk-balanced}"
DATA_DISK_NAME="${DATA_DISK_NAME:-ocrapi-data}"
DATA_DISK_SIZE="${DATA_DISK_SIZE:-50GB}"
BUCKET="${BUCKET:-${PROJECT_ID}-ocrapi-batch}"
DATA_DISK_TYPE="${DATA_DISK_TYPE:-hyperdisk-balanced}"
SA_NAME="${SA_NAME:-ocrapi-sa}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
AR_REPO="${AR_REPO:-ocrapi}"
NETWORK_TAG="${NETWORK_TAG:-ocrapi}"
ALLOWED_SSH_CIDR="${ALLOWED_SSH_CIDR:-0.0.0.0/0}" # tighten this to your office IP
# Document AI OCR processor (created automatically; override GCP_PROCESSOR_ID
# to reuse an existing processor instead).
DOCAI_LOCATION="${DOCAI_LOCATION:-us}"
DOCAI_PROCESSOR_DISPLAY_NAME="${DOCAI_PROCESSOR_DISPLAY_NAME:-ocrapi-ocr}"
GCP_PROCESSOR_ID="${GCP_PROCESSOR_ID:-}"
# Static external IP keeps the TLS cert's IP SAN valid across VM stop/start.
STATIC_IP_NAME="${STATIC_IP_NAME:-ocrapi-ip}"
# The GCS bucket only backs the legacy Document AI *batch* path, which the
# current pipeline never calls. Off by default; set ENABLE_BATCH_BUCKET=true
# to provision it.
ENABLE_BATCH_BUCKET="${ENABLE_BATCH_BUCKET:-false}"
# Optional: default Shared Drive folder baked into the generated runtime env.
DRIVE_SHARED_FOLDER_ID="${DRIVE_SHARED_FOLDER_ID:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
  storage.googleapis.com \
  --project="${PROJECT_ID}"

############################
# 1b. Document AI processor#
############################
# There is no gcloud surface for Document AI processors, so use the REST API.
# Idempotent: reuses the processor whose displayName matches, or the explicit
# GCP_PROCESSOR_ID override.
if [[ -z "${GCP_PROCESSOR_ID}" ]]; then
  echo "==> Ensuring Document AI OCR processor '${DOCAI_PROCESSOR_DISPLAY_NAME}' in '${DOCAI_LOCATION}'..."
  DOCAI_API="https://${DOCAI_LOCATION}-documentai.googleapis.com/v1"
  DOCAI_PARENT="projects/${PROJECT_ID}/locations/${DOCAI_LOCATION}"
  ACCESS_TOKEN="$(gcloud auth print-access-token)"

  GCP_PROCESSOR_ID="$(curl -sf \
      -H "Authorization: Bearer ${ACCESS_TOKEN}" \
      "${DOCAI_API}/${DOCAI_PARENT}/processors?pageSize=100" \
    | DISPLAY_NAME="${DOCAI_PROCESSOR_DISPLAY_NAME}" python3 -c '
import json, os, sys
data = json.load(sys.stdin)
want = os.environ["DISPLAY_NAME"]
for p in data.get("processors", []):
    if p.get("displayName") == want and p.get("type") == "OCR_PROCESSOR":
        print(p["name"].rsplit("/", 1)[-1])
        break
')"

  if [[ -z "${GCP_PROCESSOR_ID}" ]]; then
    echo "==> Creating Document OCR processor..."
    GCP_PROCESSOR_ID="$(curl -sf -X POST \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"displayName\": \"${DOCAI_PROCESSOR_DISPLAY_NAME}\", \"type\": \"OCR_PROCESSOR\"}" \
        "${DOCAI_API}/${DOCAI_PARENT}/processors" \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"].rsplit("/", 1)[-1])')"
  else
    echo "==> Reusing existing processor."
  fi

  if [[ -z "${GCP_PROCESSOR_ID}" ]]; then
    echo "ERROR: could not find or create the Document AI processor." >&2
    exit 1
  fi
fi
echo "==> Document AI processor ID: ${GCP_PROCESSOR_ID}"

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

############################
# 3. GCS batch bucket      #
############################
# Only needed for the legacy Document AI batch path (unused by the current
# pipeline). Skipped unless ENABLE_BATCH_BUCKET=true.
if [[ "${ENABLE_BATCH_BUCKET}" == "true" ]]; then
  echo "==> Ensuring GCS bucket gs://${BUCKET} in ${REGION}..."
  if ! gcloud storage buckets describe "gs://${BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud storage buckets create "gs://${BUCKET}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --uniform-bucket-level-access
  fi

  LIFECYCLE_FILE="$(mktemp)"
  trap 'rm -f "${LIFECYCLE_FILE}"' EXIT
  cat > "${LIFECYCLE_FILE}" <<'JSON'
{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}
JSON
  echo "==> Applying 1-day lifecycle rule to gs://${BUCKET}..."
  gcloud storage buckets update "gs://${BUCKET}" \
    --lifecycle-file="${LIFECYCLE_FILE}" \
    --project="${PROJECT_ID}"

  echo "==> Granting storage.objectAdmin on gs://${BUCKET} to ${SA_EMAIL}..."
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/storage.objectAdmin" \
    --project="${PROJECT_ID}" >/dev/null
else
  echo "==> Skipping GCS batch bucket (ENABLE_BATCH_BUCKET=false; only the legacy batch path needs it)."
  BUCKET=""
fi

cat <<EOF
==> Drive access is NOT granted via IAM. Share the Shared Drive folder
    (DRIVE_SHARED_FOLDER_ID) with this service account as Content Manager:
        ${SA_EMAIL}
EOF

############################
# 4. Artifact Registry     #
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
# 5. Firewall              #
############################
echo "==> Configuring firewall..."
# The app terminates TLS in-process (uvicorn --ssl-*) and is served on port 8000
# over HTTPS. Traffic (including PDF uploads) is encrypted in transit. Still
# restrict APP_SOURCE_CIDR to your intranet/office range for defense in depth.
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
# 6. Data disk             #
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
# 6b. Static external IP   #
############################
# A reserved IP keeps the self-signed cert's IP SAN valid across VM
# stop/start (the default ephemeral IP would drift and break --cacert).
echo "==> Ensuring static external IP '${STATIC_IP_NAME}' in ${REGION}..."
if ! gcloud compute addresses describe "${STATIC_IP_NAME}" \
      --region="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud compute addresses create "${STATIC_IP_NAME}" \
    --region="${REGION}" \
    --project="${PROJECT_ID}"
fi
STATIC_IP_ADDR="$(gcloud compute addresses describe "${STATIC_IP_NAME}" \
  --region="${REGION}" --project="${PROJECT_ID}" --format='value(address)')"
echo "==> Static IP: ${STATIC_IP_ADDR}"

############################
# 7. VM                    #
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
    --scopes=cloud-platform,https://www.googleapis.com/auth/drive \
    --tags="${NETWORK_TAG}" \
    --address="${STATIC_IP_ADDR}" \
    --metadata=enable-oslogin=TRUE \
    --project="${PROJECT_ID}"
else
  CURRENT_IP="$(gcloud compute instances describe "${VM_NAME}" \
    --zone="${ZONE}" --project="${PROJECT_ID}" \
    --format="get(networkInterfaces[0].accessConfigs[0].natIP)")"
  if [[ "${CURRENT_IP}" != "${STATIC_IP_ADDR}" ]]; then
    echo "NOTE: VM already exists with IP ${CURRENT_IP} (not the reserved ${STATIC_IP_ADDR})."
    echo "      To switch, delete/recreate the access config or the VM; the cert SAN must match."
  fi
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

############################
# 8. Runtime env metadata  #
############################
# Publish a fully filled ocrapi.env to VM instance metadata so 02-vm-setup.sh
# can install /etc/ocrapi.env without any hand edits. Regenerated (and
# overwritten in metadata) on every run; an existing /etc/ocrapi.env on the
# VM is never clobbered.
echo "==> Publishing runtime env to VM metadata (key: ocrapi-env)..."
ENV_FILE="$(mktemp)"
trap 'rm -f "${ENV_FILE}" ${LIFECYCLE_FILE:+"${LIFECYCLE_FILE}"}' EXIT
sed \
  -e "s|^GCP_PROJECT_ID=.*|GCP_PROJECT_ID=${PROJECT_ID}|" \
  -e "s|^GCP_LOCATION=.*|GCP_LOCATION=${DOCAI_LOCATION}|" \
  -e "s|^GCP_PROCESSOR_ID=.*|GCP_PROCESSOR_ID=${GCP_PROCESSOR_ID}|" \
  -e "s|^GCS_BUCKET=.*|GCS_BUCKET=${BUCKET}|" \
  -e "s|^DRIVE_SHARED_FOLDER_ID=.*|DRIVE_SHARED_FOLDER_ID=${DRIVE_SHARED_FOLDER_ID}|" \
  "${SCRIPT_DIR}/ocrapi.env.example" > "${ENV_FILE}"
gcloud compute instances add-metadata "${VM_NAME}" \
  --zone="${ZONE}" --project="${PROJECT_ID}" \
  --metadata-from-file "ocrapi-env=${ENV_FILE}" >/dev/null

EXT_IP=$(gcloud compute instances describe "${VM_NAME}" \
  --zone="${ZONE}" --project="${PROJECT_ID}" \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)")

cat <<EOF

============================================================
Provisioning complete.

  VM:              ${VM_NAME} (${MACHINE_TYPE}) in ${ZONE}
  External IP:     ${EXT_IP} (reserved: ${STATIC_IP_ADDR})
  Service account: ${SA_EMAIL}
  Data disk:       ${DATA_DISK_NAME} (${DATA_DISK_SIZE})
  DocAI processor: ${GCP_PROCESSOR_ID} (${DOCAI_LOCATION})
  Batch bucket:    ${BUCKET:+gs://}${BUCKET:-skipped (ENABLE_BATCH_BUCKET=false)}
  Artifact Repo:   ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}
  Runtime env:     published to VM metadata (installed by 02-vm-setup.sh)

Next steps:
  1) (Optional, for Drive upload) Share the Shared Drive folder with
     ${SA_EMAIL} (Content Manager). To bake the folder ID into the
     generated env, re-run with DRIVE_SHARED_FOLDER_ID=<id>.
  2) SSH into the VM:
        gcloud compute ssh ${VM_NAME} --zone=${ZONE} --project=${PROJECT_ID}
  3) On the VM, run:  deploy/gcp/02-vm-setup.sh
  4) From your dev machine, run:  deploy/gcp/03-build-and-push.sh
  5) On the VM, run:  deploy/gcp/04-run.sh

Once running, the API is served over HTTPS (TLS in uvicorn) at:
        https://${EXT_IP}:8000   (Swagger UI: https://${EXT_IP}:8000/docs)
The cert is self-signed (generated by 02-vm-setup.sh at /data/tls). Clients
should trust /data/tls/cert.pem (--cacert) or pass 'curl -k'.
TIP: the external IP is ephemeral — reserve a static IP so the cert SAN keeps
matching across VM stop/start (otherwise regenerate the cert). Set
APP_SOURCE_CIDR to your intranet range to restrict who can reach port 8000.
============================================================
EOF
