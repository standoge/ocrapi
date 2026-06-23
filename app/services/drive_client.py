"""Google Drive upload service."""

from __future__ import annotations

import io
import threading
import time
from typing import Any

import google.auth
import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.config import Settings
from app.exceptions import ConfigurationError, UpstreamServiceError
from app.schemas import DriveUploadResponse

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_HTTP_TIMEOUT_SECONDS = 300
_UPLOAD_EXECUTE_RETRIES = 5
_MAX_UPLOAD_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1, 2, 4)

_RETRYABLE_ERROR_MARKERS = (
    "redirected but the response is missing a location",
    "ssleoferror",
    "ssl",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "502",
    "503",
    "504",
    "500",
    "internal error",
)

_drive_client: DriveClient | None = None


def _is_retryable_upload_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_ERROR_MARKERS)


def _lock_credentials_refresh(credentials: Any) -> Any:
    """Wrap credentials.refresh so parallel uploads do not race token refresh."""
    refresh_lock = threading.Lock()
    original_refresh = credentials.refresh

    def locked_refresh(request: Any) -> None:
        with refresh_lock:
            return original_refresh(request)

    credentials.refresh = locked_refresh  # type: ignore[method-assign]
    return credentials


class DriveClient:
    def __init__(self, settings: Settings) -> None:
        credentials_path = settings.credentials_path
        if credentials_path and credentials_path.exists():
            credentials = service_account.Credentials.from_service_account_file(
                str(credentials_path),
                scopes=SCOPES,
            )
        else:
            try:
                credentials, _ = google.auth.default(scopes=SCOPES)
            except google.auth.exceptions.DefaultCredentialsError as exc:
                raise ConfigurationError(
                    "No Google credentials found: set GOOGLE_APPLICATION_CREDENTIALS "
                    "to a service account JSON file, or run on a host with an attached "
                    "service account (Application Default Credentials)."
                ) from exc
        self._credentials = _lock_credentials_refresh(credentials)

    def _build_service(self) -> Any:
        """Fresh httplib2 transport and Drive API client for one upload session."""
        http = httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS)
        authed_http = google_auth_httplib2.AuthorizedHttp(self._credentials, http=http)
        return build("drive", "v3", http=authed_http, cache_discovery=False)

    def _execute_upload(
        self,
        pdf_bytes: bytes,
        filename: str,
        folder_id: str,
    ) -> dict[str, Any]:
        media = MediaIoBaseUpload(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            resumable=True,
        )
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "application/pdf",
        }
        service = self._build_service()
        return (
            service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute(num_retries=_UPLOAD_EXECUTE_RETRIES)
        )

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        folder_id: str,
    ) -> DriveUploadResponse:
        last_exc: Exception | None = None
        for attempt in range(_MAX_UPLOAD_ATTEMPTS):
            try:
                created = self._execute_upload(pdf_bytes, filename, folder_id)
                return DriveUploadResponse(
                    fileId=created["id"],
                    name=created["name"],
                    webViewLink=created.get(
                        "webViewLink",
                        f"https://drive.google.com/file/d/{created['id']}/view",
                    ),
                )
            except Exception as exc:
                last_exc = exc
                if attempt >= _MAX_UPLOAD_ATTEMPTS - 1 or not _is_retryable_upload_error(exc):
                    break
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt])

        assert last_exc is not None
        raise UpstreamServiceError(f"Google Drive upload failed: {last_exc}") from last_exc


def get_drive_client(settings: Settings) -> DriveClient:
    global _drive_client
    if _drive_client is None:
        _drive_client = DriveClient(settings)
    return _drive_client
