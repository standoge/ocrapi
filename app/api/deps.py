"""Shared helpers for API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile

from app.config import Settings
from app.exceptions import PayloadTooLargeError, ValidationError

_CHUNK_SIZE = 1024 * 1024


def _normalize_pdf_filename(filename: str) -> str:
    if not filename.lower().endswith(".pdf"):
        return f"{filename}.pdf"
    return filename


def _validate_pdf_upload(upload: UploadFile) -> str:
    if upload.content_type not in (None, "application/pdf", "application/x-pdf"):
        raise ValidationError("Uploaded file must be a PDF")

    filename = upload.filename or "document.pdf"
    return _normalize_pdf_filename(filename)


async def save_upload_to_path(
    upload: UploadFile,
    dest_path: Path,
    settings: Settings,
) -> tuple[str, int]:
    """Stream an uploaded PDF to disk without buffering the full file in memory."""
    filename = _validate_pdf_upload(upload)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with dest_path.open("wb") as handle:
        while True:
            chunk = await upload.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > settings.max_upload_bytes:
                dest_path.unlink(missing_ok=True)
                raise PayloadTooLargeError(
                    f"File exceeds maximum size of {settings.max_upload_bytes} bytes"
                )
            handle.write(chunk)

    if total == 0:
        dest_path.unlink(missing_ok=True)
        raise ValidationError("Uploaded file is empty")

    return filename, total


async def read_and_validate_pdf(upload: UploadFile, settings: Settings) -> tuple[bytes, str]:
    filename = _validate_pdf_upload(upload)

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > settings.max_upload_bytes:
            raise PayloadTooLargeError(
                f"File exceeds maximum size of {settings.max_upload_bytes} bytes"
            )
        chunks.append(chunk)

    if total == 0:
        raise ValidationError("Uploaded file is empty")

    return b"".join(chunks), filename


def ocr_output_filename(original_filename: str) -> str:
    stem = original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
    return f"{stem}_ocr.pdf"
