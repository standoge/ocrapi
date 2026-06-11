"""Inject an invisible OCR text layer onto a PDF using PyMuPDF."""

from __future__ import annotations

from pathlib import Path

import fitz
from google.cloud.documentai_v1.types import document as documentai_document


def _text_from_anchor(document_text: str, text_anchor) -> str:
    if not text_anchor or not text_anchor.text_segments:
        return ""
    parts: list[str] = []
    for segment in text_anchor.text_segments:
        start = int(segment.start_index) if segment.start_index else 0
        end = int(segment.end_index) if segment.end_index else 0
        parts.append(document_text[start:end])
    return "".join(parts)


def _normalized_vertices(page_proto):
    bounding_poly = page_proto.layout.bounding_poly
    if bounding_poly.normalized_vertices:
        return bounding_poly.normalized_vertices
    if bounding_poly.vertices:
        dim = page_proto.dimension
        width = dim.width if dim and dim.width else 1
        height = dim.height if dim and dim.height else 1
        if width <= 0 or height <= 0:
            return bounding_poly.vertices
        from google.cloud.documentai_v1.types.geometry import NormalizedVertex

        return [
            NormalizedVertex(x=vertex.x / width, y=vertex.y / height)
            for vertex in bounding_poly.vertices
        ]
    return []


def _normalized_rect_to_pdf(page: fitz.Page, vertices) -> fitz.Rect:
    width = page.rect.width
    height = page.rect.height
    xs = [vertex.x * width for vertex in vertices]
    ys = [vertex.y * height for vertex in vertices]
    rect = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
    if page.rotation:
        rect = rect * page.derotation_matrix
    return rect


def _iter_tokens(document_text: str, page_proto):
    if page_proto.tokens:
        for token in page_proto.tokens:
            text = _text_from_anchor(document_text, token.layout.text_anchor).strip()
            if not text:
                continue
            vertices = _normalized_vertices(token)
            if not vertices:
                continue
            yield text, vertices
        return

    if page_proto.lines:
        for line in page_proto.lines:
            text = _text_from_anchor(document_text, line.layout.text_anchor).strip()
            if not text:
                continue
            vertices = _normalized_vertices(line)
            if not vertices:
                continue
            yield text, vertices


def _inject_page_tokens(
    pdf_page: fitz.Page,
    document_text: str,
    page_proto: documentai_document.Document.Page,
) -> None:
    for text, vertices in _iter_tokens(document_text, page_proto):
        rect = _normalized_rect_to_pdf(pdf_page, vertices)
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            continue
        fontsize = max(4.0, min(rect.height * 0.85, rect.width / max(len(text), 1)))
        overflow = pdf_page.insert_textbox(
            rect,
            text,
            fontsize=fontsize,
            fontname="helv",
            render_mode=3,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        if overflow >= 0:
            continue
        pdf_page.insert_text(
            rect.bl,
            text,
            fontsize=fontsize,
            fontname="helv",
            render_mode=3,
        )


def _collect_page_entries(
    documents: list[documentai_document.Document],
    *,
    page_offset: int = 0,
) -> list[tuple[int, str, documentai_document.Document.Page]]:
    page_entries: list[tuple[int, str, documentai_document.Document.Page]] = []
    for document in documents:
        document_text = document.text or ""
        local_fallback = 1
        for page_proto in document.pages:
            local_page_number = page_proto.page_number or local_fallback
            page_number = page_offset + local_page_number
            page_entries.append((page_number, document_text, page_proto))
            local_fallback = local_page_number + 1
    return page_entries


def _write_text_layer(
    input_pdf: Path,
    page_entries: list[tuple[int, str, documentai_document.Document.Page]],
    output_pdf: Path,
) -> int:
    page_entries.sort(key=lambda item: item[0])

    with fitz.open(input_pdf) as pdf:
        if len(pdf) == 0:
            raise ValueError("PDF contains no pages")

        for page_number, document_text, page_proto in page_entries:
            page_index = page_number - 1
            if page_index < 0 or page_index >= len(pdf):
                continue
            _inject_page_tokens(pdf[page_index], document_text, page_proto)

        output_pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.save(output_pdf, garbage=4, deflate=True)

    return len(page_entries)


def inject_text_layer(
    input_pdf: Path,
    documents: list[documentai_document.Document],
    output_pdf: Path,
) -> int:
    """Write a searchable PDF by overlaying invisible text from Document AI output."""
    page_entries = _collect_page_entries(documents)
    return _write_text_layer(input_pdf, page_entries, output_pdf)


def inject_text_layer_chunks(
    input_pdf: Path,
    chunk_results: list[tuple[int, documentai_document.Document]],
    output_pdf: Path,
) -> int:
    """Write a searchable PDF from chunked Document AI results.

    *chunk_results* is a list of ``(start_index, document)`` tuples where
    *start_index* is the zero-based page index of the first page in the chunk
    within the source PDF.
    """
    page_entries: list[tuple[int, str, documentai_document.Document.Page]] = []
    for start_index, document in chunk_results:
        page_entries.extend(
            _collect_page_entries([document], page_offset=start_index)
        )
    return _write_text_layer(input_pdf, page_entries, output_pdf)
