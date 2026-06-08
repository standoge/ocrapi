"""ocrmypdf plugin that uses GCP Document AI as the OCR engine."""

from ocrmypdf import hookimpl
from ocrmypdf.builtin_plugins.tesseract_ocr import TesseractOptions

from app.services.ocr_progress import LoggingProgressBar
from ocr_documentai_plugin.engine import DocumentAIOcrEngine


@hookimpl
def initialize(plugin_manager):
    # Disable Tesseract OCR engine/checks; Document AI is the only OCR backend.
    plugin_manager.set_blocked("ocrmypdf.builtin_plugins.tesseract_ocr")


@hookimpl
def register_options():
    # Keep TesseractOptions registered so ocrmypdf's OcrOptions model stays valid.
    return {"tesseract": TesseractOptions}


@hookimpl
def get_ocr_engine(options=None):
    if options is not None:
        ocr_engine = getattr(options, "ocr_engine", "auto")
        if ocr_engine not in ("auto", "documentai"):
            return None
    return DocumentAIOcrEngine()


@hookimpl
def get_progressbar_class():
    return LoggingProgressBar
