# Deploying ocrapi to a single GCE VM

This folder contains everything needed to run ocrapi on a Compute Engine VM
with Docker, a data disk for `JOBS_DIR`, and a GCS bucket (legacy batch
scratch, unused by the current job pipeline). Async jobs split PDFs into
small chunks, run concurrent online `processDocument` calls, and the VM
assembles searchable PDFs with PyMuPDF — no Ghostscript rasterization.

The app is served over **HTTPS on port 8000**, with TLS terminated in-process by
uvicorn (`--ssl-keyfile` / `--ssl-certfile`) — no reverse proxy. A long-lived
self-signed certificate is generated once by [`02-vm-setup.sh`](./02-vm-setup.sh)
and stored on the data disk at `/data/tls`, so uploads and results are encrypted
in transit. See [TLS / certificate](#tls--certificate) for client trust.

## Files

| File                                                 | Where it runs    | Purpose                                                            |
| ---------------------------------------------------- | ---------------- | ------------------------------------------------------------------ |
| [`01-provision.sh`](./01-provision.sh)               | dev machine      | Create SA, GCS bucket, Artifact Registry, firewall, data disk, VM (SA + Drive OAuth scopes). |
| [`02-vm-setup.sh`](./02-vm-setup.sh)                 | VM (root)        | Format/mount data disk, generate self-signed TLS cert, install Docker, register systemd units. |
| [`03-build-and-push.sh`](./03-build-and-push.sh)     | dev machine      | Build linux/amd64 image, push to Artifact Registry.                |
| [`04-run.sh`](./04-run.sh)                           | VM (root)        | Pin image tag, (re)start `ocrapi.service`, health-check.           |
| [`ocrapi.env.example`](./ocrapi.env.example)         | VM (`/etc/ocrapi.env`) | Production env: GCS bucket, concurrency, paths, no key file.   |
| [`ocrapi.service`](./ocrapi.service)                 | VM systemd unit  | Runs the container on `0.0.0.0:8000` over TLS, with `/data/jobs` and `/data/tls` bind mounts. |
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

The API is now served over HTTPS on the VM's external IP. From your machine
(substitute the external IP printed by `01-provision.sh`). Trust the cert with
`--cacert` after copying it (see [TLS / certificate](#tls--certificate)), or use
`-k` to skip identity verification — traffic is encrypted either way:

```bash
curl -sfk https://EXTERNAL_IP:8000/healthz
# Swagger UI: https://EXTERNAL_IP:8000/docs
```

## TLS / certificate

TLS is terminated inside uvicorn (`--ssl-keyfile` / `--ssl-certfile` in
[`ocrapi.service`](./ocrapi.service)) — there is no reverse proxy. `02-vm-setup.sh`
generates a self-signed certificate on the data disk and reuses it on every rerun:

| Item | Value |
|------|-------|
| Location | `/data/tls/cert.pem`, `/data/tls/key.pem` (survives redeploys) |
| Validity | 10 years (`-days 3650`) — no renewal workflow needed |
| SANs | `localhost`, `127.0.0.1`, and the VM's internal + external IPs (from the metadata server at setup time) |

**Trust it on clients** — copy the cert once and pass it with `--cacert`:

```bash
gcloud compute scp ocrapi-vm:/data/tls/cert.pem . --zone=us-central1-a --project=YOUR_PROJECT
curl --cacert cert.pem https://EXTERNAL_IP:8000/healthz
```

Or skip identity verification with `curl -k` — traffic is still encrypted; you only
lose protection against an active man-in-the-middle (passive sniffing is already
defeated by the encryption).

**IP-SAN caveat.** The cert pins the VM's IPs. The default external IP is *ephemeral*,
so a VM stop/start can change it and break `--cacert` validation. Either **reserve a
static external IP** (recommended), reach the VM by its stable internal IP, or
regenerate the cert after the IP changes.

**Regenerate** (e.g. after an IP change or to add a name):

```bash
sudo rm /data/tls/cert.pem /data/tls/key.pem
# optional: add a DNS alias or reserved IP to the cert
sudo TLS_EXTRA_SAN="DNS:ocrapi.intranet.local" bash deploy/gcp/02-vm-setup.sh
sudo bash deploy/gcp/04-run.sh   # restart to pick up the new cert
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
- **Sync OCR** (`POST /v1/ocr`): same chunked online `processDocument` path →
  returns the extracted text layer as `text/plain` (no PyMuPDF injection).
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
API=https://EXTERNAL_IP:8000
ls *.pdf | xargs -n1 -P4 -I{} curl --cacert cert.pem -X POST "$API/v1/jobs" \
  -F "file=@{}" \
  -F "folder_id=YOUR_SHARED_DRIVE_FOLDER_ID"
```

## Performance tuning

See [`QUOTA.md`](./QUOTA.md). The limiting factor for large async jobs is
Document AI **online** pages/min quota, not VM CPU. Confirm and raise it
before turning up `OCR_WORKER_CONCURRENCY` or `ONLINE_MAX_CONCURRENCY` past
the defaults shipped in `ocrapi.env.example`.
