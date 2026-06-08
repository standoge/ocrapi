"""Tests for OCR progress logging."""

from __future__ import annotations

import logging

from app.services.ocr_progress import LoggingProgressBar, ocr_context


def test_logging_progress_bar_emits_updates(caplog):
    caplog.set_level(logging.INFO, logger="app.services.ocr_progress")
    token = ocr_context.set({"job_id": "abc-123", "filename": "sample.pdf"})

    try:
        with LoggingProgressBar(total=10, desc="OCR", unit="page", disable=False) as bar:
            bar.update(completed=5)
            bar.update(completed=10)
    finally:
        ocr_context.reset(token)

    messages = [record.message for record in caplog.records]
    assert any("sample.pdf" in message and "starting" in message for message in messages)
    assert any("OCR: 5/10 pages (50%)" in message for message in messages)
    assert any("completed (10/10 pages)" in message for message in messages)
