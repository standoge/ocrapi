FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md openapi.yml ./
COPY app ./app
COPY ocr_documentai_plugin ./ocr_documentai_plugin

RUN pip install --upgrade pip && pip install .

EXPOSE 8000

# Single uvicorn worker: JobManager owns an in-process asyncio queue; multiple
# uvicorn workers would create independent queues per process and break the
# bounded worker pool. Scale concurrency via OCR_WORKER_CONCURRENCY.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
