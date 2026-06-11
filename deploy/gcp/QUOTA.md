# Document AI quota: read before raising concurrency

The throughput ceiling for **async jobs** is now the **online**
`processDocument` quota, not VM CPU. Large PDFs are split into small page
chunks (≤ `ONLINE_CHUNK_PAGES` pages and ≤ `ONLINE_CHUNK_MAX_BYTES` each) and
OCR'd concurrently via online `processDocument`. The VM assembles the
searchable PDF with PyMuPDF — no GCS upload and no batch LRO for jobs.

The **sync** endpoint (`POST /v1/ocr`) still uses online `processDocument` on
whole PDFs (small docs only, up to `SYNC_MAX_PAGES`).

## Default vs. effective limits

| Metric | Default (approx.) | Where it bites |
| ------ | ----------------- | -------------- |
| Online `processDocument` pages/min | ~120 pages/min sustained | **Primary ceiling** for async jobs and sync OCR |
| Online `processDocument` request size | ~20 MB inline | Chunks stay under `ONLINE_CHUNK_MAX_BYTES` (18 MB default) |
| Online pages per request | processor limits | Chunks stay under `ONLINE_CHUNK_PAGES` (15 default) |

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

For most online quotas you can self-serve up to a moderate ceiling; larger
asks go through Google review.

GUI:

1. On the Quotas page, tick the row for the online prediction metric you want
   to raise.
2. Click **EDIT QUOTAS**.
3. Enter the new value and a justification ("Searchable-PDF OCR API; concurrent
   online OCR for 500 MB / 1000-page PDFs in region `us`").
4. Submit.

## Right-sizing concurrency against quota

Keep these rules in mind when tuning
[`ocrapi.env.example`](./ocrapi.env.example):

```
peak_online_calls  ~=  OCR_WORKER_CONCURRENCY * ONLINE_MAX_CONCURRENCY
peak_pages_per_min ~=  peak_online_calls * ONLINE_CHUNK_PAGES
```

With defaults `OCR_WORKER_CONCURRENCY=4`, `ONLINE_MAX_CONCURRENCY=8`, and
`ONLINE_CHUNK_PAGES=15`, peak concurrent online calls ≈ **32** and theoretical
peak pages/min ≈ **480** if every call finishes instantly. In practice the
~120 pages/min sustained online quota is the floor — a 1000-page PDF therefore
takes roughly **8–15 minutes** of OCR time, not hours.

Lower `ONLINE_MAX_CONCURRENCY` if you see `429 RESOURCE_EXHAUSTED` in logs
after retries exhaust. Raise the online pages/min quota before turning both
concurrency knobs up heavily.

## Signs you need a quota bump

- `RESOURCE_EXHAUSTED` errors in `journalctl -u ocrapi.service` during
  chunked online OCR
- Jobs failing with `OCR pipeline failed: 429 ...` after retries exhaust
- Jobs that succeed but take far longer than `pages / 120` minutes

## Cloud Storage

`GCS_BUCKET` is still provisioned for legacy batch scratch but is **not used**
by the current async job pipeline. The bucket lifecycle rule remains harmless;
no manual cleanup is required.
