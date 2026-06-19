"""Async OCR job endpoint routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import ocr_output_filename, save_upload_to_path
from app.config import Settings, get_settings
from app.exceptions import ValidationError
from app.schemas import DriveUploadResponse, JobCreatedResponse, JobDriveUploadRequest, JobStatusResponse
from app.services.job_manager import get_job_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Jobs"])


def _job_status_response(job_id: str, meta: dict, request: Request) -> JobStatusResponse:
    result_url = None
    if meta.get("status") == "succeeded":
        result_url = str(request.url_for("get_job_result", job_id=job_id))

    return JobStatusResponse(
        jobId=meta["jobId"],
        status=meta["status"],
        pageCount=meta.get("pageCount"),
        error=meta.get("error"),
        createdAt=meta["createdAt"],
        startedAt=meta.get("startedAt"),
        finishedAt=meta.get("finishedAt"),
        resultUrl=result_url,
        driveFileId=meta.get("driveFileId"),
        driveWebViewLink=meta.get("driveWebViewLink"),
        driveUploadError=meta.get("driveUploadError"),
    )


@router.post(
    "/jobs",
    summary="Submit a PDF for async OCR",
    response_model=JobCreatedResponse,
    status_code=202,
)
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
    folder_id: str | None = Form(default=None),
    settings: Settings = get_settings(),
) -> JobCreatedResponse:
    job_manager = get_job_manager(settings)
    provisional_name = file.filename or "document.pdf"
    job_id = job_manager.create_job(
        provisional_name,
        output_filename=filename,
        folder_id=folder_id or None,
    )

    input_path = job_manager.input_path_for(job_id)
    original_filename, size = await save_upload_to_path(file, input_path, settings)
    job_manager.update_meta(job_id, originalFilename=original_filename)

    await job_manager.enqueue(job_id)
    logger.info("Queued OCR job %s for %s (%d bytes)", job_id, original_filename, size)

    status_url = str(request.url_for("get_job_status", job_id=job_id))
    return JobCreatedResponse(jobId=job_id, status="queued", statusUrl=status_url)


@router.get(
    "/jobs/{job_id}",
    summary="Get OCR job status",
    response_model=JobStatusResponse,
    operation_id="get_job_status",
)
async def get_job_status(
    job_id: str,
    request: Request,
    settings: Settings = get_settings(),
) -> JobStatusResponse:
    job_manager = get_job_manager(settings)
    meta = job_manager.get_status(job_id)
    return _job_status_response(job_id, meta, request)


@router.get(
    "/jobs/{job_id}/result",
    summary="Download OCR job result",
    response_class=FileResponse,
    operation_id="get_job_result",
)
async def get_job_result(
    job_id: str,
    settings: Settings = get_settings(),
) -> FileResponse:
    job_manager = get_job_manager(settings)
    meta = job_manager.get_status(job_id)
    output_path = job_manager.get_output_path(job_id)

    download_name = meta.get("outputFilename") or ocr_output_filename(meta["originalFilename"])
    return FileResponse(
        path=output_path,
        media_type="application/pdf",
        filename=download_name,
    )


@router.post(
    "/jobs/{job_id}/drive",
    summary="Upload a finished job's result to Google Drive",
    response_model=DriveUploadResponse,
    operation_id="upload_job_result_to_drive",
)
async def upload_job_to_drive(
    job_id: str,
    payload: JobDriveUploadRequest,
    settings: Settings = Depends(get_settings),
) -> DriveUploadResponse:
    target_folder = payload.folderId or settings.drive_shared_folder_id
    if not target_folder:
        raise ValidationError("folderId is required when DRIVE_SHARED_FOLDER_ID is not configured")

    job_manager = get_job_manager(settings)
    return await job_manager.upload_result_to_drive(
        job_id,
        target_folder,
        filename=payload.filename,
    )
