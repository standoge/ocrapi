from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"


JobStatusLiteral = Literal["queued", "running", "succeeded", "failed"]


class JobCreatedResponse(BaseModel):
    jobId: str = Field(..., description="Unique job identifier")
    status: JobStatusLiteral = Field(..., description="Initial job status")
    statusUrl: str = Field(..., description="URL to poll job status")


class JobStatusResponse(BaseModel):
    jobId: str = Field(..., description="Unique job identifier")
    status: JobStatusLiteral = Field(..., description="Current job status")
    pageCount: int | None = Field(default=None, description="Number of pages processed")
    error: str | None = Field(default=None, description="Error detail when status is failed")
    createdAt: str = Field(..., description="Job creation timestamp (ISO 8601)")
    startedAt: str | None = Field(default=None, description="Processing start timestamp (ISO 8601)")
    finishedAt: str | None = Field(default=None, description="Processing finish timestamp (ISO 8601)")
    resultUrl: str | None = Field(default=None, description="URL to download the OCR result PDF")
    driveFileId: str | None = Field(default=None, description="Google Drive file ID when uploaded")
    driveWebViewLink: str | None = Field(
        default=None,
        description="Google Drive view link when uploaded",
    )


class DriveUploadResponse(BaseModel):
    fileId: str = Field(..., description="Google Drive file ID")
    name: str = Field(..., description="Uploaded file name")
    webViewLink: str = Field(..., description="Link to view the file in Google Drive")


class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str
    instance: str | None = None
