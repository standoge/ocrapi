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
async def test_ocr_returns_plain_text(client):
    with patch(
        "app.api.routes_ocr.run_ocr_text_pipeline",
        new=AsyncMock(return_value=("extracted text layer", 1)),
    ):
        response = await client.post(
            "/v1/ocr",
            files={"file": ("sample.pdf", MINIMAL_PDF, "application/pdf")},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "extracted text layer"


@pytest.mark.asyncio
async def test_ocr_rejects_non_pdf(client):
    response = await client.post(
        "/v1/ocr",
        files={"file": ("sample.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"


@pytest.mark.asyncio
async def test_openapi_yaml_available(client):
    response = await client.get("/openapi.yml")
    assert response.status_code == 200
    assert "DocAI OCR API" in response.text
    assert "/v1/ocr:" in response.text
