from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ftr.adapters.common import canonical_url
from ftr.config import load_sources
from ftr.diagnostics import failure_explanation, limitation_reasons, task_diagnostics, task_report
from ftr.models import Evidence, digest

LATEST = """
WITH ranked AS (
 SELECT *, ROW_NUMBER() OVER (
  PARTITION BY source_id, canonical_url
  ORDER BY source_version DESC, extraction_version DESC
 ) AS version_rank FROM documents
), latest AS (SELECT * FROM ranked WHERE version_rank=1)
"""
QUEUE_STATES = ("PENDING", "SAVED", "FAILED", "OUT_OF_SCOPE", "UNCHANGED_SKIP")


class QueryError(Exception):
    def __init__(self, code: str, message: str, status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def response(**data: Any) -> dict[str, Any]:
    return {"queried_at": datetime.now(UTC).isoformat(), **data}


def paginated(items: list, total: int, page: int, page_size: int) -> dict[str, Any]:
    return response(items=items, total=total, page=page, page_size=page_size)


def document(row: sqlite3.Row, full: bool = False) -> dict[str, Any]:
    record = json.loads(row["record_json"])
    record.update(
        record_id=row["id"],
        quality_state=row["quality_state"],
        task_id=row["task_id"],
        source_version=row["source_version"],
        extraction_version=row["extraction_version"],
    )
    record["limitation_reasons"] = limitation_reasons(record.get("limitations", []))
    if not full:
        record.pop("body_text", None)
        record.pop("field_evidence", None)
        record.pop("related_urls", None)
        record.pop("attachments", None)
    return record


class ReadQueries:
    """每次调用打开独立只读快照，不初始化目录、表或采集锁。"""

    def __init__(self, data_dir: Path):
        self.root = data_dir.resolve()
        self.database = self.root / "database.sqlite3"

    @contextmanager
    def snapshot(self) -> Iterator[sqlite3.Connection]:
        if not self.database.is_file():
            raise QueryError("DATABASE_MISSING", "未找到政策数据库，请检查 FTR_DATA_DIR。")
        connection = None
        try:
            connection = sqlite3.connect(
                self.database.as_uri() + "?mode=ro", uri=True, timeout=1, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            yield connection
        except sqlite3.Error as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                raise QueryError("DATABASE_BUSY", "数据库暂时繁忙，请稍后刷新。") from exc
            if "malformed" in str(exc) or "not a database" in str(exc):
                raise QueryError("DATABASE_CORRUPT", "数据库损坏，无法读取政策数据。") from exc
            raise QueryError("DATABASE_QUERY_FAILED", "数据库查询失败，请检查数据库结构。") from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise QueryError("DATA_INVALID", "数据库记录格式异常，无法读取。") from exc
        finally:
            if connection is not None:
                connection.close()

    def overview(self) -> dict:
        with self.snapshot() as db:
            quality = dict(
                db.execute(
                    LATEST + "SELECT quality_state, COUNT(*) FROM latest GROUP BY quality_state"
                )
            )
            tasks = dict(db.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state"))
            reasons: Counter = Counter()
            for row in db.execute(
                LATEST + "SELECT record_json FROM latest WHERE quality_state='quarantined'"
            ):
                record = json.loads(row[0])
                reasons.update(
                    {item["code"] for item in limitation_reasons(record.get("limitations", []))}
                    or {"other_historical"}
                )
            return response(
                quarantine_reasons=[
                    {"code": code, "count": count} for code, count in sorted(reasons.items())
                ],
                policies_total=sum(quality.values()),
                quality_counts=quality,
                task_counts=tasks,
                tasks_total=sum(tasks.values()),
            )

    def sources(self) -> dict:
        registry = load_sources()
        with self.snapshot() as db:
            counts = dict(
                db.execute(LATEST + "SELECT source_id, COUNT(*) FROM latest GROUP BY source_id")
            )
            return response(
                items=[
                    {"source_id": key, **value.model_dump(), "policies_count": counts.get(key, 0)}
                    for key, value in registry.sources.items()
                ]
            )

    def policies(
        self,
        q: str = "",
        source_id: str | None = None,
        quality_state: str | None = None,
        document_type: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "date",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        conditions: list[str] = []
        params: list[Any] = []
        if q:
            fields = ("title", "document_number", "body_text")
            conditions.append(
                "("
                + " OR ".join(
                    f"instr(lower(COALESCE(json_extract(record_json, '$.{key}'),'')), lower(?))>0"
                    for key in fields
                )
                + ")"
            )
            params.extend([q] * len(fields))
        for key, value in (("source_id", source_id), ("quality_state", quality_state)):
            if value:
                conditions.append(f"{key}=?")
                params.append(value)
        if document_type:
            conditions.append("json_extract(record_json, '$.document_type')=?")
            params.append(document_type)
        for op, value in ((">=", date_from), ("<=", date_to)):
            if value:
                conditions.append(
                    f"(json_extract(record_json, '$.listing_date'){op}? OR json_extract(record_json, '$.listing_date') IS NULL)"
                )
                params.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        order = (
            "json_extract(record_json, '$.title') ASC, id"
            if sort == "title"
            else "json_extract(record_json, '$.listing_date') IS NULL, "
            "json_extract(record_json, '$.listing_date') DESC, id"
        )
        with self.snapshot() as db:
            total = db.execute(LATEST + "SELECT COUNT(*) FROM latest" + where, params).fetchone()[0]
            rows = db.execute(
                LATEST + "SELECT * FROM latest" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            result = paginated([document(row) for row in rows], total, page, page_size)
            result["date_unknown_count"] = db.execute(
                LATEST
                + "SELECT COUNT(*) FROM latest"
                + (where + " AND " if where else " WHERE ")
                + "json_extract(record_json, '$.listing_date') IS NULL",
                params,
            ).fetchone()[0]
            result["date_unknown_notice"] = (
                "日期未知资料同时保留，无法确认是否属于指定日期区间"
                if (date_from or date_to)
                else "日期未知资料置后显示"
            )
            return result

    def _record(self, db: sqlite3.Connection, record_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise QueryError("POLICY_NOT_FOUND", "资料不存在或已不可用。", 404)
        return row

    def policy(self, record_id: str) -> dict:
        with self.snapshot() as db:
            row = self._record(db, record_id)
            record = document(row, full=True)
            evidence = db.execute(
                "SELECT metadata_json FROM evidence WHERE id=?", (record["evidence_id"],)
            ).fetchone()
            record["evidence"] = json.loads(evidence[0]) if evidence else None
            return response(item=record)

    def versions(self, record_id: str) -> dict:
        with self.snapshot() as db:
            row = self._record(db, record_id)
            rows = db.execute(
                "SELECT * FROM documents WHERE source_id=? AND canonical_url=? "
                "ORDER BY source_version DESC, extraction_version DESC",
                (row["source_id"], row["canonical_url"]),
            ).fetchall()
            return response(items=[document(item) for item in rows])

    def _task(self, db: sqlite3.Connection, task_id: str) -> dict:
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise QueryError("TASK_NOT_FOUND", "采集任务不存在。", 404)
        item = {
            "task_id": row["id"],
            "state": row["state"],
            "created_at": row["created_at"],
            "request": json.loads(row["request_json"]),
            "pause_requested": bool(row["pause_requested"]),
            "cancel_requested": bool(row["cancel_requested"]),
        }
        last = db.execute(
            "SELECT created_at FROM audit WHERE task_id=? AND event='task_state' "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        item["last_state_at"] = last[0] if last else None
        item.update(task_diagnostics(db, task_id, self.root))
        item["report"] = task_report(db, task_id)
        item["active_failure_count"] = item["report"]["active_failure_count"]
        item["pending_decisions"] = db.execute(
            "SELECT COUNT(*) FROM decisions WHERE task_id=? AND state='PENDING'",
            (task_id,),
        ).fetchone()[0]
        sources = []
        for run in db.execute("SELECT * FROM source_runs WHERE task_id=?", (task_id,)):
            source = dict(run)
            counts = dict(
                db.execute(
                    "SELECT state, COUNT(*) FROM discovered WHERE task_id=? AND source_id=? "
                    "GROUP BY state",
                    (task_id, run["source_id"]),
                )
            )
            source["queue_counts"] = {state: counts.get(state, 0) for state in QUEUE_STATES}
            source["discovered_count"] = sum(counts.values())
            source["new_versions_count"] = db.execute(
                "SELECT COUNT(*) FROM documents WHERE task_id=? AND source_id=?",
                (task_id, run["source_id"]),
            ).fetchone()[0]
            source["discovery_done"] = bool(source["discovery_done"])
            sources.append(source)
        item["sources"] = sources
        item["queue_counts"] = dict(
            sum((Counter(source["queue_counts"]) for source in sources), Counter())
        )
        item["pages_count"] = sum(source["pages_count"] for source in sources)
        item["new_versions_count"] = sum(source["new_versions_count"] for source in sources)
        return item

    def tasks(self, state: str | None = None, page: int = 1, page_size: int = 20) -> dict:
        with self.snapshot() as db:
            where, params = (" WHERE state=?", [state]) if state else ("", [])
            total = db.execute("SELECT COUNT(*) FROM tasks" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT id FROM tasks" + where + " ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return paginated([self._task(db, row[0]) for row in rows], total, page, page_size)

    def task(self, task_id: str) -> dict:
        with self.snapshot() as db:
            return response(item=self._task(db, task_id))

    def task_items(
        self,
        task_id: str,
        state: str | None = None,
        source_id: str | None = None,
        q: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        with self.snapshot() as db:
            self._task(db, task_id)
            conditions, params = ["task_id=?"], [task_id]
            for key, value in (("state", state), ("source_id", source_id)):
                if value:
                    conditions.append(f"{key}=?")
                    params.append(value)
            if q:
                conditions.append(
                    "(instr(json_extract(ref_json,'$.listing_title'),?)>0 OR instr(url,?)>0)"
                )
                params.extend([q, q])
            where = " WHERE " + " AND ".join(conditions)
            total = db.execute("SELECT COUNT(*) FROM discovered" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM discovered" + where + " ORDER BY rowid LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for row in rows:
                item = json.loads(row["ref_json"])
                item.update(state=row["state"], error=row["error"], record_id=None)
                if row["state"] in ("SAVED", "UNCHANGED_SKIP"):
                    record = db.execute(
                        "SELECT id,task_id FROM documents WHERE source_id=? AND canonical_url=? "
                        "ORDER BY source_version DESC, extraction_version DESC LIMIT 1",
                        (row["source_id"], canonical_url(row["url"])),
                    ).fetchone()
                    if record:
                        item["record_id"] = record["id"]
                        item["record_created_by_task"] = record["task_id"] == task_id
                items.append(item)
            return paginated(items, total, page, page_size)

    def events(self, task_id: str, page: int = 1, page_size: int = 20) -> dict:
        with self.snapshot() as db:
            self._task(db, task_id)
            union = (
                "SELECT 'audit:'||id AS event_id, event, details_json, created_at, "
                "NULL AS source_id, 'state' AS kind FROM audit WHERE task_id=? "
                "UNION ALL SELECT 'failure:'||id, category, details_json, created_at, "
                "source_id, 'failure' FROM failures WHERE task_id=?"
            )
            total = db.execute(f"SELECT COUNT(*) FROM ({union})", (task_id, task_id)).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM ({union}) ORDER BY created_at DESC, event_id DESC LIMIT ? OFFSET ?",
                (task_id, task_id, page_size, (page - 1) * page_size),
            ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["details"] = json.loads(item.pop("details_json"))
                if item["kind"] == "failure":
                    item["explanation"] = failure_explanation(item["event"], item["details"])
                items.append(item)
            return paginated(items, total, page, page_size)

    def evidence_download(self, evidence_id: str) -> tuple[bytes, Evidence]:
        with self.snapshot() as db:
            row = db.execute(
                "SELECT metadata_json FROM evidence WHERE id=?",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise QueryError("EVIDENCE_NOT_FOUND", "证据未登记，无法下载。", 404)
            evidence = Evidence.model_validate_json(row[0])
        root = (self.root / "evidence").resolve()
        path = (root / evidence.relative_path).resolve()
        if not path.is_relative_to(root) or path == root:
            raise QueryError("EVIDENCE_PATH_INVALID", "证据路径越界，已拒绝下载。", 409)
        if not path.is_file():
            raise QueryError("EVIDENCE_FILE_MISSING", "证据文件缺失，无法下载。", 404)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise QueryError("EVIDENCE_READ_FAILED", "证据文件无法读取。") from exc
        if digest(content) != evidence.sha256 or evidence.evidence_id != evidence_id:
            raise QueryError("EVIDENCE_CORRUPT", "证据哈希不匹配，已拒绝下载。", 409)
        return content, evidence
