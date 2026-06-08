"""Job API integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_api import MINIMAL_PDF


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
    def fake_pipeline(
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

    with patch("app.services.job_manager.ocr_pipeline.run_ocr_pipeline_file", side_effect=fake_pipeline):
        create_response = await client.post(
            "/v1/jobs",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )
        assert create_response.status_code == 202
        job_id = create_response.json()["jobId"]

        status_payload = None
        for _ in range(50):
            status_response = await client.get(f"/v1/jobs/{job_id}")
            assert status_response.status_code == 200
            status_payload = status_response.json()
            if status_payload["status"] == "succeeded":
                break
            if status_payload["status"] == "failed":
                pytest.fail(f"Job failed: {status_payload.get('error')}")
            await asyncio.sleep(0.05)
        else:
            pytest.fail(f"Job did not succeed in time: {status_payload}")

        assert status_payload["pageCount"] == 1
        assert status_payload["resultUrl"] is not None

        result_response = await client.get(f"/v1/jobs/{job_id}/result")
        assert result_response.status_code == 200
        assert result_response.headers["content-type"].startswith("application/pdf")
        assert result_response.content.startswith(b"%PDF")

