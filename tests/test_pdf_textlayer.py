"""Unit tests for Document AI -> searchable PDF text-layer injection."""

from __future__ import annotations

from pathlib import Path

import fitz
from google.cloud.documentai_v1.types import document as documentai_document
from google.cloud.documentai_v1.types.geometry import NormalizedVertex

from ocr_documentai_plugin.pdf_textlayer import inject_text_layer


def _vertex(x: float, y: float) -> NormalizedVertex:
    return NormalizedVertex(x=x, y=y)


def _make_document() -> documentai_document.Document:
    document = documentai_document.Document(text="Hello world")
    page = documentai_document.Document.Page(page_number=1)

    token1 = documentai_document.Document.Page.Token()
    token1.layout.text_anchor.text_segments.append(
        documentai_document.Document.TextAnchor.TextSegment(start_index=0, end_index=5)
    )
    token1.layout.bounding_poly.normalized_vertices.extend(
        [_vertex(0.1, 0.1), _vertex(0.45, 0.1), _vertex(0.45, 0.2), _vertex(0.1, 0.2)]
    )
    token1.layout.confidence = 0.98
    page.tokens.append(token1)

    token2 = documentai_document.Document.Page.Token()
    token2.layout.text_anchor.text_segments.append(
        documentai_document.Document.TextAnchor.TextSegment(start_index=6, end_index=11)
    )
    token2.layout.bounding_poly.normalized_vertices.extend(
        [_vertex(0.5, 0.1), _vertex(0.9, 0.1), _vertex(0.9, 0.2), _vertex(0.5, 0.2)]
    )
    token2.layout.confidence = 0.92
    page.tokens.append(token2)

    document.pages.append(page)
    return document


def test_inject_text_layer_writes_searchable_pdf(tmp_path: Path):
    input_pdf = tmp_path / "input.pdf"
    output_pdf = tmp_path / "output.pdf"

    with fitz.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_text((72, 72), "visible anchor")
        pdf.save(input_pdf)

    processed_pages = inject_text_layer(input_pdf, [_make_document()], output_pdf)

    assert processed_pages == 1
    assert output_pdf.exists()

    with fitz.open(output_pdf) as pdf:
        text = pdf[0].get_text("text")

    assert "Hello" in text
    assert "world" in text
