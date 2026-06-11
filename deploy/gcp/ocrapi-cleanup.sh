#!/usr/bin/env bash
# Prunes ocrapi job folders older than RETENTION_DAYS.
# Triggered by ocrapi-cleanup.timer (daily). Safe to run by hand.

set -euo pipefail

JOBS_DIR="${JOBS_DIR:-/data/jobs}"
RETENTION_DAYS="${RETENTION_DAYS:-7}"

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

log "Cleanup done."
