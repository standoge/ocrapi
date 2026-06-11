# Document AI quota: read before raising concurrency

The throughput ceiling for **async jobs** is **Document AI batch processing
quota**, not VM CPU. Large PDFs are submitted as a single batch operation;
Document AI rasterizes and OCRs server-side while the VM polls the long-running
operation and assembles the searchable PDF with PyMuPDF.

The **sync** endpoint (`POST /v1/ocr`) still uses the online
`processDocument` API on whole PDFs (small docs only, up to `SYNC_MAX_PAGES`).

## Default vs. effective limits

| Metric                                                       | Default (approx.)               | Where it bites                                                                                          |
| ------------------------------------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `prediction_long_running_requests_per_minute_per_processor`  | varies by region                | Each async job = 1 batch LRO. Raise this before increasing `OCR_WORKER_CONCURRENCY` heavily.            |
| Batch pages per document                                     | up to processor limits (~2000+) | Fits the app's `MAX_PDF_PAGES` default.                                                                 |
| Online `processDocument` requests                            | ~120 pages/min sustained        | Only affects sync `/v1/ocr` for small PDFs.                                                             |

Numbers shift over time and per processor type. Always check the live values
in the console.

## How to check current quota

GUI:

1. Open `https://console.cloud.google.com/iam-admin/quotas?project=<PROJECT_ID>`
2. Filter `Service` = **Cloud Document AI API**
3. Filter `Location` = the region you set in `.env` (`us` by default).
4. Note the **Current usage** column once load is steady.

CLI:

```bash
# All Document AI quotas in the project (limits column is the relevant one)
gcloud alpha services quota list \
    --service=documentai.googleapis.com \
    --consumer=projects/$(gcloud config get-value project)
```

## How to request more

For most batch quotas you can self-serve up to a moderate ceiling; larger
asks go through Google review.

GUI:

1. On the Quotas page, tick the row for the batch metric you want to raise.
2. Click **EDIT QUOTAS**.
3. Enter the new value and a justification ("Searchable-PDF OCR API; async
    batch jobs for 500 MB / 1000-page PDFs in region `us`").
4. Submit.

CLI:

```bash
# Example: raise batch LRO requests per minute for region `us`.
gcloud alpha services quota update \
    --service=documentai.googleapis.com \
    --consumer=projects/$(gcloud config get-value project) \
    --metric=documentai.googleapis.com/prediction_long_running_requests_per_minute_per_processor \
    --unit=1/min/{project}/{region}/{processor} \
    --value=120 \
    --dimensions=region=us
```

Approval typically takes a few minutes for small bumps and up to a few
business days for large ones.

## Right-sizing concurrency against quota

Keep this rule in mind when tuning [`ocrapi.env.example`](./ocrapi.env.example):

```
peak_batch_jobs_per_min  ~=  OCR_WORKER_CONCURRENCY / avg_minutes_per_job
```

With `OCR_WORKER_CONCURRENCY=4` and a 1000-page PDF finishing in ~15 minutes,
peak ≈ `4 / 15 ≈ 0.27 batch jobs/min`. That is well within typical batch
quotas. If you run many concurrent large jobs, raise the batch LRO quota first.

## Signs you need a quota bump

- `RESOURCE_EXHAUSTED` errors in `journalctl -u ocrapi.service` during batch submit
- Jobs failing with `OCR pipeline failed: 429 ...` after retries exhaust
- Batch operations stuck in `running` with rising queue depth across jobs

## Cloud Storage

Batch scratch lives in `GCS_BUCKET` (created by `01-provision.sh`) with a
1-day lifecycle delete rule. No manual cleanup is required; ensure the service
account has `roles/storage.objectAdmin` on that bucket.
