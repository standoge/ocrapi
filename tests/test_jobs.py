"""Job API integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from tests.test_api import MINIMAL_PDF


def _fake_pipeline(
    input_path: Path,
    output_path: Path,
    settings,
    *,
    max_pages=None,
    job_id=None,
    original_filename=None,
) -> int:
    output_path.write_bytes(MINIMAL_PDF)
    return 1


async def _wait_for_job_success(client, job_id: str) -> dict:
    status_payload = None
    for _ in range(50):
        status_response = await client.get(f"/v1/jobs/{job_id}")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        if status_payload["status"] == "succeeded":
            return status_payload
        if status_payload["status"] == "failed":
            pytest.fail(f"Job failed: {status_payload.get('error')}")
        await asyncio.sleep(0.05)
    pytest.fail(f"Job did not succeed in time: {status_payload}")


@pytest.mark.asyncio
async def test_create_job_returns_202(client):
    with patch("app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file", return_value=1):
        response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    assert "jobId" in payload
    assert payload["statusUrl"].endswith(f"/v1/jobs/{payload['jobId']}")


@pytest.mark.asyncio
async def test_get_job_status_not_found(client):
    response = await client.get("/v1/jobs/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_job_lifecycle(client):
    with patch("app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file", side_effect=_fake_pipeline):
        create_response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["jobId"]

        status_payload = await _wait_for_job_success(client, job_id)

        assert status_payload["pageCount"] == 1
        assert status_payload["resultUrl"] is not None

        result_response = await client.get(f"/v1/jobs/{job_id}/result")
        assert result_response.status_code == 200
        assert result_response.headers["content-type"].startswith("application/pdf")
        assert result_response.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_upload_job_to_drive(client):
    from app.schemas import DriveUploadResponse

    mock_result = DriveUploadResponse(
        fileId="drive-file-123",
        name="sample_ocr.pdf",
        webViewLink="https://drive.google.com/file/d/drive-file-123/view",
    )

    with patch(
        "app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file",
        side_effect=_fake_pipeline,
    ), patch("app.services.job_manager.get_drive_client") as mock_drive_factory:
        mock_drive_factory.return_value.upload_pdf.return_value = mock_result

        create_response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["jobId"]
        await _wait_for_job_success(client, job_id)

        drive_response = await client.post(
            f"/v1/jobs/{job_id}/drive",
            json={"folderId": "target-folder-id", "filename": "custom-name.pdf"},
        )

    assert drive_response.status_code == 200
    payload = drive_response.json()
    assert payload["fileId"] == "drive-file-123"
    assert payload["name"] == "sample_ocr.pdf"
    assert payload["webViewLink"].startswith("https://drive.google.com/")

    status_response = await client.get(f"/v1/jobs/{job_id}")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["driveFileId"] == "drive-file-123"
    assert status_payload["driveWebViewLink"] == mock_result.webViewLink
    assert status_payload["driveUploadError"] is None


@pytest.mark.asyncio
async def test_upload_job_to_drive_uses_env_fallback(client):
    from app.schemas import DriveUploadResponse

    mock_result = DriveUploadResponse(
        fileId="drive-file-456",
        name="sample_ocr.pdf",
        webViewLink="https://drive.google.com/file/d/drive-file-456/view",
    )

    with patch(
        "app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file",
        side_effect=_fake_pipeline,
    ), patch("app.services.job_manager.get_drive_client") as mock_drive_factory:
        mock_drive_factory.return_value.upload_pdf.return_value = mock_result

        create_response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["jobId"]
        await _wait_for_job_success(client, job_id)

        drive_response = await client.post(f"/v1/jobs/{job_id}/drive", json={})

    assert drive_response.status_code == 200
    mock_drive_factory.return_value.upload_pdf.assert_called_once()
    assert mock_drive_factory.return_value.upload_pdf.call_args.args[2] == "test-folder-id"


@pytest.mark.asyncio
async def test_upload_job_to_drive_missing_folder(client):
    from unittest.mock import MagicMock

    from app.config import get_settings
    from app.main import app

    mock_settings = MagicMock()
    mock_settings.drive_shared_folder_id = None
    app.dependency_overrides[get_settings] = lambda: mock_settings
    try:
        response = await client.post(
            "/v1/jobs/00000000-0000-0000-0000-000000000000/drive",
            json={},
        )
    finally:
        app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 400
    assert "DRIVE_SHARED_FOLDER_ID" in response.json()["detail"]


@pytest.mark.asyncio
async def test_upload_job_to_drive_not_ready(client):
    pipeline_started = Event()
    release_pipeline = Event()

    def blocking_pipeline(
        input_path: Path,
        output_path: Path,
        settings,
        *,
        max_pages=None,
        job_id=None,
        original_filename=None,
    ) -> int:
        pipeline_started.set()
        release_pipeline.wait(timeout=5)
        output_path.write_bytes(MINIMAL_PDF)
        return 1

    with patch(
        "app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file",
        side_effect=blocking_pipeline,
    ):
        create_response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["jobId"]

        assert pipeline_started.wait(timeout=2), "OCR worker did not start in time"

        drive_response = await client.post(
            f"/v1/jobs/{job_id}/drive",
            json={"folderId": "target-folder-id"},
        )

        release_pipeline.set()

    assert drive_response.status_code == 400


@pytest.mark.asyncio
async def test_upload_job_to_drive_not_found(client):
    response = await client.post(
        "/v1/jobs/00000000-0000-0000-0000-000000000000/drive",
        json={"folderId": "target-folder-id"},
    )
    assert response.status_code == 404

