"""API integration tests with mocked OCR pipeline."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\n"
    b"xref\n0 4\n0000000000 65535 f \n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n100\n%%EOF"
)


@pytest.mark.asyncio
async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ocr_returns_pdf(client):
    with patch(
        "app.api.routes_ocr.run_ocr_pipeline",
        new=AsyncMock(return_value=(MINIMAL_PDF, 1)),
    ):
        response = await client.post(
            "/v1/ocr",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_ocr_rejects_non_pdf(client):
    response = await client.post(
        "/v1/ocr",
        files={"file": ("sample.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.asyncio
async def test_ocr_drive_returns_metadata(client):
    from app.schemas import DriveUploadResponse

    mock_result = DriveUploadResponse(
        fileId="abc123",
        name="sample_ocr.pdf",
        webViewLink="https://drive.google.com/file/d/abc123/view",
    )

    with patch(
        "app.api.routes_drive.run_ocr_pipeline",
        new=AsyncMock(return_value=(MINIMAL_PDF, 1)),
    ), patch(
        "app.api.routes_drive.get_drive_client"
    ) as mock_drive_factory:
        mock_drive_factory.return_value.upload_pdf.return_value = mock_result
        response = await client.post(
            "/v1/ocr/drive",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["fileId"] == "abc123"
    assert payload["name"] == "sample_ocr.pdf"
    assert payload["webViewLink"].startswith("https://drive.google.com/")


@pytest.mark.asyncio
async def test_openapi_yaml_available(client):
    response = await client.get("/openapi.yml")
    assert response.status_code == 200
    assert "DocAI OCR API" in response.text
    assert "/v1/ocr:" in response.text
