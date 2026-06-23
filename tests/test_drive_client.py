"""Drive client unit tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import UpstreamServiceError
from app.services.drive_client import DriveClient, _is_retryable_upload_error


@pytest.fixture
def drive_client(tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")
    monkeypatch.setenv("GCP_LOCATION", "us")
    monkeypatch.setenv("GCP_PROCESSOR_ID", "test-processor")
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    creds_path = tmp_path / "creds.json"
    creds_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_path))

    with patch("app.services.drive_client.service_account.Credentials.from_service_account_file") as mock_creds:
        mock_credentials = MagicMock()
        mock_credentials.refresh = MagicMock()
        mock_creds.return_value = mock_credentials
        client = DriveClient(
            Settings(
                gcp_project_id="test-project",
                gcp_processor_id="test-processor",
                gcs_bucket="test-bucket",
                google_application_credentials=str(creds_path),
            )
        )
    return client


def test_is_retryable_upload_error():
    assert _is_retryable_upload_error(
        Exception("Redirected but the response is missing a Location: header.")
    )
    assert _is_retryable_upload_error(Exception("SSLEOFError: EOF occurred"))
    assert not _is_retryable_upload_error(Exception("File not found: abc"))


def test_upload_pdf_retries_on_transient_error(drive_client):
    call_count = 0

    def flaky_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise Exception("Redirected but the response is missing a Location: header.")
        return {"id": "file-1", "name": "doc.pdf", "webViewLink": "https://drive.example/view"}

    with patch.object(drive_client, "_build_service") as mock_build:
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.files.return_value.create.return_value.execute.side_effect = flaky_execute

        with patch("app.services.drive_client.time.sleep"):
            result = drive_client.upload_pdf(b"%PDF-1.4", "doc.pdf", "folder-id")

    assert result.fileId == "file-1"
    assert call_count == 2
    assert mock_build.call_count == 2


def test_upload_pdf_raises_after_exhausted_retries(drive_client):
    with patch.object(drive_client, "_build_service") as mock_build:
        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_service.files.return_value.create.return_value.execute.side_effect = Exception(
            "Redirected but the response is missing a Location: header."
        )

        with patch("app.services.drive_client.time.sleep"):
            with pytest.raises(UpstreamServiceError, match="missing a Location"):
                drive_client.upload_pdf(b"%PDF-1.4", "doc.pdf", "folder-id")

    assert mock_build.call_count == 3


def test_concurrent_uploads_use_isolated_services(drive_client):
    build_count = 0
    build_lock = threading.Lock()
    execute_barrier = threading.Barrier(4)

    def build_service():
        nonlocal build_count
        with build_lock:
            build_count += 1
        service = MagicMock()

        def execute(*args, **kwargs):
            execute_barrier.wait(timeout=5)
            thread_name = threading.current_thread().name
            return {
                "id": f"file-{thread_name}",
                "name": f"{thread_name}.pdf",
                "webViewLink": f"https://drive.example/{thread_name}",
            }

        service.files.return_value.create.return_value.execute.side_effect = execute
        return service

    with patch.object(drive_client, "_build_service", side_effect=build_service):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    drive_client.upload_pdf,
                    b"%PDF-1.4",
                    f"doc-{i}.pdf",
                    "folder-id",
                )
                for i in range(4)
            ]
            results = [future.result() for future in as_completed(futures)]

    assert len(results) == 4
    assert build_count == 4
    assert len({result.fileId for result in results}) == 4
