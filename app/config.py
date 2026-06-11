from functools import lru_cache
import json
import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gcp_project_id: str
    gcp_location: str = "us"
    gcp_processor_id: str
    google_application_credentials: str | None = None

    drive_shared_folder_id: str

    gcs_bucket: str
    batch_timeout_seconds: int = 1800
    batch_poll_interval_seconds: int = 10

    max_upload_bytes: int = 600 * 1024 * 1024
    max_pdf_pages: int = 2000
    sync_max_pages: int = 15

    jobs_dir: str = "jobs"
    ocr_worker_concurrency: int = 4

    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    @property
    def processor_resource_name(self) -> str:
        return (
            f"projects/{self.gcp_project_id}/locations/{self.gcp_location}"
            f"/processors/{self.gcp_processor_id}"
        )

    @property
    def documentai_api_endpoint(self) -> str:
        return f"{self.gcp_location}-documentai.googleapis.com"

    @property
    def openapi_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "openapi.yml"

    @property
    def resolved_jobs_dir(self) -> Path:
        path = Path(self.jobs_dir)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return path.resolve()

    @property
    def credentials_path(self) -> Path | None:
        if not self.google_application_credentials:
            return None
        path = Path(self.google_application_credentials)
        if not path.is_absolute():
            path = Path(__file__).resolve().parent.parent / path
        return path.resolve()

    @property
    def service_account_email(self) -> str | None:
        path = self.credentials_path
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data.get("client_email")

    @property
    def credentials_project_id(self) -> str | None:
        path = self.credentials_path
        if not path or not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data.get("project_id")

    def validate_gcp_configuration(self) -> list[str]:
        warnings: list[str] = []
        path = self.credentials_path
        if not path:
            warnings.append("GOOGLE_APPLICATION_CREDENTIALS is not set.")
            return warnings
        if not path.exists():
            warnings.append(f"Credentials file not found: {path}")
            return warnings

        creds_project = self.credentials_project_id
        sa_email = self.service_account_email
        if creds_project and creds_project != self.gcp_project_id:
            warnings.append(
                f"Credentials belong to project '{creds_project}' but "
                f"GCP_PROJECT_ID is '{self.gcp_project_id}'. Grant "
                f"'roles/documentai.apiUser' on '{self.gcp_project_id}' to "
                f"service account '{sa_email}'."
            )
        elif sa_email:
            warnings.append(
                f"Using service account '{sa_email}' for Document AI in project "
                f"'{self.gcp_project_id}'."
            )
        return warnings


@lru_cache
def get_settings() -> Settings:
    return Settings()
