"""Google Drive upload service."""

from __future__ import annotations

import io
import threading

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

# Per-request socket timeout for the (otherwise timeout-less) httplib2 transport.
_HTTP_TIMEOUT_SECONDS = 300
# Number of automatic retries for transient (5xx / socket / SSL) upload errors.
_UPLOAD_NUM_RETRIES = 5

_drive_client: DriveClient | None = None

# #region agent log (debug b90bd8 - upload concurrency instrumentation)
import logging as _logging

_dbg_logger = _logging.getLogger("app.services.drive_client")
_dbg_counter_lock = threading.Lock()
_dbg_active_uploads = 0
# #endregion


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
        self._credentials = credentials
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _build_http(self) -> google_auth_httplib2.AuthorizedHttp:
        """A fresh authorized transport per request.

        httplib2.Http is not thread-safe and reuses cached keep-alive
        connections; sharing one across the job worker threads corrupts the
        SSL socket (SSLEOFError / SSL internal error). Each upload gets its own.
        """
        return google_auth_httplib2.AuthorizedHttp(
            self._credentials,
            http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS),
        )

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        folder_id: str,
    ) -> DriveUploadResponse:
        media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=True)
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "application/pdf",
        }

        try:
            created = (
                self._service.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id,name,webViewLink",
                    supportsAllDrives=True,
                )
                .execute(http=self._build_http(), num_retries=_UPLOAD_NUM_RETRIES)
            )
        except Exception as exc:
            raise UpstreamServiceError(f"Google Drive upload failed: {exc}") from exc
        finally:
            # #region agent log (debug b90bd8 - H-1 concurrency check)
            with _dbg_counter_lock:
                _dbg_active_uploads -= 1
            _dbg_logger.info(
                "[debug b90bd8] Drive upload END thread=%s file=%s",
                threading.current_thread().name, filename,
            )
            # #endregion

        return DriveUploadResponse(
            fileId=created["id"],
            name=created["name"],
            webViewLink=created.get("webViewLink", f"https://drive.google.com/file/d/{created['id']}/view"),
        )


def get_drive_client(settings: Settings) -> DriveClient:
    global _drive_client
    if _drive_client is None:
        _drive_client = DriveClient(settings)
    return _drive_client
