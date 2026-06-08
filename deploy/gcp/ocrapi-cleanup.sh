#!/usr/bin/env bash
# Prunes ocrapi job folders older than RETENTION_DAYS and ocrmypdf scratch.
# Triggered by ocrapi-cleanup.timer (daily). Safe to run by hand.

set -euo pipefail

JOBS_DIR="${JOBS_DIR:-/data/jobs}"
TMP_DIR="${TMP_DIR:-/data/tmp}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"
# Stale ocrmypdf scratch dirs (older than this many hours) likely belong to
# crashed runs and are safe to delete; live OCR jobs touch them constantly.
TMP_STALE_HOURS="${TMP_STALE_HOURS:-12}"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

if [[ -d "${JOBS_DIR}" ]]; then
  log "Pruning ${JOBS_DIR} entries older than ${RETENTION_DAYS} days..."
  BEFORE=$(df -BG --output=avail "${JOBS_DIR}" | tail -1 | tr -dc '0-9')
  find "${JOBS_DIR}" -mindepth 1 -maxdepth 1 -type d -mtime "+${RETENTION_DAYS}" \
    -print -exec rm -rf {} +
  AFTER=$(df -BG --output=avail "${JOBS_DIR}" | tail -1 | tr -dc '0-9')
  log "Freed approximately $((AFTER - BEFORE)) GiB in ${JOBS_DIR}."
else
  log "Skipping: ${JOBS_DIR} does not exist."
fi

if [[ -d "${TMP_DIR}" ]]; then
  log "Pruning stale ocrmypdf scratch in ${TMP_DIR} older than ${TMP_STALE_HOURS}h..."
  TMP_MMIN=$(( TMP_STALE_HOURS * 60 ))
  find "${TMP_DIR}" -mindepth 1 -maxdepth 1 \
    \( -name 'ocrmypdf.io*' -o -name 'com.github.ocrmypdf*' -o -name 'ocrapi_*' \) \
    -mmin "+${TMP_MMIN}" -print -exec rm -rf {} +
fi

log "Cleanup done."
