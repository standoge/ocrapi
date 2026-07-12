"""OCR endpoint routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import PlainTextResponse

from app.api.deps import read_and_validate_pdf
from app.config import Settings, get_settings
from app.services.ocr_pipeline import run_ocr_text_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OCR"])


@router.post(
    "/ocr",
    summary="OCR a PDF and return its text layer as plain text",
    response_class=PlainTextResponse,
    responses={
        200: {
            "content": {"text/plain": {}},
            "description": "Plain text extracted from the PDF via OCR",
        }
    },
)
async def ocr_pdf(
    file: UploadFile = File(...),
    settings: Settings = get_settings(),
) -> PlainTextResponse:
    pdf_bytes, filename = await read_and_validate_pdf(file, settings)
    logger.info("Starting OCR text extraction for %s (%d bytes)", filename, len(pdf_bytes))

    text, page_count = await run_ocr_text_pipeline(pdf_bytes, settings, filename)
    logger.info(
        "Completed OCR text extraction for %s (%d pages, %d chars)",
        filename,
        page_count,
        len(text),
    )

    return PlainTextResponse(content=text)
