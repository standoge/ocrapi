"""Google Drive upload service."""

from __future__ import annotations

import time
from typing import Any

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

from app.config import Settings
from app.exceptions import ConfigurationError, UpstreamServiceError
from app.schemas import DriveUploadResponse

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_DRIVE_RESUMABLE_INIT_URL = (
    "https://www.googleapis.com/upload/drive/v3/files"
    "?uploadType=resumable&supportsAllDrives=true&fields=id,name,webViewLink"
)
_REQUEST_TIMEOUT = (30, 300)
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
    "429",
    "too many requests",
)

_drive_client: DriveClient | None = None


class DriveUploadError(Exception):
    """Drive upload failure; retryable=True triggers upload_pdf retry loop."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_upload_error(exc: Exception) -> bool:
    if isinstance(exc, DriveUploadError):
        return exc.retryable
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_ERROR_MARKERS)


def _http_status_retryable(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


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

    def _new_session(self) -> AuthorizedSession:
        """Fresh authorized requests session for one upload (thread-safe isolation)."""
        return AuthorizedSession(self._credentials)

    def _execute_upload(
        self,
        session: AuthorizedSession,
        pdf_bytes: bytes,
        filename: str,
        folder_id: str,
    ) -> dict[str, Any]:
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "application/pdf",
        }
        init_headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "application/pdf",
            "X-Upload-Content-Length": str(len(pdf_bytes)),
        }

        try:
            init_response = session.post(
                _DRIVE_RESUMABLE_INIT_URL,
                json=metadata,
                headers=init_headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            raise DriveUploadError(f"Drive resumable init request failed: {exc}", retryable=True) from exc

        if not init_response.ok:
            body = init_response.text[:500]
            retryable = _http_status_retryable(init_response.status_code)
            raise DriveUploadError(
                f"Drive resumable init failed ({init_response.status_code}): {body}",
                retryable=retryable,
            )

        session_uri = init_response.headers.get("Location")
        if not session_uri:
            raise DriveUploadError(
                "Drive resumable init succeeded but response is missing Location header.",
                retryable=True,
            )

        upload_headers = {"Content-Type": "application/pdf"}
        try:
            upload_response = session.put(
                session_uri,
                data=pdf_bytes,
                headers=upload_headers,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            raise DriveUploadError(f"Drive resumable upload failed: {exc}", retryable=True) from exc

        if not upload_response.ok:
            body = upload_response.text[:500]
            retryable = _http_status_retryable(upload_response.status_code)
            raise DriveUploadError(
                f"Drive resumable upload failed ({upload_response.status_code}): {body}",
                retryable=retryable,
            )

        try:
            created = upload_response.json()
        except ValueError as exc:
            raise DriveUploadError(
                "Drive resumable upload returned non-JSON response.",
                retryable=True,
            ) from exc

        if "id" not in created:
            raise DriveUploadError(
                "Drive resumable upload response missing file id.",
                retryable=True,
            )
        return created

    def upload_pdf(
        self,
        pdf_bytes: bytes,
        filename: str,
        folder_id: str,
    ) -> DriveUploadResponse:
        last_exc: Exception | None = None
        for attempt in range(_MAX_UPLOAD_ATTEMPTS):
            session = self._new_session()
            try:
                created = self._execute_upload(session, pdf_bytes, filename, folder_id)
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
