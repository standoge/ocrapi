"""Unit tests for PDF splitting."""

from __future__ import annotations

from pathlib import Path

import fitz

from ocr_documentai_plugin.pdf_split import split_pdf


def _make_pdf(path: Path, page_count: int) -> None:
    with fitz.open() as pdf:
        for index in range(page_count):
            page = pdf.new_page(width=612, height=792)
            page.insert_text((72, 72), f"page-{index + 1}")
        pdf.save(path)


def test_split_pdf_respects_page_limit(tmp_path: Path):
    input_pdf = tmp_path / "input.pdf"
    _make_pdf(input_pdf, 5)

    chunks = split_pdf(input_pdf, max_pages=2, max_bytes=10 * 1024 * 1024)

    assert [start for start, _ in chunks] == [0, 2, 4]
    assert [_chunk_page_count(chunk_bytes) for _, chunk_bytes in chunks] == [2, 2, 1]


def test_split_pdf_respects_byte_limit(tmp_path: Path):
    input_pdf = tmp_path / "input.pdf"
    _make_pdf(input_pdf, 3)

    first_page_bytes = _page_range_bytes(input_pdf, 0, 0)
    max_bytes = len(first_page_bytes) + 100

    chunks = split_pdf(input_pdf, max_pages=10, max_bytes=max_bytes)

    assert len(chunks) == 3
    assert chunks[0][0] == 0
    assert chunks[1][0] == 1
    assert chunks[2][0] == 2


def _page_range_bytes(input_pdf: Path, from_page: int, to_page: int) -> bytes:
    with fitz.open(input_pdf) as src, fitz.open() as dst:
        dst.insert_pdf(src, from_page=from_page, to_page=to_page)
        return dst.tobytes(garbage=3, deflate=True)


def _chunk_page_count(chunk_bytes: bytes) -> int:
    with fitz.open(stream=chunk_bytes, filetype="pdf") as pdf:
        return len(pdf)
