"""GCP Document AI client helpers."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud import documentai_v1 as documentai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ocr_documentai_plugin.gcs_client import GCSClient, get_gcs_client

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

_client: DocumentAIClient | None = None
_client_lock = threading.Lock()


class DocumentAIClient:
    """Wrapper around Document AI online and batch processing."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        credentials_path = settings.credentials_path
        if credentials_path and credentials_path.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)
        client_options = {"api_endpoint": settings.documentai_api_endpoint}
        self._client = documentai.DocumentProcessorServiceClient(
            client_options=client_options
        )
        self._processor_name = settings.processor_resource_name

    @retry(
        retry=retry_if_exception_type((GoogleAPICallError, RetryError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        reraise=True,
    )
    def process_pdf_online(self, pdf_bytes: bytes) -> documentai.Document:
        raw_document = documentai.RawDocument(content=pdf_bytes, mime_type="application/pdf")
        request = documentai.ProcessRequest(
            name=self._processor_name,
            raw_document=raw_document,
            skip_human_review=True,
        )
        result = self._client.process_document(request=request)
        return result.document

    def batch_process_from_gcs(
        self,
        gcs_input_prefix: str,
        gcs_output_prefix: str,
        *,
        gcs_client: GCSClient | None = None,
    ) -> list[documentai.Document]:
        """Run batch OCR on PDFs under a GCS prefix and return parsed Document shards."""
        request = documentai.BatchProcessRequest(
            name=self._processor_name,
            input_documents=documentai.BatchDocumentsInputConfig(
                gcs_prefix=documentai.GcsPrefix(gcs_uri_prefix=gcs_input_prefix),
            ),
            document_output_config=documentai.DocumentOutputConfig(
                gcs_output_config=documentai.DocumentOutputConfig.GcsOutputConfig(
                    gcs_uri=gcs_output_prefix,
                ),
            ),
            skip_human_review=True,
        )

        logger.info(
            "Submitting Document AI batch job: input=%s output=%s",
            gcs_input_prefix,
            gcs_output_prefix,
        )
        operation = self._client.batch_process_documents(request=request)
        self._wait_for_operation(operation)

        storage_client = gcs_client or get_gcs_client(self._settings)
        output_prefix = self._gcs_prefix_to_blob_prefix(gcs_output_prefix)
        return self._download_document_shards(storage_client, output_prefix)

    def _wait_for_operation(self, operation) -> None:
        timeout = self._settings.batch_timeout_seconds
        poll_interval = self._settings.batch_poll_interval_seconds
        deadline = time.monotonic() + timeout

        while not operation.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Document AI batch operation timed out after {timeout} seconds"
                )
            logger.info(
                "Document AI batch operation in progress; polling again in %ss",
                poll_interval,
            )
            time.sleep(min(poll_interval, remaining))

        exception = operation.exception()
        if exception is not None:
            raise exception

        operation.result(timeout=0)

    @staticmethod
    def _gcs_prefix_to_blob_prefix(gcs_prefix: str) -> str:
        if not gcs_prefix.startswith("gs://"):
            raise ValueError(f"Expected gs:// URI, got {gcs_prefix!r}")
        _, _, remainder = gcs_prefix.partition("gs://")
        bucket, _, prefix = remainder.partition("/")
        if not bucket:
            raise ValueError(f"Invalid GCS URI: {gcs_prefix!r}")
        return prefix.rstrip("/") + "/"

    @staticmethod
    def _download_document_shards(
        gcs_client: GCSClient,
        output_prefix: str,
    ) -> list[documentai.Document]:
        blob_names = sorted(
            name
            for name in gcs_client.list_blob_names(output_prefix)
            if name.endswith(".json")
        )
        if not blob_names:
            raise RuntimeError(f"No Document AI batch output JSON found under {output_prefix!r}")

        documents: list[documentai.Document] = []
        for blob_name in blob_names:
            payload = gcs_client.download_json(blob_name)
            documents.append(documentai.Document.from_json(json.dumps(payload)))
        return documents


def get_documentai_client(settings: Settings) -> DocumentAIClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = DocumentAIClient(settings)
    return _client
