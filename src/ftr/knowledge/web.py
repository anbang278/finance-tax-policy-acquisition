from __future__ import annotations

import json
from contextlib import closing

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ftr.knowledge.models import KnowledgeError
from ftr.knowledge.processing import MAX_RESULT_BYTES, put_blob
from ftr.knowledge.service import Knowledge
from ftr.repository import Repository


def install(app, root, authorize):
    @app.exception_handler(KnowledgeError)
    async def knowledge_error(_request, exc):
        return JSONResponse(
            {"error": {"code": exc.code, "message": str(exc)}},
            status_code=404
            if exc.code == "NOT_FOUND"
            else 409
            if exc.code in ("EDIT_CONFLICT", "INPUT_STALE", "IDEMPOTENCY_CONFLICT")
            else 422,
        )

    def query(fn):
        if not (root / "database.sqlite3").exists():
            return {
                "available": False,
                "items": [],
                "message": "尚无资料库；请先在明确范围内采集资料",
            }
        with closing(Repository(root, readonly=True)) as repo:
            k = Knowledge(repo)
            if not k.available():
                return {
                    "available": False,
                    "items": [],
                    "message": "需要显式写入操作升级到知识库 v5；浏览不会自动迁移",
                }
            return fn(k)

    @app.get("/api/knowledge/overview")
    def overview():
        def read(k):
            objects = k.listing("objects")
            for obj in objects:
                candidate = k.db.execute(
                    "SELECT id FROM kw_versions WHERE object_id=? ORDER BY rowid DESC LIMIT 1",
                    (obj["id"],),
                ).fetchone()
                obj["candidate_version"] = candidate[0] if candidate else None
                obj["dependency_state"] = (
                    k.dependency(obj["current_version"]) if obj["current_version"] else "unknown"
                )
            return {
                "available": True,
                **{
                    name: k.listing(name)
                    for name in ("domains", "topics", "objects", "jobs", "impacts")
                },
                "objects": objects,
                "domain_topics": [dict(x) for x in k.db.execute("SELECT * FROM kw_domain_topics")],
                "index": dict(k.db.execute("SELECT key,value FROM kw_meta").fetchall()),
            }

        return query(read)

    @app.get("/api/knowledge/search")
    def search(
        q: str = "",
        kind: str = "all",
        include_limited: bool = False,
        as_of: str | None = None,
        domain: str | None = None,
        topic: str | None = None,
        region: str | None = None,
        subject: str | None = None,
    ):
        return query(
            lambda k: k.search(q, kind, include_limited, as_of, domain, topic, region, subject)
        )

    @app.get("/api/knowledge/materials")
    def materials():
        return query(
            lambda k: {
                "items": [
                    {"id": r.record_id, "title": r.title, "limitations": r.limitations}
                    for r in k.repo.search("")
                    if k.source_state(r.record_id) == "checked"
                ]
            }
        )

    @app.get("/api/knowledge/objects/{id}")
    def detail(id: str):
        return query(lambda k: k.detail(id))

    @app.get("/api/knowledge/jobs/{id}")
    def job(id: str):
        return query(lambda k: k.job(id))

    @app.get("/api/knowledge/jobs/{id}/package")
    def package(id: str):
        return query(lambda k: k.package(id))

    @app.get("/api/knowledge/segments/{id}")
    def segment(id: str):
        return query(lambda k: json.loads(k.row("kw_segments", id)["data_json"]))

    @app.get("/api/knowledge/objects/{id}/diff/{version}")
    def difference(id: str, version: str):
        return query(lambda k: k.diff(id, version))

    @app.get("/api/knowledge/objects/{id}/export")
    def export(id: str, version: str | None = None):
        result = query(lambda k: k.export(id, version))
        if not isinstance(result, str):
            return result
        return Response(
            result,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="wiki.md"'},
        )

    @app.post("/api/manage/knowledge/upload")
    async def upload(request: Request):
        authorize(request)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "仅接受 JSON")
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > MAX_RESULT_BYTES:
                raise HTTPException(413, "结果超过 4 MiB")
        id = await run_in_threadpool(put_blob, root, bytes(raw))
        return {"blob_id": id, "bytes": len(raw)}
