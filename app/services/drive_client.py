"""Google Drive upload service."""

from __future__ import annotations

import io
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from app.config import Settings
from app.exceptions import ConfigurationError, UpstreamServiceError
from app.schemas import DriveUploadResponse

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_drive_client: DriveClient | None = None


class DriveClient:
    def __init__(self, settings: Settings) -> None:
        credentials_path = settings.google_application_credentials
        if not credentials_path or not os.path.exists(credentials_path):
            raise ConfigurationError(
                "GOOGLE_APPLICATION_CREDENTIALS must point to a valid service account JSON file"
            )

        credentials = service_account.Credentials.from_service_account_file(
            credentials_path,
            scopes=SCOPES,
        )
        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)

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
                .execute()
            )
        except Exception as exc:
            raise UpstreamServiceError(f"Google Drive upload failed: {exc}") from exc

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
