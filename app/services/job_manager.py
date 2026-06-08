"""Async OCR job queue with filesystem-backed job store."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.config import Settings
from app.exceptions import NotFoundError, ValidationError
from app.services import ocr_pipeline
from app.services.drive_client import get_drive_client

logger = logging.getLogger(__name__)

_job_manager: JobManager | None = None


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class JobManager:
    """Manages OCR jobs with a bounded worker pool and on-disk persistence."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jobs_dir = settings.resolved_jobs_dir
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._shutdown = False

    def _job_dir(self, job_id: str) -> Path:
        return self._jobs_dir / job_id

    def _meta_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "meta.json"

    def _input_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "input.pdf"

    def _output_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "output.pdf"

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _read_meta(self, job_id: str) -> dict[str, Any]:
        path = self._meta_path(job_id)
        if not path.exists():
            raise NotFoundError(f"Job {job_id} not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_meta(self, job_id: str, meta: dict[str, Any]) -> None:
        self._meta_path(job_id).write_text(
            json.dumps(meta, indent=2),
            encoding="utf-8",
        )

    def create_job(
        self,
        original_filename: str,
        *,
        output_filename: str | None = None,
        folder_id: str | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        self._job_dir(job_id).mkdir(parents=True, exist_ok=False)
        meta = {
            "jobId": job_id,
            "status": JobStatus.QUEUED,
            "originalFilename": original_filename,
            "outputFilename": output_filename,
            "folderId": folder_id,
            "pageCount": None,
            "error": None,
            "driveFileId": None,
            "driveWebViewLink": None,
            "driveUploadError": None,
            "createdAt": self._now_iso(),
            "startedAt": None,
            "finishedAt": None,
        }
        self._write_meta(job_id, meta)
        return job_id

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def get_status(self, job_id: str) -> dict[str, Any]:
        return self._read_meta(job_id)

    def update_meta(self, job_id: str, **fields: Any) -> dict[str, Any]:
        meta = self._read_meta(job_id)
        meta.update(fields)
        self._write_meta(job_id, meta)
        return meta

    def get_output_path(self, job_id: str) -> Path:
        meta = self._read_meta(job_id)
        if meta["status"] != JobStatus.SUCCEEDED:
            raise ValidationError(
                f"Job {job_id} is not ready for download (status={meta['status']})"
            )
        output = self._output_path(job_id)
        if not output.exists():
            raise NotFoundError(f"Output file for job {job_id} not found")
        return output

    def input_path_for(self, job_id: str) -> Path:
        return self._input_path(job_id)

    def _collect_restartable_job_ids(self) -> list[str]:
        job_ids: list[str] = []
        for job_dir in sorted(self._jobs_dir.iterdir()):
            if not job_dir.is_dir():
                continue
            meta_path = job_dir / "meta.json"
            if not meta_path.exists():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            status = meta.get("status")
            job_id = meta["jobId"]
            if status == JobStatus.QUEUED:
                job_ids.append(job_id)
            elif status == JobStatus.RUNNING:
                meta["status"] = JobStatus.QUEUED
                meta["startedAt"] = None
                self._write_meta(job_id, meta)
                job_ids.append(job_id)
                logger.info("Re-queued interrupted job %s", job_id)
        return job_ids

    async def start(self) -> None:
        for job_id in self._collect_restartable_job_ids():
            await self._queue.put(job_id)

        concurrency = self._settings.ocr_worker_concurrency
        for worker_id in range(concurrency):
            task = asyncio.create_task(self._worker(worker_id), name=f"ocr-worker-{worker_id}")
            self._workers.append(task)
        logger.info("Started %d OCR worker(s)", concurrency)

    async def stop(self) -> None:
        self._shutdown = True
        for _ in self._workers:
            await self._queue.put(None)
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _worker(self, worker_id: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if job_id is None or self._shutdown:
                    break
                await self._process_job(job_id)
            except Exception:
                logger.exception("Worker %d failed processing job %s", worker_id, job_id)
            finally:
                self._queue.task_done()

    async def _process_job(self, job_id: str) -> None:
        meta = self._read_meta(job_id)
        filename = meta["originalFilename"]
        meta["status"] = JobStatus.RUNNING
        meta["startedAt"] = self._now_iso()
        meta["error"] = None
        self._write_meta(job_id, meta)
        logger.info("Job %s started: %s", job_id, filename)

        input_path = self._input_path(job_id)
        output_path = self._output_path(job_id)

        try:
            page_count = await asyncio.to_thread(
                ocr_pipeline.run_ocr_pipeline_file,
                input_path,
                output_path,
                self._settings,
                job_id=job_id,
                original_filename=filename,
            )
            meta = self._read_meta(job_id)
            meta["status"] = JobStatus.SUCCEEDED
            meta["pageCount"] = page_count
            meta["finishedAt"] = self._now_iso()
            self._write_meta(job_id, meta)
            logger.info(
                "Job %s succeeded: %s (%d pages)",
                job_id,
                filename,
                page_count,
            )

            folder_id = meta.get("folderId")
            if folder_id:
                logger.info("Job %s uploading result to Drive folder %s", job_id, folder_id)
                await self._upload_to_drive(job_id, output_path, meta, folder_id)
        except Exception as exc:
            meta = self._read_meta(job_id)
            meta["status"] = JobStatus.FAILED
            meta["error"] = str(exc)
            meta["finishedAt"] = self._now_iso()
            self._write_meta(job_id, meta)
            logger.error("Job %s failed: %s", job_id, exc)

    async def _upload_to_drive(
        self,
        job_id: str,
        output_path: Path,
        meta: dict[str, Any],
        folder_id: str,
    ) -> None:
        output_name = meta.get("outputFilename")
        if not output_name:
            stem = Path(meta["originalFilename"]).stem
            output_name = f"{stem}_ocr.pdf"

        try:
            drive_client = get_drive_client(self._settings)
            result = await asyncio.to_thread(
                drive_client.upload_pdf,
                output_path.read_bytes(),
                output_name,
                folder_id,
            )
            meta = self._read_meta(job_id)
            meta["driveFileId"] = result.fileId
            meta["driveWebViewLink"] = result.webViewLink
            meta["driveUploadError"] = None
            self._write_meta(job_id, meta)
            logger.info("Job %s uploaded to Drive file %s", job_id, result.fileId)
        except Exception as exc:
            logger.exception("Drive upload failed for job %s", job_id)
            meta = self._read_meta(job_id)
            meta["driveUploadError"] = str(exc)
            self._write_meta(job_id, meta)


def get_job_manager(settings: Settings) -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager(settings)
    return _job_manager


def reset_job_manager() -> None:
    global _job_manager
    _job_manager = None
