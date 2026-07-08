# DocAI OCR API

REST API that accepts a PDF and returns a searchable PDF using **GCP Document AI** for OCR and **PyMuPDF** for invisible text-layer injection.

## Features

- `POST /v1/jobs` — submit a PDF for async OCR (recommended for large documents)
- `GET /v1/jobs/{jobId}` — poll job status
- `GET /v1/jobs/{jobId}/result` — download searchable PDF when ready
- `POST /v1/jobs/{jobId}/drive` — upload a finished job's result to Google Drive
- `POST /v1/ocr` — upload a PDF, receive a searchable PDF synchronously (small docs only)
- `POST /v1/ocr/drive` — OCR a PDF and upload the result to a Google Drive Shared Drive
- Design-first OpenAPI contract at `/openapi.yml`

## Documentation

Technical details live in the [project wiki](../../wiki):

- **[Architecture](../../wiki/Architecture)** — how the project works end to end
- **[Infrastructure](../../wiki/Infrastructure)** — GCP services, their configuration, and a service diagram
- **[OCR Plugin](../../wiki/OCR-Plugin)** — the `ocr_documentai_plugin` (Document AI + PyMuPDF)
- **[Job Manager](../../wiki/Job-Manager)** — the async job queue and worker pool
- **[Endpoints](../../wiki/Home)** — full HTTP endpoint reference

## Prerequisites

1. **GCP**
   - Enable Document AI API
   - Create a **Document OCR** processor and note `processor_id` and `location`
   - Create a service account with `roles/documentai.apiUser`
   - Create a GCS bucket (required by config; used only if batch OCR is enabled)

2. **Google Drive**
   - Enable **Google Drive API** on the project
   - Create or use a **Shared Drive** folder (not a personal My Drive folder)
   - Share the folder with the service account email as **Content Manager**
   - On GCE, ensure the VM OAuth access scopes include `https://www.googleapis.com/auth/drive` in addition to `cloud-platform` (see [`deploy/gcp/README.md`](deploy/gcp/README.md))

3. **Runtime**
   - Python 3.11+
   - No system OCR or PDF rasterization tools required (PyMuPDF and Document AI handle the pipeline)
   - Docker image is Python slim only (see `Dockerfile`)

## Setup

```bash
cp .env.example .env
# Edit .env with your values

pip install -e ".[dev]"
```

## Run locally

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open Swagger UI at http://localhost:8000/docs

## Docker

```bash
docker build -t ocrapi .
docker run --env-file .env -p 8000:8000 ocrapi
```

## Deploy to GCP

See [`deploy/gcp/README.md`](deploy/gcp/README.md) for provisioning a single GCE VM with Docker, a data disk for `JOBS_DIR`, Artifact Registry, and systemd units.

## API usage

### Async OCR (large documents)

For PDFs with many pages (1000+), use the job API so the client does not hold a long HTTP connection:

```bash
# Submit job
curl -X POST http://localhost:8000/v1/jobs \
  -F "file=@large-document.pdf"

# Poll status (replace JOB_ID)
curl http://localhost:8000/v1/jobs/JOB_ID

# Download result when status is "succeeded"
curl -OJ http://localhost:8000/v1/jobs/JOB_ID/result
```

Optional: upload result to Google Drive on completion (use the full Shared Drive folder ID):

```bash
curl -X POST http://localhost:8000/v1/jobs \
  -F "file=@large-document.pdf" \
  -F "filename=searchable-document.pdf" \
  -F "folder_id=YOUR_SHARED_DRIVE_FOLDER_ID"
```

Batch submit (multiple PDFs in parallel; each job uploads independently when OCR finishes):

```bash
API=http://YOUR_HOST:8000
ls *.pdf | xargs -n1 -P4 -I{} curl -X POST "$API/v1/jobs" \
  -F "file=@{}" \
  -F "folder_id=YOUR_SHARED_DRIVE_FOLDER_ID"
```

Or upload after the job succeeds (omit `folderId` to use `DRIVE_SHARED_FOLDER_ID`):

```bash
curl -X POST http://localhost:8000/v1/jobs/JOB_ID/drive \
  -H "Content-Type: application/json" \
  -d '{"folderId": "YOUR_SHARED_DRIVE_FOLDER_ID", "filename": "searchable-document.pdf"}'

curl -X POST http://localhost:8000/v1/jobs/JOB_ID/drive \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Sync OCR (small documents)

For quick tests or small PDFs (up to `SYNC_MAX_PAGES`, default 15):

```bash
curl -X POST http://localhost:8000/v1/ocr \
  -F "file=@document.pdf" \
  -o searchable.pdf

curl -X POST http://localhost:8000/v1/ocr/drive \
  -F "file=@document.pdf" \
  -F "filename=searchable-document.pdf" \
  -F "folder_id=YOUR_SHARED_DRIVE_FOLDER_ID"
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GCP_PROJECT_ID` | — | GCP project containing the Document AI processor |
| `GCP_LOCATION` | `us` | Document AI processor location |
| `GCP_PROCESSOR_ID` | — | Document OCR processor ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Path to service account JSON (optional on GCE; uses VM SA via ADC) |
| `GCS_BUCKET` | — | GCS bucket for batch OCR scratch (unused by current async jobs) |
| `BATCH_TIMEOUT_SECONDS` | `1800` | Timeout for batch OCR LRO polling |
| `BATCH_POLL_INTERVAL_SECONDS` | `10` | Poll interval for batch OCR |
| `DRIVE_SHARED_FOLDER_ID` | — | Optional default Shared Drive folder for `/v1/ocr/drive` and `/v1/jobs/{jobId}/drive` when folder ID is omitted |
| `MAX_UPLOAD_BYTES` | `629145600` (600 MB) | Maximum upload size for job and sync endpoints |
| `MAX_PDF_PAGES` | `2000` | Maximum pages for async job processing |
| `SYNC_MAX_PAGES` | `15` | Maximum pages for sync `/v1/ocr` and `/v1/ocr/drive` |
| `ONLINE_CHUNK_PAGES` | `15` | Maximum pages per online OCR chunk (async jobs) |
| `ONLINE_CHUNK_MAX_BYTES` | `18874368` (18 MB) | Maximum bytes per online OCR chunk |
| `ONLINE_MAX_CONCURRENCY` | `8` | Concurrent online `processDocument` calls per async job |
| `PDF_SAVE_INCREMENTAL` | `true` | PyMuPDF incremental save when injecting text layer |
| `PDF_USE_TEXTWRITER` | `true` | PyMuPDF TextWriter for faster token injection |
| `JOBS_DIR` | `jobs` | Directory for job input/output files |
| `OCR_WORKER_CONCURRENCY` | `4` | Number of PDFs processed in parallel |

> For concurrency tuning, quota, disk retention, and how the settings interact, see the
> [Job Manager](../../wiki/Job-Manager) and [Infrastructure](../../wiki/Infrastructure) wiki pages.

## Tests

```bash
pytest
```
