"""OCR pipeline service using Document AI and PyMuPDF."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from google.cloud.documentai_v1.types import document as documentai_document
from pypdf import PdfReader

from app.config import Settings
from app.exceptions import UnprocessablePdfError, UpstreamServiceError
from ocr_documentai_plugin.documentai_client import get_documentai_client
from ocr_documentai_plugin.gcs_client import get_gcs_client
from ocr_documentai_plugin.pdf_split import split_pdf
from ocr_documentai_plugin.pdf_textlayer import inject_text_layer, inject_text_layer_chunks

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
    if "storage.objects" in message or "storage.buckets" in message:
        sa_email = settings.service_account_email or "unknown service account"
        return (
            "GCP Cloud Storage permission denied for batch OCR. Grant "
            f"'roles/storage.objectAdmin' on bucket '{settings.gcs_bucket}' to "
            f"'{sa_email}'."
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


def _run_online_ocr_sync(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    max_pages: int | None = None,
    job_id: str | None = None,
    original_filename: str | None = None,
) -> int:
    page_count = _validate_pdf(input_path, settings, max_pages=max_pages)
    filename = original_filename or input_path.name
    log_prefix = f"[job={job_id[:8]} | {filename}] " if job_id else f"[{filename}] "
    logger.info("%sRunning online Document AI OCR (%d pages)", log_prefix, page_count)

    pdf_bytes = input_path.read_bytes()
    document = get_documentai_client(settings).process_pdf_online(pdf_bytes)
    processed_pages = inject_text_layer(input_path, [document], output_path)
    logger.info("%sOnline OCR completed (%d pages)", log_prefix, processed_pages)
    return page_count


def _process_chunk_online(
    chunk_index: int,
    start_index: int,
    chunk_bytes: bytes,
    settings: Settings,
) -> tuple[int, int, documentai_document.Document]:
    document = get_documentai_client(settings).process_pdf_online(chunk_bytes)
    return chunk_index, start_index, document


def _run_chunked_online_sync(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    job_id: str | None = None,
    original_filename: str | None = None,
) -> int:
    page_count = _validate_pdf(input_path, settings)
    filename = original_filename or input_path.name
    log_prefix = f"[job={job_id[:8]} | {filename}] " if job_id else f"[{filename}] "

    chunks = split_pdf(
        input_path,
        max_pages=settings.online_chunk_pages,
        max_bytes=settings.online_chunk_max_bytes,
    )
    logger.info(
        "%sRunning chunked online Document AI OCR (%d pages, %d chunks, concurrency=%d)",
        log_prefix,
        page_count,
        len(chunks),
        settings.online_max_concurrency,
    )

    ordered_results: list[tuple[int, documentai_document.Document] | None] = [
        None
    ] * len(chunks)
    max_workers = min(settings.online_max_concurrency, len(chunks))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _process_chunk_online,
                chunk_index,
                start_index,
                chunk_bytes,
                settings,
            ): chunk_index
            for chunk_index, (start_index, chunk_bytes) in enumerate(chunks)
        }
        for future in as_completed(futures):
            chunk_index, start_index, document = future.result()
            ordered_results[chunk_index] = (start_index, document)

    chunk_results = [
        result for result in ordered_results if result is not None
    ]
    if len(chunk_results) != len(chunks):
        raise UpstreamServiceError("OCR pipeline failed: missing chunk results")

    processed_pages = inject_text_layer_chunks(input_path, chunk_results, output_path)
    logger.info("%sChunked online OCR completed (%d pages)", log_prefix, processed_pages)
    return page_count


def _run_batch_ocr_sync(
    input_path: Path,
    output_path: Path,
    settings: Settings,
    *,
    job_id: str | None = None,
    original_filename: str | None = None,
) -> int:
    page_count = _validate_pdf(input_path, settings)
    filename = original_filename or input_path.name
    if not job_id:
        raise UpstreamServiceError("Batch OCR requires a job_id")

    log_prefix = f"[job={job_id[:8]} | {filename}] "
    gcs_client = get_gcs_client(settings)
    docai_client = get_documentai_client(settings)

    input_prefix = f"{job_id}/input"
    output_prefix = f"{job_id}/output"
    input_blob = f"{input_prefix}/input.pdf"

    logger.info("%sUploading input PDF to GCS (%d pages)", log_prefix, page_count)
    gcs_client.upload_file(input_path, input_blob)

    try:
        documents = docai_client.batch_process_from_gcs(
            gcs_client.gcs_prefix(input_prefix),
            gcs_client.gcs_prefix(output_prefix),
            gcs_client=gcs_client,
        )
        processed_pages = inject_text_layer(input_path, documents, output_path)
        logger.info("%sBatch OCR completed (%d pages)", log_prefix, processed_pages)
    finally:
        logger.info("%sCleaning up GCS scratch prefix %s", log_prefix, job_id)
        gcs_client.delete_prefix(f"{job_id}/")

    return page_count


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
    try:
        page_count = _run_chunked_online_sync(
            input_path,
            output_path,
            settings,
            job_id=job_id,
            original_filename=original_filename,
        )
    except Exception as exc:
        raise UpstreamServiceError(_format_ocr_failure(exc, settings)) from exc

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

        try:
            page_count = await asyncio.to_thread(
                _run_online_ocr_sync,
                input_path,
                output_path,
                settings,
                max_pages=settings.sync_max_pages,
            )
        except Exception as exc:
            raise UpstreamServiceError(_format_ocr_failure(exc, settings)) from exc

        if not output_path.exists():
            raise UpstreamServiceError("OCR completed but output PDF was not created")

        return output_path.read_bytes(), page_count
