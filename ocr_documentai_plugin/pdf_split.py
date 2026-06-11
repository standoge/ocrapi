"""Split a PDF into byte-aware chunks for online Document AI processing."""

from __future__ import annotations

from pathlib import Path

import fitz


def split_pdf(
    input_pdf: Path,
    *,
    max_pages: int,
    max_bytes: int,
) -> list[tuple[int, bytes]]:
    """Split *input_pdf* into chunks suitable for online ``process_document``.

    Each chunk is a tuple of ``(start_index, pdf_bytes)`` where *start_index* is
    the zero-based page index of the first page in the chunk within the source
    PDF. Chunks respect both *max_pages* and *max_bytes* (whichever limit is hit
    first). A single page that exceeds *max_bytes* on its own still becomes its
    own chunk so callers can surface a clear upstream error.
    """
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    chunks: list[tuple[int, bytes]] = []

    with fitz.open(input_pdf) as src:
        total_pages = len(src)
        if total_pages == 0:
            raise ValueError("PDF contains no pages")

        start = 0
        while start < total_pages:
            end = start
            chunk_bytes: bytes | None = None

            while end < total_pages:
                candidate_end = end
                page_count = candidate_end - start + 1
                if page_count > max_pages:
                    break

                candidate_bytes = _extract_pages(src, start, candidate_end)
                if len(candidate_bytes) > max_bytes:
                    if candidate_end == start:
                        chunks.append((start, candidate_bytes))
                        start = candidate_end + 1
                        chunk_bytes = None
                        break
                    break

                chunk_bytes = candidate_bytes
                end = candidate_end + 1

            if chunk_bytes is not None:
                chunks.append((start, chunk_bytes))
                start = end

    return chunks


def _extract_pages(src: fitz.Document, from_page: int, to_page: int) -> bytes:
    with fitz.open() as dst:
        dst.insert_pdf(src, from_page=from_page, to_page=to_page)
        return dst.tobytes(garbage=3, deflate=True)
