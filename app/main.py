"""FastAPI application entrypoint."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.openapi.docs import get_swagger_ui_html

from app.api.routes_drive import router as drive_router
from app.api.routes_jobs import router as jobs_router
from app.api.routes_ocr import router as ocr_router
from app.config import get_settings
from app.exceptions import AppError
from app.schemas import HealthResponse, ProblemDetails
from app.services.job_manager import get_job_manager

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def _configure_logging() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] [request_id=%(request_id)s] %(message)s",
    )

    class RequestIdFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.request_id = request_id_ctx.get("-")
            return True

    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RequestIdFilter())


def _load_openapi_spec() -> dict:
    settings = get_settings()
    with settings.openapi_path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


_configure_logging()

settings = get_settings()
for warning in settings.validate_gcp_configuration():
    logging.getLogger(__name__).warning(warning)


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_manager = get_job_manager(get_settings())
    await job_manager.start()
    yield
    await job_manager.stop()


app = FastAPI(
    title="DocAI OCR API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    token = request_id_ctx.set(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        request_id_ctx.reset(token)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    problem = ProblemDetails(
        type=exc.type_,
        title=exc.title,
        status=exc.status,
        detail=exc.detail,
        instance=str(request.url.path),
    )
    return JSONResponse(
        status_code=exc.status,
        content=problem.model_dump(),
        media_type="application/problem+json",
    )


@app.get("/healthz", tags=["System"], response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/openapi.yml", include_in_schema=False)
async def openapi_yaml() -> PlainTextResponse:
    settings = get_settings()
    content = settings.openapi_path.read_text(encoding="utf-8")
    return PlainTextResponse(content, media_type="application/yaml")


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    return JSONResponse(_load_openapi_spec())


@app.get("/docs", include_in_schema=False)
async def swagger_ui():
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="DocAI OCR API",
    )


app.include_router(ocr_router)
app.include_router(drive_router)
app.include_router(jobs_router)
