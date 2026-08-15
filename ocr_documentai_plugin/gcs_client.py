"""Google Cloud Storage helpers for Document AI batch processing."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from google.cloud import storage

if TYPE_CHECKING:
    from app.config import Settings

_client: GCSClient | None = None
_client_lock = threading.Lock()


class GCSClient:
    """Thin wrapper around Cloud Storage for batch OCR scratch objects."""

    def __init__(self, settings: Settings) -> None:
        if not settings.gcs_bucket:
            raise ValueError(
                "GCS_BUCKET is not configured. It is only needed for the "
                "Document AI batch path; set it (and provision the bucket) "
                "before using batch OCR."
            )
        self._settings = settings
        self._bucket_name = settings.gcs_bucket
        credentials_path = settings.credentials_path
        if credentials_path and credentials_path.exists():
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(credentials_path)
        self._client = storage.Client(project=settings.gcp_project_id)
        self._bucket = self._client.bucket(self._bucket_name)

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    def upload_file(self, local_path: Path, blob_name: str, *, content_type: str = "application/pdf") -> str:
        blob = self._bucket.blob(blob_name)
        blob.upload_from_filename(str(local_path), content_type=content_type)
        return f"gs://{self._bucket_name}/{blob_name}"

    def gcs_uri(self, blob_name: str) -> str:
        return f"gs://{self._bucket_name}/{blob_name}"

    def gcs_prefix(self, prefix: str) -> str:
        normalized = prefix.rstrip("/")
        return f"gs://{self._bucket_name}/{normalized}/"

    def list_blob_names(self, prefix: str) -> list[str]:
        return [blob.name for blob in self._client.list_blobs(self._bucket_name, prefix=prefix)]

    def download_json(self, blob_name: str) -> dict:
        blob = self._bucket.blob(blob_name)
        return json.loads(blob.download_as_text(encoding="utf-8"))

    def delete_prefix(self, prefix: str) -> None:
        blobs = list(self._client.list_blobs(self._bucket_name, prefix=prefix))
        if blobs:
            self._bucket.delete_blobs(blobs)


def get_gcs_client(settings: Settings) -> GCSClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = GCSClient(settings)
    return _client
