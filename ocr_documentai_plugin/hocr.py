"""Convert Document AI responses into hOCR."""

from __future__ import annotations

import html
from xml.etree import ElementTree as ET

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


def _normalized_bbox_to_pixels(vertices, width: int, height: int) -> tuple[int, int, int, int]:
    xs = [vertex.x for vertex in vertices]
    ys = [vertex.y for vertex in vertices]
    left = int(min(xs) * width)
    top = int(min(ys) * height)
    right = int(max(xs) * width)
    bottom = int(max(ys) * height)
    return left, top, right, bottom


def _confidence_to_wconf(confidence: float) -> int:
    return max(0, min(100, int(round(confidence * 100))))


def document_to_hocr(
    document: documentai_document.Document,
    image_width: int,
    image_height: int,
    page_number: int = 1,
) -> tuple[str, str]:
    """Build hOCR XML and plain sidecar text from a Document AI response."""
    if not document.pages:
        empty = _empty_hocr(page_number, image_width, image_height)
        return empty, ""

    page = document.pages[0]
    document_text = document.text or ""

    root = ET.Element(
        "html",
        {
            "xmlns": "http://www.w3.org/1999/xhtml",
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:ocr": "http://www.w3.org/2000/10/ocr-xhtml",
            "xmlns:ocrp": "http://www.w3.org/2000/10/ocr-xhtml",
            "xmlns:ocrx": "http://www.w3.org/2000/10/ocr-xhtml",
            "xsi:schemaLocation": (
                "http://www.w3.org/1999/xhtml "
                "http://www.w3.org/2000/10/ocr-xhtml/ocrxhtml.xsd"
            ),
        },
    )
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "OCR Output"
    ET.SubElement(head, "meta", {"content": "Document AI OCR", "name": "ocr-system"})
    ET.SubElement(head, "meta", {"content": "ocr_page ocr_carea ocr_par ocr_line ocrx_word", "name": "ocr-capabilities"})
    body = ET.SubElement(root, "body")
    page_div = ET.SubElement(
        body,
        "div",
        {
            "class": "ocr_page",
            "id": f"page_{page_number}",
            "title": f"image \"\"; bbox 0 0 {image_width} {image_height}; ppageno {page_number - 1}",
        },
    )
    carea = ET.SubElement(
        page_div,
        "div",
        {
            "class": "ocr_carea",
            "id": "block_1_1",
            "title": f"bbox 0 0 {image_width} {image_height}",
        },
    )
    # ocrmypdf's HocrParser only walks the standard hOCR nesting
    # (ocr_page > ocr_par > ocr_line > ocrx_word) and its fpdf2 renderer only
    # draws paragraphs/lines. Words must live inside a <span class="ocr_line">
    # that lives inside a <p class="ocr_par">, or no text layer is produced.
    par = ET.SubElement(
        carea,
        "p",
        {
            "class": "ocr_par",
            "id": "par_1_1",
            "title": f"bbox 0 0 {image_width} {image_height}",
        },
    )

    line_texts: list[str] = []

    if page.lines:
        for line_index, line in enumerate(page.lines, start=1):
            line_text = _text_from_anchor(document_text, line.layout.text_anchor).strip()
            if not line_text:
                continue
            line_texts.append(line_text)
            vertices = line.layout.bounding_poly.normalized_vertices
            bbox = _normalized_bbox_to_pixels(vertices, image_width, image_height)
            line_span = ET.SubElement(
                par,
                "span",
                {
                    "class": "ocr_line",
                    "id": f"line_1_{line_index}",
                    "title": f"bbox {' '.join(str(v) for v in bbox)}; baseline 0 0",
                },
            )
            for token_index, token in enumerate(
                _tokens_for_line(document_text, page.tokens, line), start=1
            ):
                token_text = _text_from_anchor(document_text, token.layout.text_anchor)
                if not token_text:
                    continue
                token_vertices = token.layout.bounding_poly.normalized_vertices
                token_bbox = _normalized_bbox_to_pixels(token_vertices, image_width, image_height)
                confidence = token.layout.confidence if token.layout.confidence else 0.0
                word_span = ET.SubElement(
                    line_span,
                    "span",
                    {
                        "class": "ocrx_word",
                        "id": f"word_1_{line_index}_{token_index}",
                        "title": (
                            f"bbox {' '.join(str(v) for v in token_bbox)}; "
                            f"x_wconf {_confidence_to_wconf(confidence)}"
                        ),
                    },
                )
                word_span.text = html.escape(token_text)
    elif page.tokens:
        line_span = ET.SubElement(
            par,
            "span",
            {
                "class": "ocr_line",
                "id": "line_1_1",
                "title": f"bbox 0 0 {image_width} {image_height}; baseline 0 0",
            },
        )
        for token_index, token in enumerate(page.tokens, start=1):
            token_text = _text_from_anchor(document_text, token.layout.text_anchor)
            if not token_text:
                continue
            line_texts.append(token_text)
            token_vertices = token.layout.bounding_poly.normalized_vertices
            token_bbox = _normalized_bbox_to_pixels(token_vertices, image_width, image_height)
            confidence = token.layout.confidence if token.layout.confidence else 0.0
            word_span = ET.SubElement(
                line_span,
                "span",
                {
                    "class": "ocrx_word",
                    "id": f"word_1_1_{token_index}",
                    "title": (
                        f"bbox {' '.join(str(v) for v in token_bbox)}; "
                        f"x_wconf {_confidence_to_wconf(confidence)}"
                    ),
                },
            )
            word_span.text = html.escape(token_text)

    sidecar_text = "\n".join(line_texts) if line_texts else document_text.strip()
    hocr_bytes = ET.tostring(root, encoding="unicode", method="xml")
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n{hocr_bytes}", sidecar_text


def _tokens_for_line(document_text: str, tokens, line):
    if not tokens:
        return []
    line_text = _text_from_anchor(document_text, line.layout.text_anchor)
    if not line_text:
        return tokens
    matched = []
    for token in tokens:
        token_text = _text_from_anchor(document_text, token.layout.text_anchor)
        if token_text and token_text in line_text:
            matched.append(token)
    return matched or tokens


def _empty_hocr(page_number: int, width: int, height: int) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<html xmlns="http://www.w3.org/1999/xhtml">'
        f'<body><div class="ocr_page" id="page_{page_number}" '
        f'title="image ""; bbox 0 0 {width} {height}; ppageno {page_number - 1}"></div>'
        f"</body></html>"
    )
