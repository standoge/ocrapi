# Document AI quota: read before raising concurrency

The single hardest ceiling on this stack's throughput is **Document AI online
processing quota**, not VM CPU. The OCR work itself runs on Google's side; the
VM just rasterizes pages locally and forwards them to the
`documentai.processors.processOnline` endpoint.

With the defaults out of the box, you will hit `RESOURCE_EXHAUSTED` (HTTP 429
/ gRPC code 8) long before saturating a `c4-standard-16`. The retry policy in
[`ocr_documentai_plugin/documentai_client.py`](../../ocr_documentai_plugin/documentai_client.py)
will paper over short bursts, but a sustained overload turns into job
failures.

## Default vs. effective limits

| Metric                                                       | Default (approx.)               | Where it bites                                                                                          |
| ------------------------------------------------------------ | ------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `processor_request_per_minute_per_project_per_region`        | ~1,800 req/min (30 req/s)       | Each rasterized page = 1 online request. 32 parallel page workers can spike past this for a few seconds.|
| Online processing page-rate                                  | varies, often ~120 pages/min    | The real ceiling for sustained throughput.                                                              |
| `prediction_long_running_requests_per_minute_per_processor`  | low                             | Only matters if you later switch to `batch_process_documents`.                                          |

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

1. On the Quotas page, tick the row for the metric you want to raise.
2. Click **EDIT QUOTAS**.
3. Enter the new value and a justification ("Searchable-PDF OCR API; expected
    sustained 50 req/s, peak 100 req/s in region `us`").
4. Submit.

CLI:

```bash
# Example: raise online-process requests per minute to 6000 for region `us`.
gcloud alpha services quota update \
    --service=documentai.googleapis.com \
    --consumer=projects/$(gcloud config get-value project) \
    --metric=documentai.googleapis.com/processor_request_per_minute_per_project_per_region \
    --unit=1/min/{project}/{region} \
    --value=6000 \
    --dimensions=region=us
```

Approval typically takes a few minutes for small bumps and up to a few
business days for large ones.

## Right-sizing concurrency against quota

Keep this rule in mind when tuning [`ocrapi.env.example`](./ocrapi.env.example):

```
peak_req_per_sec  =  (OCR_WORKER_CONCURRENCY * OCRMYPDF_JOBS) / avg_seconds_per_page
```

With `OCR_WORKER_CONCURRENCY=4`, `OCRMYPDF_JOBS=8`, and ~1.5s per page on
Document AI, peak ≈ `32 / 1.5 ≈ 21 req/s ≈ 1,260 req/min`. That fits under the
default 1,800 req/min with headroom. If you raise either knob, also raise the
processor quota first.

## Signs you need a quota bump

- `RESOURCE_EXHAUSTED` errors in `journalctl -u ocrapi.service`
- Jobs failing with `OCR pipeline failed: 429 ...` after the tenacity retry
    chain exhausts (`stop_after_attempt(5)` in
    [`ocr_documentai_plugin/documentai_client.py`](../../ocr_documentai_plugin/documentai_client.py))
- Latency per page suddenly rising (Google is throttling, not erroring).
