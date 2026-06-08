"""Drive upload endpoint routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, UploadFile

from app.api.deps import ocr_output_filename, read_and_validate_pdf
from app.config import Settings, get_settings
from app.exceptions import ValidationError
from app.schemas import DriveUploadResponse
from app.services.drive_client import get_drive_client
from app.services.ocr_pipeline import run_ocr_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OCR"])


@router.post(
    "/ocr/drive",
    summary="OCR a PDF and upload to Google Drive (sync, small docs only)",
    response_model=DriveUploadResponse,
)
async def ocr_pdf_to_drive(
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
    folder_id: str | None = Form(default=None),
    settings: Settings = get_settings(),
) -> DriveUploadResponse:
    pdf_bytes, original_filename = await read_and_validate_pdf(file, settings)
    target_folder = folder_id or settings.drive_shared_folder_id
    if not target_folder:
        raise ValidationError("folder_id is required when DRIVE_SHARED_FOLDER_ID is not configured")

    output_name = filename or ocr_output_filename(original_filename)
    logger.info(
        "Starting OCR + Drive upload for %s -> folder %s",
        original_filename,
        target_folder,
    )

    output_bytes, page_count = await run_ocr_pipeline(pdf_bytes, settings)
    drive_client = get_drive_client(settings)
    result = drive_client.upload_pdf(output_bytes, output_name, target_folder)

    logger.info(
        "Uploaded OCR result for %s (%d pages) to Drive file %s",
        original_filename,
        page_count,
        result.fileId,
    )
    return result
