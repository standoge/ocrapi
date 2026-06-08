# Deploying ocrapi to a single GCE VM

This folder contains everything needed to run ocrapi on a Compute Engine VM
with Docker and an attached 500 GB SSD for `JOBS_DIR` and the ocrmypdf scratch
directory. It is the deployment target described in the project plan and fixes
the root cause of the `[Errno 28] No space left on device` failure: rasterized
pages now land on a large persistent disk instead of the VM's small root
filesystem.

The app is served directly over **plain HTTP on port 8000** (no TLS / reverse
proxy). This is the simplest setup for testing. Note that uploads and results
travel unencrypted; if you later want HTTPS, put a reverse proxy (Caddy,
nginx, or a GCP HTTPS Load Balancer) in front and bind the container to
`127.0.0.1:8000` in [`ocrapi.service`](./ocrapi.service).

## Files

| File                                                 | Where it runs    | Purpose                                                            |
| ---------------------------------------------------- | ---------------- | ------------------------------------------------------------------ |
| [`01-provision.sh`](./01-provision.sh)               | dev machine      | Create SA, Artifact Registry repo, firewall, data disk, GCE VM.    |
| [`02-vm-setup.sh`](./02-vm-setup.sh)                 | VM (root)        | Format/mount data disk, install Docker, register systemd units.    |
| [`03-build-and-push.sh`](./03-build-and-push.sh)     | dev machine      | Build linux/amd64 image, push to Artifact Registry.                |
| [`04-run.sh`](./04-run.sh)                           | VM (root)        | Pin image tag, (re)start `ocrapi.service`, health-check.           |
| [`ocrapi.env.example`](./ocrapi.env.example)         | VM (`/etc/ocrapi.env`) | Production env: tuned concurrency, paths, no key file.       |
| [`ocrapi.service`](./ocrapi.service)                 | VM systemd unit  | Runs the container on `0.0.0.0:8000` with `TMPDIR=/data/tmp` and bind mounts. |
| [`ocrapi-cleanup.sh`](./ocrapi-cleanup.sh)           | VM (`/usr/local/bin`) | Prunes old job folders and stale scratch.                     |
| [`ocrapi-cleanup.service`](./ocrapi-cleanup.service) | VM systemd unit  | Oneshot wrapper around the cleanup script.                         |
| [`ocrapi-cleanup.timer`](./ocrapi-cleanup.timer)     | VM systemd timer | Runs the cleanup daily + 10 min after boot.                        |
| [`QUOTA.md`](./QUOTA.md)                             | runbook          | How to verify and raise the Document AI quota ceiling.             |

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

# --- on the VM (SSH in) ---
gcloud compute ssh ocrapi-vm --zone=us-central1-a
# Copy the deploy/ folder onto the VM (e.g. git clone the repo) and:
sudo bash deploy/gcp/02-vm-setup.sh              # mounts /data, installs docker

# Edit /etc/ocrapi.env if you need to change GCP_PROJECT_ID / processor / Drive ID
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
du -sh /data/jobs /data/tmp

# Manual cleanup (defaults to 7 days)
sudo RETENTION_DAYS=3 /usr/local/bin/ocrapi-cleanup.sh

# Roll a new build
bash deploy/gcp/03-build-and-push.sh
# then on the VM:
sudo IMAGE=us-central1-docker.pkg.dev/YOUR_PROJECT/ocrapi/ocrapi:NEW_TAG \
     bash deploy/gcp/04-run.sh
```

## What was changed in the repo

- [`Dockerfile`](../../Dockerfile): removed `tesseract-ocr*` packages (the
    plugin disables Tesseract anyway and Document AI is the sole OCR engine),
    pinned `--workers 1` on uvicorn so the in-process `JobManager` queue isn't
    sharded across worker processes.

Everything else lives under this folder; the application code is untouched.

## How the disk-space failure is fixed

`ocrmypdf` rasterizes each PDF page at 300 DPI via Ghostscript into the system
temp dir before calling the OCR engine. On the local Windows machine, the
container's `/tmp` was on the small root filesystem; a 700 MB / 2000-page PDF
can produce tens of GB of scratch.

The systemd unit ([`ocrapi.service`](./ocrapi.service)) sets `TMPDIR=/data/tmp`
and bind-mounts the host's `/data/tmp` (on the 500 GB Hyperdisk) into the
container. Python's `tempfile` honors `TMPDIR`, and ocrmypdf uses `tempfile`,
so all scratch lands on the big disk. `JOBS_DIR` is bind-mounted the same
way. The retention timer keeps both directories from growing without bound.

## Performance tuning

See [`QUOTA.md`](./QUOTA.md). Short version: the limiting factor for sustained
throughput is the Document AI online-process quota for your processor, not the
VM. Confirm and raise it before turning up `OCR_WORKER_CONCURRENCY` or
`OCRMYPDF_JOBS` past the defaults shipped in `ocrapi.env.example`.
