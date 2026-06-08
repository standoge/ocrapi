"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="session", autouse=True)
def _test_env() -> None:
    os.environ.setdefault("GCP_PROJECT_ID", "test-project")
    os.environ.setdefault("GCP_LOCATION", "us")
    os.environ.setdefault("GCP_PROCESSOR_ID", "test-processor")
    os.environ.setdefault("DRIVE_SHARED_FOLDER_ID", "test-folder-id")
    os.environ.setdefault("MAX_UPLOAD_BYTES", "20971520")
    os.environ.setdefault("MAX_PDF_PAGES", "2000")
    os.environ.setdefault("SYNC_MAX_PAGES", "15")
    os.environ.setdefault("OCR_WORKER_CONCURRENCY", "1")
    os.environ.setdefault("OCRMYPDF_JOBS", "1")
    os.environ.setdefault("LOG_LEVEL", "WARNING")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(tmp_path):
    from app.config import get_settings
    from app.main import app
    from app.services.job_manager import get_job_manager, reset_job_manager

    os.environ["JOBS_DIR"] = str(tmp_path / "jobs")
    get_settings.cache_clear()
    reset_job_manager()

    settings = get_settings()
    job_manager = get_job_manager(settings)
    await job_manager.start()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await job_manager.stop()
    get_settings.cache_clear()
    reset_job_manager()
