"""Drive client unit tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.exceptions import UpstreamServiceError
from app.services.drive_client import DriveClient, DriveUploadError, _is_retryable_upload_error


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


def _success_responses(session_uri: str = "https://upload.example/session"):
    init_response = MagicMock()
    init_response.ok = True
    init_response.headers = {"Location": session_uri}

    upload_response = MagicMock()
    upload_response.ok = True
    upload_response.json.return_value = {
        "id": "file-1",
        "name": "doc.pdf",
        "webViewLink": "https://drive.example/view",
    }
    return init_response, upload_response


def test_is_retryable_upload_error():
    assert _is_retryable_upload_error(
        Exception("Redirected but the response is missing a Location: header.")
    )
    assert _is_retryable_upload_error(Exception("SSLEOFError: EOF occurred"))
    assert _is_retryable_upload_error(requests.exceptions.ConnectionError("reset"))
    assert _is_retryable_upload_error(DriveUploadError("missing Location", retryable=True))
    assert not _is_retryable_upload_error(DriveUploadError("not found", retryable=False))
    assert not _is_retryable_upload_error(Exception("File not found: abc"))


def test_upload_pdf_retries_on_transient_error(drive_client):
    attempt_count = 0
    session_count = 0

    def new_session():
        nonlocal session_count
        session_count += 1
        session = MagicMock()

        def post(*args, **kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 2:
                response = MagicMock()
                response.ok = True
                response.headers = {}
                return response
            init_response, _ = _success_responses()
            return init_response

        def put(*args, **kwargs):
            _, upload_response = _success_responses()
            return upload_response

        session.post.side_effect = post
        session.put.side_effect = put
        return session

    with patch.object(drive_client, "_new_session", side_effect=new_session):
        with patch("app.services.drive_client.time.sleep"):
            result = drive_client.upload_pdf(b"%PDF-1.4", "doc.pdf", "folder-id")

    assert result.fileId == "file-1"
    assert attempt_count == 2
    assert session_count == 2


def test_upload_pdf_raises_after_exhausted_retries(drive_client):
    session = MagicMock()
    init_response = MagicMock()
    init_response.ok = True
    init_response.headers = {}
    session.post.return_value = init_response

    with patch.object(drive_client, "_new_session", return_value=session):
        with patch("app.services.drive_client.time.sleep"):
            with pytest.raises(UpstreamServiceError, match="missing Location"):
                drive_client.upload_pdf(b"%PDF-1.4", "doc.pdf", "folder-id")

    assert session.post.call_count == 3


def test_upload_pdf_non_retryable_403(drive_client):
    session = MagicMock()
    init_response = MagicMock()
    init_response.ok = False
    init_response.status_code = 403
    init_response.text = "Forbidden"
    session.post.return_value = init_response

    with patch.object(drive_client, "_new_session", return_value=session):
        with pytest.raises(UpstreamServiceError, match="403"):
            drive_client.upload_pdf(b"%PDF-1.4", "doc.pdf", "folder-id")

    assert session.post.call_count == 1


def test_concurrent_uploads_use_isolated_sessions(drive_client):
    session_count = 0
    session_lock = threading.Lock()
    upload_barrier = threading.Barrier(4)

    def new_session():
        nonlocal session_count
        with session_lock:
            session_count += 1
        session = MagicMock()
        init_response, upload_response = _success_responses(
            session_uri=f"https://upload.example/session-{threading.current_thread().name}",
        )
        session.post.return_value = init_response

        def put(*args, **kwargs):
            upload_barrier.wait(timeout=5)
            thread_name = threading.current_thread().name
            response = MagicMock()
            response.ok = True
            response.json.return_value = {
                "id": f"file-{thread_name}",
                "name": f"{thread_name}.pdf",
                "webViewLink": f"https://drive.example/{thread_name}",
            }
            return response

        session.put.side_effect = put
        return session

    with patch.object(drive_client, "_new_session", side_effect=new_session):
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
    assert session_count == 4
    assert len({result.fileId for result in results}) == 4
