"""Unit tests for Document AI -> hOCR conversion."""

from __future__ import annotations

from google.cloud.documentai_v1.types import document as documentai_document
from google.cloud.documentai_v1.types.geometry import NormalizedVertex

from ocr_documentai_plugin.hocr import document_to_hocr


def _vertex(x: float, y: float) -> NormalizedVertex:
    return NormalizedVertex(x=x, y=y)


def _make_document() -> documentai_document.Document:
    document = documentai_document.Document(text="Hello world")
    page = documentai_document.Document.Page(page_number=1)

    line = documentai_document.Document.Page.Line()
    line.layout.text_anchor.text_segments.append(
        documentai_document.Document.TextAnchor.TextSegment(start_index=0, end_index=11)
    )
    line.layout.bounding_poly.normalized_vertices.extend(
        [_vertex(0.1, 0.1), _vertex(0.9, 0.1), _vertex(0.9, 0.2), _vertex(0.1, 0.2)]
    )
    line.layout.confidence = 0.95
    page.lines.append(line)

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


def test_document_to_hocr_contains_words_and_sidecar():
    document = _make_document()
    hocr_xml, sidecar = document_to_hocr(document, image_width=1000, image_height=2000)

    assert "ocrx_word" in hocr_xml
    assert "Hello" in hocr_xml
    assert "world" in hocr_xml
    assert "x_wconf 98" in hocr_xml
    assert "bbox 100 200 449 400" in hocr_xml
    assert "Hello world" in sidecar
