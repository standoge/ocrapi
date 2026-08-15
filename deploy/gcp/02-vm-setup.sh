#!/usr/bin/env bash
# Runs ON the GCE VM (Ubuntu 24.04). Idempotent.
# Mounts the attached data disk at /data, installs Docker, configures Artifact
# Registry auth, and installs the ocrapi systemd unit + cleanup timer.
#
# Usage:
#   gcloud compute ssh ocrapi-vm --zone=us-central1-a
#   sudo bash 02-vm-setup.sh
#
# Optionally override the data disk device name (default matches what
# 01-provision.sh attaches):
#   sudo DATA_DEVICE_NAME=ocrapi-data bash 02-vm-setup.sh

set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

DATA_DEVICE_NAME="${DATA_DEVICE_NAME:-ocrapi-data}"
MOUNT_POINT="${MOUNT_POINT:-/data}"
REGION="${REGION:-us-central1}"

############################
# 1. Format + mount disk   #
############################
echo "==> Discovering data disk device..."
# GCE attaches with stable name under /dev/disk/by-id/google-${DEVICE_NAME}
DEV_LINK="/dev/disk/by-id/google-${DATA_DEVICE_NAME}"
if [[ ! -e "${DEV_LINK}" ]]; then
  echo "ERROR: ${DEV_LINK} not found. Is the disk attached?" >&2
  ls -l /dev/disk/by-id/ >&2 || true
  exit 1
fi
DEV_PATH="$(readlink -f "${DEV_LINK}")"
echo "==> Data disk: ${DEV_LINK} -> ${DEV_PATH}"

FSTYPE="$(blkid -o value -s TYPE "${DEV_PATH}" || true)"
if [[ -z "${FSTYPE}" ]]; then
  # blkid exits non-zero both for "no filesystem" and for real errors; a
  # transient failure here must never wipe a formatted disk, so double-check.
  if [[ -n "$(lsblk -no FSTYPE "${DEV_PATH}" | tr -d '[:space:]')" ]]; then
    echo "ERROR: blkid saw no filesystem on ${DEV_PATH} but lsblk disagrees; refusing to mkfs." >&2
    exit 1
  fi
  echo "==> Formatting ${DEV_PATH} as ext4..."
  mkfs.ext4 -F -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "${DEV_PATH}"
else
  echo "==> Disk already formatted (${FSTYPE}); skipping mkfs."
fi

mkdir -p "${MOUNT_POINT}"
UUID="$(blkid -o value -s UUID "${DEV_PATH}")"
FSTAB_LINE="UUID=${UUID} ${MOUNT_POINT} ext4 discard,defaults,nofail 0 2"
# Guard on the MOUNT POINT, not the UUID: if the disk was ever reformatted its
# UUID changed, and guarding on the UUID would append a second ${MOUNT_POINT}
# line while leaving the stale one — systemd's fstab generator only honors the
# first, so the mount would fail at boot with "Dependency failed". Stale
# entries are removed even when the correct line is already present, since it
# may sit BELOW the stale one.
FSTAB_CHANGED=0
STALE="$(awk -v mp="${MOUNT_POINT}" -v u="UUID=${UUID}" \
  '$1 !~ /^#/ && $2 == mp && $1 != u' /etc/fstab || true)"
if [[ -n "${STALE}" ]]; then
  echo "==> Removing stale ${MOUNT_POINT} entries from /etc/fstab:"
  echo "${STALE}"
  awk -v mp="${MOUNT_POINT}" -v u="UUID=${UUID}" \
    '$1 ~ /^#/ || $2 != mp || $1 == u' /etc/fstab > /etc/fstab.new
  cat /etc/fstab.new > /etc/fstab
  rm -f /etc/fstab.new
  FSTAB_CHANGED=1
fi
if ! grep -qE "^UUID=${UUID}[[:space:]]+${MOUNT_POINT}[[:space:]]" /etc/fstab; then
  echo "${FSTAB_LINE}" >> /etc/fstab
  FSTAB_CHANGED=1
fi
if [[ "${FSTAB_CHANGED}" -eq 1 ]]; then
  systemctl daemon-reload
  echo "==> /etc/fstab updated."
fi

if ! mountpoint -q "${MOUNT_POINT}"; then
  # If the mount previously failed (e.g. stale fstab UUID), the app may have
  # been writing into ${MOUNT_POINT} on the BOOT disk. Mounting now would
  # shadow that data (live TLS cert + job dirs). Refuse and point at the guide.
  if [[ -n "$(find "${MOUNT_POINT}" -mindepth 1 -maxdepth 1 ! -name 'lost+found' -print -quit 2>/dev/null)" ]]; then
    echo "ERROR: ${MOUNT_POINT} contains data but is not a mount point." >&2
    echo "Mounting now would shadow it. Migrate it to the data disk first:" >&2
    echo "see FIX-DATA-DISK.md at the repo root." >&2
    exit 1
  fi
  mount "${MOUNT_POINT}"
fi

mkdir -p "${MOUNT_POINT}/jobs"
echo "==> ${MOUNT_POINT} ready: $(df -h "${MOUNT_POINT}" | tail -1)"

############################
# 1b. TLS certificate      #
############################
# uvicorn terminates TLS in-process (see ocrapi.service). A long-lived
# self-signed cert lives on the data disk so it survives container restarts and
# image redeploys — clients keep trusting the same cert. Idempotent.
TLS_DIR="${MOUNT_POINT}/tls"
mkdir -p "${TLS_DIR}"
if [[ ! -f "${TLS_DIR}/cert.pem" ]]; then
  echo "==> Generating self-signed TLS certificate..."
  META="http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0"
  EXT_IP="$(curl -s -H 'Metadata-Flavor: Google' "${META}/access-configs/0/external-ip" || true)"
  INT_IP="$(curl -s -H 'Metadata-Flavor: Google' "${META}/ip" || true)"
  SAN="DNS:localhost,IP:127.0.0.1"
  [[ -n "${INT_IP}" ]] && SAN="${SAN},IP:${INT_IP}"
  [[ -n "${EXT_IP}" ]] && SAN="${SAN},IP:${EXT_IP}"
  # Add an extra name (DNS alias or reserved IP) via TLS_EXTRA_SAN, e.g.
  #   sudo TLS_EXTRA_SAN="DNS:ocrapi.intranet.local" bash 02-vm-setup.sh
  [[ -n "${TLS_EXTRA_SAN:-}" ]] && SAN="${SAN},${TLS_EXTRA_SAN}"
  openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "${TLS_DIR}/key.pem" -out "${TLS_DIR}/cert.pem" \
    -days 3650 -subj "/CN=ocrapi" -addext "subjectAltName=${SAN}"
  chmod 600 "${TLS_DIR}/key.pem"
  chmod 644 "${TLS_DIR}/cert.pem"
  echo "==> TLS cert generated (SAN: ${SAN})"
else
  echo "==> TLS cert already present at ${TLS_DIR}; skipping."
fi

############################
# 2. Install Docker        #
############################
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installing Docker..."
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  systemctl enable --now docker
else
  echo "==> Docker already installed."
fi

############################
# 3. Artifact Registry auth#
############################
echo "==> Configuring Docker to use gcloud Artifact Registry credentials..."
# The VM's attached service account has artifactregistry.reader; the credential
# helper exchanges the metadata-server token for a Docker auth token.
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

############################
# 4. Install systemd units #
############################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f /etc/ocrapi.env ]]; then
  echo "==> Installing /etc/ocrapi.env from template (REVIEW AND EDIT)..."
  install -m 600 "${SCRIPT_DIR}/ocrapi.env.example" /etc/ocrapi.env
fi

echo "==> Installing ocrapi systemd unit..."
install -m 644 "${SCRIPT_DIR}/ocrapi.service" /etc/systemd/system/ocrapi.service

echo "==> Installing job-folder cleanup script + timer..."
install -m 755 "${SCRIPT_DIR}/ocrapi-cleanup.sh"     /usr/local/bin/ocrapi-cleanup.sh
install -m 644 "${SCRIPT_DIR}/ocrapi-cleanup.service" /etc/systemd/system/ocrapi-cleanup.service
install -m 644 "${SCRIPT_DIR}/ocrapi-cleanup.timer"   /etc/systemd/system/ocrapi-cleanup.timer

systemctl daemon-reload
systemctl enable ocrapi-cleanup.timer
systemctl start ocrapi-cleanup.timer

cat <<EOF

============================================================
VM setup complete.

Mounted:        ${MOUNT_POINT} ($(df -h --output=size "${MOUNT_POINT}" | tail -1 | xargs))
Docker:         $(docker --version)
Systemd units:  ocrapi.service, ocrapi-cleanup.timer
TLS cert:       ${TLS_DIR}/cert.pem (self-signed; copy to clients or use curl -k)

Next steps:
  1) Edit /etc/ocrapi.env if needed (GCP_PROJECT_ID, processor, Drive ID).
  2) From your dev machine: bash deploy/gcp/03-build-and-push.sh
  3) On this VM:           sudo bash deploy/gcp/04-run.sh
============================================================
EOF
