# Deploying ocrapi to a single GCE VM

This folder contains everything needed to run ocrapi on a Compute Engine VM
with Docker, a data disk for `JOBS_DIR`, and a GCS bucket (legacy batch
scratch, unused by the current job pipeline). Async jobs split PDFs into
small chunks, run concurrent online `processDocument` calls, and the VM
assembles searchable PDFs with PyMuPDF — no Ghostscript rasterization.

The app is served directly over **plain HTTP on port 8000** (no TLS / reverse
proxy). This is the simplest setup for testing. Note that uploads and results
travel unencrypted; if you later want HTTPS, put a reverse proxy (Caddy,
nginx, or a GCP HTTPS Load Balancer) in front and bind the container to
`127.0.0.1:8000` in [`ocrapi.service`](./ocrapi.service).

## Files

| File                                                 | Where it runs    | Purpose                                                            |
| ---------------------------------------------------- | ---------------- | ------------------------------------------------------------------ |
| [`01-provision.sh`](./01-provision.sh)               | dev machine      | Create SA, GCS bucket, Artifact Registry, firewall, data disk, VM (SA + Drive OAuth scopes). |
| [`02-vm-setup.sh`](./02-vm-setup.sh)                 | VM (root)        | Format/mount data disk, install Docker, register systemd units.    |
| [`03-build-and-push.sh`](./03-build-and-push.sh)     | dev machine      | Build linux/amd64 image, push to Artifact Registry.                |
| [`04-run.sh`](./04-run.sh)                           | VM (root)        | Pin image tag, (re)start `ocrapi.service`, health-check.           |
| [`ocrapi.env.example`](./ocrapi.env.example)         | VM (`/etc/ocrapi.env`) | Production env: GCS bucket, concurrency, paths, no key file.   |
| [`ocrapi.service`](./ocrapi.service)                 | VM systemd unit  | Runs the container on `0.0.0.0:8000` with `/data/jobs` bind mount. |
| [`ocrapi-cleanup.sh`](./ocrapi-cleanup.sh)           | VM (`/usr/local/bin`) | Prunes old job folders.                                         |
| [`ocrapi-cleanup.service`](./ocrapi-cleanup.service) | VM systemd unit  | Oneshot wrapper around the cleanup script.                         |
| [`ocrapi-cleanup.timer`](./ocrapi-cleanup.timer)     | VM systemd timer | Runs the cleanup daily + 10 min after boot.                        |
| [`QUOTA.md`](./QUOTA.md)                             | runbook          | How to verify and raise Document AI online OCR quota.              |

## End-to-end deployment

Replace `YOUR_PROJECT` with your GCP project ID (e.g. `anda-dev-457721`).

```bash
# --- on your dev machine ---
gcloud config set project YOUR_PROJECT
# Optional but recommended: lock the app port to your own IP instead of the world.
# APP_SOURCE_CIDR="203.0.113.4/32" bash deploy/gcp/01-provision.sh
bash deploy/gcp/01-provision.sh                  # ~2 min

# Share the Drive folder with the service account printed above
# (Content Manager on the Shared Drive folder).
# See "Google Drive on GCE" below if uploads fail with 403 scope or 404 folder errors.

# --- on the VM (SSH in) ---
gcloud compute ssh ocrapi-vm --zone=us-central1-a
# Copy the deploy/ folder onto the VM (e.g. git clone the repo) and:
sudo bash deploy/gcp/02-vm-setup.sh              # mounts /data, installs docker

# Edit /etc/ocrapi.env if you need to change GCP_PROJECT_ID / processor / GCS bucket / Drive ID
sudoedit /etc/ocrapi.env

# --- back on dev machine ---
bash deploy/gcp/03-build-and-push.sh             # builds + pushes to Artifact Registry
# Copy the IMAGE=... line it prints.

# --- on the VM ---
sudo IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/ocrapi/ocrapi:TAG \
     bash deploy/gcp/04-run.sh                   # starts ocrapi.service, hits /healthz
```

The API is now served over plain HTTP on the VM's external IP. From your
machine (substitute the external IP printed by `01-provision.sh`):

```bash
curl -sf http://EXTERNAL_IP:8000/healthz
# Swagger UI: http://EXTERNAL_IP:8000/docs
```

## Day-2 operations

```bash
# Logs
sudo journalctl -u ocrapi.service -f

# Disk usage on the data volume
df -h /data
du -sh /data/jobs

# Manual cleanup (defaults to 7 days)
sudo RETENTION_DAYS=3 /usr/local/bin/ocrapi-cleanup.sh

# Roll a new build
bash deploy/gcp/03-build-and-push.sh
# then on the VM:
sudo IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/ocrapi/ocrapi:NEW_TAG \
     bash deploy/gcp/04-run.sh
```

## Architecture notes

- **Async jobs** (`POST /v1/jobs`): split PDF into chunks → concurrent online
  `processDocument` per chunk → PyMuPDF invisible text layer → local
  `output.pdf` → optional upload to Shared Drive.
- **Sync OCR** (`POST /v1/ocr`): online `processDocument` on the whole PDF (small
  docs only) → same PyMuPDF injector.
- **GCS bucket** is provisioned but unused by the current async pipeline.
- Default VM sizing is `c4-standard-4` with a 50 GB data disk (input/output PDFs
  only; no page rasterization scratch).

## Google Drive on GCE

Drive uploads from the VM use **Application Default Credentials** from the attached
service account (`ocrapi-sa@PROJECT.iam.gserviceaccount.com`). There is no JSON key
in the container (`GOOGLE_APPLICATION_CREDENTIALS` is intentionally unset in
[`ocrapi.env.example`](./ocrapi.env.example)).

### Service account: IAM vs Drive folder

| Requirement | How |
|-------------|-----|
| Document AI | IAM role `roles/documentai.apiUser` on the project (granted by `01-provision.sh`) |
| GCS batch bucket | IAM `roles/storage.objectAdmin` on `gs://PROJECT-ocrapi-batch` |
| Artifact Registry / logging | `roles/artifactregistry.reader`, `roles/logging.logWriter`, etc. |
| **Drive file upload** | **Not** an IAM role — share the Shared Drive folder with `ocrapi-sa@...` as **Content Manager** |
| Drive API | Enable `drive.googleapis.com` on the project (`01-provision.sh` enables it) |

Set `DRIVE_SHARED_FOLDER_ID` in `/etc/ocrapi.env` to the folder ID from the Shared
Drive URL. Use the **full** ID (typically 33 characters). A truncated ID (e.g. missing
a leading character) causes `404 File not found` on upload.

### VM OAuth access scopes (required for Drive)

The VM must be allowed to obtain OAuth tokens that include the **Drive** scope.
[`01-provision.sh`](./01-provision.sh) attaches the service account with:

```text
cloud-platform,https://www.googleapis.com/auth/drive
```

- `cloud-platform` — Document AI, GCS, Logging, etc.
- `https://www.googleapis.com/auth/drive` — Google Drive API (upload to Shared Drive)

If the VM was created with only `cloud-platform`, uploads fail with:

```text
403 Request had insufficient authentication scopes
```

**Fix an existing VM** (brief downtime — stop the VM first):

```bash
gcloud compute instances stop ocrapi-vm --zone=us-central1-a --project=YOUR_PROJECT

gcloud compute instances set-service-account ocrapi-vm \
  --zone=us-central1-a --project=YOUR_PROJECT \
  --service-account=ocrapi-sa@YOUR_PROJECT.iam.gserviceaccount.com \
  --scopes=cloud-platform,https://www.googleapis.com/auth/drive

gcloud compute instances start ocrapi-vm --zone=us-central1-a --project=YOUR_PROJECT
```

Verify scopes:

```bash
gcloud compute instances describe ocrapi-vm \
  --zone=us-central1-a --project=YOUR_PROJECT \
  --format="json(serviceAccounts)"
```

### Upload workflow (application)

The app uploads searchable PDFs with the Drive v3 **resumable REST API** via
`requests` and `AuthorizedSession` (see `app/services/drive_client.py`):

1. `POST` resumable session init (`uploadType=resumable`, `supportsAllDrives=true`)
2. `PUT` PDF bytes to the session `Location` URL
3. Fresh HTTP session per upload so multiple jobs can upload in parallel when OCR
   finishes at the same time (`OCR_WORKER_CONCURRENCY` workers)

Async jobs: pass `folder_id` on `POST /v1/jobs`, or retry later with
`POST /v1/jobs/{jobId}/drive`. If upload fails after OCR succeeds, check
`driveUploadError` on the job status; the PDF is still available at
`/v1/jobs/{jobId}/result`.

Example batch (client-side parallelism; each job uploads on its own when ready):

```bash
API=http://EXTERNAL_IP:8000
ls *.pdf | xargs -n1 -P4 -I{} curl -X POST "$API/v1/jobs" \
  -F "file=@{}" \
  -F "folder_id=YOUR_SHARED_DRIVE_FOLDER_ID"
```

## Performance tuning

See [`QUOTA.md`](./QUOTA.md). The limiting factor for large async jobs is
Document AI **online** pages/min quota, not VM CPU. Confirm and raise it
before turning up `OCR_WORKER_CONCURRENCY` or `ONLINE_MAX_CONCURRENCY` past
the defaults shipped in `ocrapi.env.example`.
