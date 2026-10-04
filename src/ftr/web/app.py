from __future__ import annotations

import ipaddress
import mimetypes
import socket
import sqlite3
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ftr.config import SchedulerSettings, WebSettings
from ftr.web.query import QueryError, ReadQueries, response

Source = Literal["mof", "chinatax"]
Quality = Literal["collected", "validated", "quarantined", "rejected"]
DocumentType = Literal[
    "policy_file",
    "policy_announcement",
    "official_interpretation",
    "release_message",
    "other",
    "unknown",
]
TaskState = Literal[
    "CREATED",
    "RUNNING",
    "WAITING_DECISION",
    "PARTIAL",
    "COMPLETED",
    "COMPLETED_EMPTY",
    "CANCELLED",
]
QueueState = Literal["PENDING", "SAVED", "FAILED", "OUT_OF_SCOPE", "UNCHANGED_SKIP"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Search = Annotated[str, Query(max_length=500)]


def create_app(
    data_dir: Path,
    settings: WebSettings | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    *,
    management_session=None,
    runtime_settings=None,
) -> FastAPI:
    settings = settings or WebSettings()
    from ftr.config import load_settings
    from ftr.update_check import UpdateMonitor

    runtime_settings = runtime_settings or load_settings(
        overrides={
            "data_dir": data_dir,
            **({"scheduler": scheduler_settings.model_dump()} if scheduler_settings else {}),
        }
    )
    monitor = UpdateMonitor(runtime_settings)

    @asynccontextmanager
    async def lifespan(_app):
        monitor.start()
        try:
            yield
        finally:
            monitor.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="政策工作台 · 只读 API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.state.update_monitor = monitor
    local = settings.host == "localhost" or ipaddress.ip_address(settings.host).is_loopback
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"] if local else ["*"]
    )
    from ftr.web.management import install

    install(
        app,
        data_dir,
        runtime_settings,
        management_session if local else None,
    )
    queries = ReadQueries(data_dir)
    static = Path(__file__).parent / "static"

    @app.middleware("http")
    async def headers(request: Request, call_next):
        result = await call_next(request)
        result.headers["X-Content-Type-Options"] = "nosniff"
        result.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
        )
        result.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/api/", "/static/")) or request.url.path == "/":
            result.headers["Cache-Control"] = "no-store"
        return result

    @app.exception_handler(QueryError)
    async def query_error(_request: Request, exc: QueryError):
        return JSONResponse(
            response(error={"code": exc.code, "message": exc.message}), status_code=exc.status
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_busy(_request: Request, _exc: sqlite3.OperationalError):
        return JSONResponse(
            response(
                error={
                    "code": "DATABASE_UNAVAILABLE",
                    "message": "资料库暂不可读，可能正在写入；请稍后刷新并核对后台操作状态。",
                }
            ),
            status_code=503,
        )

    @app.exception_handler(ValidationError)
    async def invalid_data(_request: Request, _exc: ValidationError):
        return JSONResponse(
            response(error={"code": "DATA_INVALID", "message": "数据库记录格式异常，无法读取。"}),
            status_code=503,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_query(_request: Request, _exc: RequestValidationError):
        return JSONResponse(
            response(
                error={
                    "code": "QUERY_INVALID",
                    "message": "查询参数无效，请检查日期、分页和筛选条件。",
                }
            ),
            status_code=422,
        )

    @app.get("/api/update-status")
    def update_status() -> dict:
        return response(**monitor.read())

    @app.get("/api/overview")
    def overview() -> dict:
        return queries.overview()

    @app.get("/api/ui-config")
    def ui_config() -> dict:
        return response(
            poll_interval_ms=settings.poll_interval_ms,
            request_timeout_ms=settings.request_timeout_ms,
        )

    @app.get("/api/scheduler")
    def scheduler_status() -> dict:
        from ftr.scheduler import status

        return response(**status(data_dir, scheduler_settings))

    @app.get("/api/sources")
    def sources() -> dict:
        return queries.sources()

    @app.get("/api/policies")
    def policies(
        q: Search = "",
        source_id: Source | None = None,
        quality_state: Quality | None = None,
        document_type: DocumentType | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort: Literal["date", "title"] = "date",
        page: Page = 1,
        page_size: PageSize = 20,
    ) -> dict:
        if date_from and date_to and date_from > date_to:
            raise QueryError("DATE_RANGE_INVALID", "开始日期不能晚于结束日期。", 422)
        return queries.policies(
            q,
            source_id,
            quality_state,
            document_type,
            date_from.isoformat() if date_from else None,
            date_to.isoformat() if date_to else None,
            sort,
            page,
            page_size,
        )

    @app.get("/api/policies/{record_id}")
    def policy(record_id: str) -> dict:
        return queries.policy(record_id)

    @app.get("/api/policies/{record_id}/versions")
    def versions(record_id: str) -> dict:
        return queries.versions(record_id)

    @app.get("/api/tasks")
    def tasks(
        state: TaskState | None = None,
        page: Page = 1,
        page_size: PageSize = 20,
    ) -> dict:
        return queries.tasks(state, page, page_size)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str) -> dict:
        return queries.task(task_id)

    @app.get("/api/tasks/{task_id}/report")
    def report(task_id: str) -> dict:
        return response(report=queries.task(task_id)["item"]["report"])

    @app.get("/api/tasks/{task_id}/missing")
    def missing(task_id: str) -> dict:
        report = queries.task(task_id)["item"]["report"]
        return response(
            items=report["missing_items"],
            failures=report["failures"],
            unseen_matches=report["unseen_matches"],
            next_step=report["next_step"],
        )

    @app.get("/api/tasks/{task_id}/items")
    def items(
        task_id: str,
        state: QueueState | None = None,
        source_id: Source | None = None,
        q: Search = "",
        page: Page = 1,
        page_size: PageSize = 20,
    ) -> dict:
        return queries.task_items(task_id, state, source_id, q, page, page_size)

    @app.get("/api/tasks/{task_id}/events")
    def events(task_id: str, page: Page = 1, page_size: PageSize = 20) -> dict:
        return queries.events(task_id, page, page_size)

    @app.get("/api/evidence/{evidence_id}/download")
    def download(evidence_id: str) -> Response:
        content, evidence = queries.evidence_download(evidence_id)
        extension = mimetypes.guess_extension(evidence.media_type.split(";")[0]) or ".bin"
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{evidence.sha256}{extension}"',
            },
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app


def serve(
    data_dir: Path,
    port: int,
    settings: WebSettings | None = None,
    scheduler_settings: SchedulerSettings | None = None,
    *,
    runtime_settings=None,
) -> None:
    import uvicorn

    settings = (settings or WebSettings()).model_copy(update={"port": port})
    settings = WebSettings.model_validate(settings.model_dump())
    family = socket.AF_INET6 if ":" in settings.host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((settings.host, port))
            listener.listen(128)
        except OSError as exc:
            raise RuntimeError(f"无法监听 {settings.host}:{port}：端口被占用或不可用。") from exc
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    data_dir, settings, scheduler_settings, runtime_settings=runtime_settings
                ),
                host=settings.host,
                port=port,
                log_level="info",
            )
        )
        server.run(sockets=[listener])
