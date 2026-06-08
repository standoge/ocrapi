"""GCP Document AI client helpers."""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

from google.api_core.exceptions import GoogleAPICallError, RetryError
from google.cloud import documentai_v1 as documentai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from app.config import Settings

_client: DocumentAIClient | None = None
_client_lock = threading.Lock()


class DocumentAIClient:
    """Thin wrapper around the Document AI processor client."""

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
    def process_image(self, image_bytes: bytes, mime_type: str = "image/png") -> documentai.Document:
        raw_document = documentai.RawDocument(content=image_bytes, mime_type=mime_type)
        request = documentai.ProcessRequest(
            name=self._processor_name,
            raw_document=raw_document,
            skip_human_review=True,
        )
        result = self._client.process_document(request=request)
        return result.document


def get_documentai_client(settings: Settings) -> DocumentAIClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = DocumentAIClient(settings)
    return _client
