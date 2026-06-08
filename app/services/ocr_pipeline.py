"""OCR pipeline service wrapping ocrmypdf."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import ocrmypdf
from pypdf import PdfReader

from app.config import Settings
from app.exceptions import UnprocessablePdfError, UpstreamServiceError
from app.services.ocr_progress import ocr_context

logger = logging.getLogger(__name__)


def _format_ocr_failure(exc: Exception, settings: Settings) -> str:
    message = str(exc)
    if "documentai.processors.processOnline" in message or "IAM_PERMISSION_DENIED" in message:
        sa_email = settings.service_account_email or "unknown service account"
        return (
            "GCP Document AI permission denied. Grant role 'Document AI API User' "
            f"({settings.processor_resource_name}) to '{sa_email}' on project "
            f"'{settings.gcp_project_id}'. "
            "If the service account belongs to another project, add the role on the "
            "processor project explicitly."
        )
    return f"OCR pipeline failed: {exc}"


def _validate_pdf(
    input_path: Path,
    settings: Settings,
    *,
    max_pages: int | None = None,
) -> int:
    limit = max_pages if max_pages is not None else settings.max_pdf_pages
    try:
        reader = PdfReader(str(input_path))
        page_count = len(reader.pages)
    except Exception as exc:
        raise UnprocessablePdfError(f"Invalid or corrupted PDF: {exc}") from exc

    if page_count == 0:
        raise UnprocessablePdfError("PDF contains no pages")

    if page_count > limit:
        raise UnprocessablePdfError(
            f"PDF has {page_count} pages; maximum allowed is {limit}"
        )

    return page_count


def _run_ocr_sync(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    output_type: str | None = None,
    jobs: int | None = None,
    progress_context: dict[str, str] | None = None,
) -> None:
    token = None
    if progress_context:
        token = ocr_context.set(progress_context)

    try:
        logger.info(
            "Running ocrmypdf on %s -> %s",
            input_path.name,
            output_path.name,
        )
        ocrmypdf.ocr(
            str(input_path),
            str(output_path),
            force_ocr=True,
            output_type=output_type or settings.default_output_type,
            progress_bar=settings.ocr_progress_logging,
            optimize=0,
            jobs=jobs if jobs is not None else settings.ocrmypdf_jobs,
            ocr_engine="documentai",
        )
    except ocrmypdf.exceptions.PriorOcrFoundError as exc:
        raise UnprocessablePdfError(str(exc)) from exc
    except ocrmypdf.exceptions.EncryptedPdfError as exc:
        raise UnprocessablePdfError(f"Encrypted PDF is not supported: {exc}") from exc
    except ocrmypdf.exceptions.MissingDependencyError as exc:
        raise UpstreamServiceError(
            f"Missing system dependency for OCR pipeline: {exc}. "
            "Install Ghostscript and qpdf (see README). Document AI is used for OCR; Tesseract is not required."
        ) from exc
    except Exception as exc:
        raise UpstreamServiceError(_format_ocr_failure(exc, settings)) from exc
    finally:
        if token is not None:
            ocr_context.reset(token)


def run_ocr_pipeline_file(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    max_pages: int | None = None,
    job_id: str | None = None,
    original_filename: str | None = None,
) -> int:
    """Run OCR on a PDF file and write the searchable PDF to disk."""
    page_count = _validate_pdf(input_path, settings, max_pages=max_pages)
    progress_context = {
        "filename": original_filename or input_path.name,
    }
    if job_id:
        progress_context["job_id"] = job_id

    logger.info(
        "Validated %s (%d pages)",
        progress_context["filename"],
        page_count,
    )
    _run_ocr_sync(
        input_path,
        output_path,
        settings,
        progress_context=progress_context,
    )

    if not output_path.exists():
        raise UpstreamServiceError("OCR completed but output PDF was not created")

    return page_count


async def run_ocr_pipeline(pdf_bytes: bytes, settings: Settings) -> tuple[bytes, int]:
    """Run OCR on PDF bytes and return searchable PDF bytes and page count."""
    with tempfile.TemporaryDirectory(prefix="ocrapi_") as tmpdir:
        tmp = Path(tmpdir)
        input_path = tmp / "input.pdf"
        output_path = tmp / "output.pdf"
        input_path.write_bytes(pdf_bytes)

        page_count = _validate_pdf(
            input_path,
            settings,
            max_pages=settings.sync_max_pages,
        )
        await asyncio.to_thread(
            _run_ocr_sync,
            input_path,
            output_path,
            settings,
            progress_context={"filename": "upload.pdf"},
        )

        if not output_path.exists():
            raise UpstreamServiceError("OCR completed but output PDF was not created")

        return output_path.read_bytes(), page_count
