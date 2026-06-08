"""Document AI OCR engine implementation for ocrmypdf."""

from __future__ import annotations

import tempfile
from pathlib import Path

from PIL import Image

from app.config import Settings, get_settings
from ocr_documentai_plugin.documentai_client import DocumentAIClient, get_documentai_client
from ocr_documentai_plugin.hocr import document_to_hocr
from ocrmypdf.fpdf_renderer import Fpdf2PdfRenderer
from ocrmypdf.font import MultiFontManager
from ocrmypdf.hocrtransform import HocrParser
from ocrmypdf.pluginspec import OcrEngine, OrientationConfidence


class DocumentAIOcrEngine(OcrEngine):
    """OCR engine backed by GCP Document AI Document OCR processor."""

    @staticmethod
    def version() -> str:
        return "documentai-1.0"

    @staticmethod
    def creator_tag(options) -> str:
        return "Document AI OCR"

    def __str__(self) -> str:
        return f"Document AI OCR {self.version()}"

    @staticmethod
    def languages(options) -> set[str]:
        return {"eng"}

    @staticmethod
    def get_orientation(input_file: Path, options) -> OrientationConfidence:
        return OrientationConfidence(angle=0, confidence=0.0)

    @staticmethod
    def _settings() -> Settings:
        return get_settings()

    @classmethod
    def _client(cls) -> DocumentAIClient:
        return get_documentai_client(cls._settings())

    @classmethod
    def _process_page_image(cls, input_file: Path):
        image_bytes = input_file.read_bytes()
        mime_type = "image/png"
        suffix = input_file.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif suffix in {".tif", ".tiff"}:
            mime_type = "image/tiff"

        with Image.open(input_file) as image:
            width, height = image.size

        document = cls._client().process_image(image_bytes, mime_type=mime_type)
        return document, width, height

    @classmethod
    def generate_hocr(
        cls,
        input_file: Path,
        output_hocr: Path,
        output_text: Path,
        options,
    ) -> None:
        document, width, height = cls._process_page_image(input_file)
        hocr_xml, sidecar_text = document_to_hocr(document, width, height)
        output_hocr.write_text(hocr_xml, encoding="utf-8")
        output_text.write_text(sidecar_text, encoding="utf-8")

    @classmethod
    def generate_pdf(
        cls,
        input_file: Path,
        output_pdf: Path,
        output_text: Path,
        options,
    ) -> None:
        with tempfile.NamedTemporaryFile(suffix=".hocr", delete=False) as handle:
            temp_hocr = Path(handle.name)

        try:
            cls.generate_hocr(input_file, temp_hocr, output_text, options)
            page_element = HocrParser(temp_hocr).parse()
            dpi = getattr(options, "image_dpi", 300)
            renderer = Fpdf2PdfRenderer(
                page=page_element,
                dpi=dpi,
                multi_font_manager=MultiFontManager(),
                invisible_text=True,
            )
            renderer.render(output_pdf)
        finally:
            temp_hocr.unlink(missing_ok=True)
