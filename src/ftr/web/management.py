"""Loopback-only management capability; tokens never appear in request URLs or logs."""

from __future__ import annotations

import ipaddress
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ftr.config import RuntimeSettings, SchedulerSettings
from ftr.management import (
    Command,
    Conflict,
    effective_plan,
    enqueue,
    operations,
    preflight,
    review_detail,
    review_queue,
)
from ftr.scheduler import preview
from ftr.web.query import response
from ftr.worker import ensure_worker, public_status


class ManagementSession:
    def __init__(self):
        self.bootstrap: dict[str, float] = {}
        self.sessions: set[str] = set()

    def issue(self):
        now = time.monotonic()
        self.bootstrap = {k: v for k, v in self.bootstrap.items() if v > now}
        value = secrets.token_urlsafe(32)
        self.bootstrap[value] = now + 120
        return value

    def exchange(self, token):
        expiry = self.bootstrap.pop(token, 0)
        if expiry < time.monotonic():
            raise HTTPException(403, "管理入口已使用或过期；请重新通过启动器打开")
        value = secrets.token_urlsafe(32)
        self.sessions.add(value)
        return value


def install(app, root: Path, settings: RuntimeSettings, session: ManagementSession | None):
    settings = settings.model_copy(update={"data_dir": root.resolve()})

    def check_local(request: Request):
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if session is None or not local:
            raise HTTPException(403, "当前工作台为只读模式")
        origin = str(request.base_url).rstrip("/")
        if request.headers.get("Origin") != origin:
            raise HTTPException(403, "拒绝跨源或缺少 Origin 的写入")
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin"):
            raise HTTPException(403, "拒绝跨站请求")

    def authorize(request: Request):
        check_local(request)
        assert session is not None
        if request.cookies.get("ftr_manage") not in session.sessions:
            raise HTTPException(403, "缺少有效本机管理会话")

    @app.get("/api/manage/session")
    def session_status(request: Request):
        return response(
            management_enabled=session is not None,
            authorized=session is not None
            and request.cookies.get("ftr_manage") in session.sessions,
        )

    @app.post("/api/manage/session")
    async def exchange(request: Request):
        check_local(request)
        assert session is not None
        try:
            payload = await request.json()
            if (
                not isinstance(payload, dict)
                or set(payload) != {"token"}
                or not isinstance(payload["token"], str)
            ):
                raise ValueError
            cookie = session.exchange(payload["token"])
        except ValueError as exc:
            raise HTTPException(422, "管理入口格式无效") from exc
        result = JSONResponse(response(authorized=True))
        result.set_cookie("ftr_manage", cookie, httponly=True, samesite="strict", path="/")
        return result

    @app.get("/api/manage/plan")
    def plan():
        return response(**effective_plan(root, settings.scheduler), worker=public_status(root))

    @app.post("/api/manage/preview")
    async def plan_preview(request: Request):
        authorize(request)
        try:
            value = SchedulerSettings.model_validate(await request.json())
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, "计划参数无效") from exc
        return response(**preview(value))

    @app.get("/api/manage/operations")
    def operation_status():
        items = operations(root)
        worker = public_status(root)
        running = worker.get("activity", {}).get("operation_id")
        for item in items:
            if item["id"] == running and item["state"] == "QUEUED":
                item["state"] = "RUNNING"
        return response(items=items, worker=worker)

    @app.post("/api/manage/commands")
    async def submit(request: Request):
        authorize(request)
        if request.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "仅接受 JSON 管理命令")
        raw = await request.body()
        if len(raw) > 64_000:
            raise HTTPException(413, "管理命令过大")
        try:
            command = Command.model_validate_json(raw)
            await run_in_threadpool(preflight, root, command)
            # Fail closed before queueing if an independent scheduler owns the directory.
            await run_in_threadpool(ensure_worker, settings)
            result = await run_in_threadpool(enqueue, root, command)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, "管理操作无效或后台不可接管：" + str(exc)) from exc
        return JSONResponse(response(**result), status_code=202)

    @app.get("/api/reviews")
    def reviews(category: str = "pending"):
        if category not in ("pending", "limited", "further", "done", "all"):
            raise HTTPException(422, "复核队列无效")
        return response(items=review_queue(root, category))

    @app.get("/api/reviews/{record_id}")
    def detail(record_id: str):
        try:
            return response(**review_detail(root, record_id))
        except KeyError as exc:
            raise HTTPException(404, "资料不存在") from exc
