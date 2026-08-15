# DocAI OCR API

REST API for OCR over PDFs using **GCP Document AI**. Async jobs produce a searchable PDF via **PyMuPDF** invisible text-layer injection; the sync endpoint returns the extracted text layer as plain text.

## Documentation

This README covers what you need to **run** the project. Technical and architectural documentation lives in the [project wiki](../../wiki):

- **[Architecture](../../wiki/Architecture)** — how the project works end to end
- **[Infrastructure](../../wiki/Infrastructure)** — GCP services, their configuration, and a service diagram
- **[OCR Plugin](../../wiki/OCR-Plugin)** — the `ocr_documentai_plugin` (Document AI + PyMuPDF)
- **[Job Manager](../../wiki/Job-Manager)** — the async job queue and worker pool
- **[Configuration](../../wiki/Configuration)** — full environment-variable reference
- **[Endpoints & usage](../../wiki/Home)** — full HTTP endpoint reference with curl examples

## Required knowledge

To run and operate this project you should be comfortable with:

- **Python 3.11+** and virtual environments (the app is FastAPI + uvicorn)
- **Docker** basics (build, run, `--env-file`) for containerized runs
- **GCP fundamentals**: projects, IAM service accounts, and enabling APIs — specifically **Document AI** (what a processor is) and **Cloud Storage**
- **Google Drive Shared Drives** (only if you use the Drive upload feature): how folder sharing and folder IDs work
- For production deploys: **GCE VMs**, **systemd** units, and `gcloud` CLI (see [`deploy/gcp/README.md`](deploy/gcp/README.md))

## Required resources

1. **GCP project** with:
   - Document AI API enabled, and a **Document OCR** processor (note its `processor_id` and `location`)
   - A service account with `roles/documentai.apiUser` (JSON key for local dev; on GCE the VM's attached service account is used — no key file)
   - A GCS bucket (required by config; used only if batch OCR is enabled)

2. **Google Drive** (optional, for result delivery):
   - Google Drive API enabled on the project
   - A **Shared Drive** folder (not a personal My Drive folder), shared with the service account email as **Content Manager**
   - On GCE, the VM's OAuth scopes must include `https://www.googleapis.com/auth/drive` in addition to `cloud-platform` (see [`deploy/gcp/README.md`](deploy/gcp/README.md))

3. **Local machine**:
   - Python 3.11+ (no system OCR or PDF tools needed — PyMuPDF and Document AI handle the pipeline)
   - Docker (optional, for containerized runs)

## Setup

```bash
cp .env.example .env
# Edit .env with your GCP project, processor and bucket values.
# Full variable reference: wiki Configuration page.

pip install -e ".[dev]"
```

## Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open Swagger UI at http://localhost:8000/docs, or smoke-test with:

```bash
curl http://localhost:8000/healthz
```

For endpoint usage (async jobs, sync OCR, Drive upload) see [Endpoints & usage](../../wiki/Home).

## Docker

```bash
docker build -t ocrapi .
docker run --env-file .env -p 8000:8000 ocrapi
```

## Deploy to GCP

See [`deploy/gcp/README.md`](deploy/gcp/README.md) for provisioning a single GCE VM with Docker, a data disk for `JOBS_DIR`, TLS, Artifact Registry, and systemd units. 

## Tests

```bash
pytest
```
