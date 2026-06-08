"""OCR endpoint routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import Response

from app.api.deps import ocr_output_filename, read_and_validate_pdf
from app.config import Settings, get_settings
from app.services.ocr_pipeline import run_ocr_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OCR"])


@router.post(
    "/ocr",
    summary="OCR a PDF and return searchable PDF (sync, small docs only)",
    response_class=Response,
    responses={
        200: {
            "content": {"application/pdf": {}},
            "description": "Searchable PDF (PDF/A)",
        }
    },
)
async def ocr_pdf(
    file: UploadFile = File(...),
    settings: Settings = get_settings(),
) -> Response:
    pdf_bytes, filename = await read_and_validate_pdf(file, settings)
    logger.info("Starting OCR for %s (%d bytes)", filename, len(pdf_bytes))

    output_bytes, page_count = await run_ocr_pipeline(pdf_bytes, settings)
    output_name = ocr_output_filename(filename)
    logger.info("Completed OCR for %s (%d pages, %d bytes output)", filename, page_count, len(output_bytes))

    return Response(
        content=output_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{output_name}"'},
    )
