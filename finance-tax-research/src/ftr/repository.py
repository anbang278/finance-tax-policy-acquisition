from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from ftr.models import (
    DecisionRequest,
    DocumentRecord,
    Evidence,
    SemanticDecision,
    TaskRequest,
    digest,
    new_id,
)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
 id TEXT PRIMARY KEY, request_json TEXT NOT NULL, request_digest TEXT NOT NULL,
 idempotency_key TEXT UNIQUE, state TEXT NOT NULL, pause_requested INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS source_runs (
 task_id TEXT NOT NULL REFERENCES tasks(id), source_id TEXT NOT NULL,
 next_page INTEGER NOT NULL DEFAULT 1, discovery_done INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'CREATED', pages_count INTEGER NOT NULL DEFAULT 0,
 documents_count INTEGER NOT NULL DEFAULT 0, bytes_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(task_id, source_id)
);
CREATE TABLE IF NOT EXISTS discovered (
 task_id TEXT NOT NULL REFERENCES tasks(id), source_id TEXT NOT NULL,
 url TEXT NOT NULL, ref_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 error TEXT, PRIMARY KEY(task_id, source_id, url)
);
CREATE TABLE IF NOT EXISTS evidence (
 id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL, canonical_url TEXT NOT NULL,
 source_version INTEGER NOT NULL, extraction_version INTEGER NOT NULL,
 record_json TEXT NOT NULL, raw_sha256 TEXT NOT NULL, body_sha256 TEXT NOT NULL,
 quality_state TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id),
 UNIQUE(source_id, canonical_url, source_version, extraction_version)
);
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 record_id TEXT NOT NULL REFERENCES documents(id), request_json TEXT NOT NULL,
 result_json TEXT, state TEXT NOT NULL DEFAULT 'PENDING'
);
CREATE TABLE IF NOT EXISTS failures (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL, source_id TEXT NOT NULL,
 category TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, event TEXT NOT NULL,
 details_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Repository:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.path = data_dir / "database.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def audit(self, task_id: str | None, event: str, details: dict) -> None:
        self.db.execute(
            "INSERT INTO audit(task_id,event,details_json) VALUES(?,?,?)",
            (task_id, event, json.dumps(details, ensure_ascii=False)),
        )
        self.db.commit()

    def create_task(self, request: TaskRequest) -> str:
        request_json = request.model_dump_json()
        request_digest = digest(request_json.encode())
        if request.idempotency_key:
            row = self.db.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (request.idempotency_key,)
            ).fetchone()
            if row:
                if row["request_digest"] != request_digest:
                    raise ValueError("相同幂等键对应不同任务输入")
                return str(row["id"])
        task_id = new_id()
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(id,request_json,request_digest,idempotency_key,state) VALUES(?,?,?,?,?)",
                (task_id, request_json, request_digest, request.idempotency_key, "CREATED"),
            )
            for source_id in request.source_ids:
                self.db.execute(
                    "INSERT INTO source_runs(task_id,source_id) VALUES(?,?)", (task_id, source_id)
                )
        return task_id

    def task(self, task_id: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"任务不存在: {task_id}")
        return row

    def source_run(self, task_id: str, source_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM source_runs WHERE task_id=? AND source_id=?", (task_id, source_id)
        ).fetchone()
        if row is None:
            raise KeyError(f"来源任务不存在: {source_id}")
        return row

    def set_task_state(self, task_id: str, state: str) -> None:
        self.db.execute("UPDATE tasks SET state=? WHERE id=?", (state, task_id))
        self.db.commit()
        self.audit(task_id, "task_state", {"state": state})

    def request_stop(self, task_id: str, cancel: bool) -> None:
        field = "cancel_requested" if cancel else "pause_requested"
        self.db.execute(f"UPDATE tasks SET {field}=1 WHERE id=?", (task_id,))
        self.db.commit()

    def save_page(self, task_id: str, source_id: str, refs: list, next_page: int | None) -> None:
        with self.db:
            for ref in refs:
                self.db.execute(
                    "INSERT OR IGNORE INTO discovered(task_id,source_id,url,ref_json) VALUES(?,?,?,?)",
                    (task_id, source_id, ref.url, ref.model_dump_json()),
                )
            self.db.execute(
                "UPDATE source_runs SET next_page=?, discovery_done=?, pages_count=pages_count+1, state=? WHERE task_id=? AND source_id=?",
                (
                    next_page or 0,
                    int(next_page is None),
                    "DISCOVERED" if next_page is None else "DISCOVERING",
                    task_id,
                    source_id,
                ),
            )

    def pending_refs(self, task_id: str, source_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM discovered WHERE task_id=? AND source_id=? AND state='PENDING' ORDER BY rowid",
            (task_id, source_id),
        ).fetchall()

    def retry_failed_refs(self, task_id: str, source_id: str) -> None:
        self.db.execute(
            "UPDATE discovered SET state='PENDING', error=NULL "
            "WHERE task_id=? AND source_id=? AND state='FAILED'",
            (task_id, source_id),
        )
        self.db.commit()

    def set_ref_state(
        self, task_id: str, source_id: str, url: str, state: str, error: str | None = None
    ) -> None:
        self.db.execute(
            "UPDATE discovered SET state=?, error=? WHERE task_id=? AND source_id=? AND url=?",
            (state, error, task_id, source_id, url),
        )
        self.db.commit()

    def save_evidence(self, evidence: Evidence) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO evidence(id,metadata_json) VALUES(?,?)",
            (evidence.evidence_id, evidence.model_dump_json()),
        )
        self.db.commit()

    def get_evidence(self, evidence_id: str) -> Evidence:
        row = self.db.execute(
            "SELECT metadata_json FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise KeyError("证据不存在")
        return Evidence.model_validate_json(row["metadata_json"])

    def add_document(self, task_id: str, record: DocumentRecord) -> tuple[str, bool]:
        latest = self.db.execute(
            "SELECT * FROM documents WHERE source_id=? AND canonical_url=? ORDER BY source_version DESC, extraction_version DESC LIMIT 1",
            (record.source_id, record.canonical_url),
        ).fetchone()
        if (
            latest
            and latest["raw_sha256"] == record.evidence_id
            and latest["body_sha256"] == record.body_sha256
            and DocumentRecord.model_validate_json(latest["record_json"]).parser_version
            == record.parser_version
        ):
            return str(latest["id"]), False
        source_version = (
            (int(latest["source_version"]) + 1)
            if latest and latest["raw_sha256"] != record.evidence_id
            else (int(latest["source_version"]) if latest else 1)
        )
        extraction_version = (
            1
            if not latest or source_version != int(latest["source_version"])
            else int(latest["extraction_version"]) + 1
        )
        with self.db:
            self.db.execute(
                "INSERT INTO documents(id,source_id,canonical_url,source_version,extraction_version,record_json,raw_sha256,body_sha256,quality_state,task_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    record.record_id,
                    record.source_id,
                    record.canonical_url,
                    source_version,
                    extraction_version,
                    record.model_dump_json(),
                    record.evidence_id,
                    record.body_sha256,
                    record.quality_state,
                    task_id,
                ),
            )
            self.db.execute(
                "UPDATE source_runs SET documents_count=documents_count+1 WHERE task_id=? AND source_id=?",
                (task_id, record.source_id),
            )
        return record.record_id, True

    def get_document(self, record_id: str) -> DocumentRecord:
        row = self.db.execute(
            "SELECT record_json FROM documents WHERE id=?", (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError("资料不存在")
        return DocumentRecord.model_validate_json(row["record_json"])

    def has_document(self, source_id: str, canonical_url: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM documents WHERE source_id=? AND canonical_url=? LIMIT 1",
                (source_id, canonical_url),
            ).fetchone()
            is not None
        )

    def add_decision(self, request: DecisionRequest) -> None:
        self.db.execute(
            "INSERT INTO decisions(id,task_id,record_id,request_json) VALUES(?,?,?,?)",
            (request.decision_id, request.task_id, request.record_id, request.model_dump_json()),
        )
        self.db.commit()

    def pending_decisions(self, task_id: str) -> list[DecisionRequest]:
        rows = self.db.execute(
            "SELECT request_json FROM decisions WHERE task_id=? AND state='PENDING'", (task_id,)
        ).fetchall()
        return [DecisionRequest.model_validate_json(row["request_json"]) for row in rows]

    def submit_decision(self, decision: SemanticDecision) -> DocumentRecord:
        row = self.db.execute(
            "SELECT * FROM decisions WHERE id=?", (decision.decision_id,)
        ).fetchone()
        if row is None:
            raise KeyError("决策请求不存在")
        request = DecisionRequest.model_validate_json(row["request_json"])
        if decision.input_digest != request.input_digest or decision.evidence_ids != [
            request.evidence_id
        ]:
            raise ValueError("决策输入或证据不匹配")
        if row["state"] == "DONE":
            if row["result_json"] != decision.model_dump_json():
                raise ValueError("决策已提交且内容不同")
            return self.get_document(request.record_id)
        record = self.get_document(request.record_id)
        if digest(record.model_dump_json().encode()) != request.input_digest:
            raise ValueError("资料已变化，决策失效")
        state: Literal["validated", "quarantined"] = (
            "validated" if decision.result == "PASS" and not record.limitations else "quarantined"
        )
        record.quality_state = state
        with self.db:
            self.db.execute(
                "UPDATE documents SET record_json=?, quality_state=? WHERE id=?",
                (record.model_dump_json(), state, record.record_id),
            )
            self.db.execute(
                "UPDATE decisions SET result_json=?, state='DONE' WHERE id=?",
                (decision.model_dump_json(), decision.decision_id),
            )
        return record

    def add_failure(self, task_id: str, source_id: str, category: str, details: dict) -> str:
        failure_id = new_id()
        self.db.execute(
            "INSERT INTO failures(id,task_id,source_id,category,details_json) VALUES(?,?,?,?,?)",
            (failure_id, task_id, source_id, category, json.dumps(details, ensure_ascii=False)),
        )
        self.db.commit()
        return failure_id

    def search(self, query: str, include_limited: bool = False) -> list[DocumentRecord]:
        rows = self.db.execute(
            "SELECT source_id,canonical_url,quality_state,record_json FROM documents "
            "ORDER BY source_id,canonical_url,source_version DESC,extraction_version DESC"
        ).fetchall()
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            latest.setdefault((row["source_id"], row["canonical_url"]), row)
        allowed = {"validated", "quarantined"} if include_limited else {"validated"}
        docs = [
            DocumentRecord.model_validate_json(row["record_json"])
            for row in latest.values()
            if row["quality_state"] in allowed
        ]
        return [
            doc
            for doc in docs
            if query in doc.title or query in doc.body_text or query in (doc.document_number or "")
        ]
