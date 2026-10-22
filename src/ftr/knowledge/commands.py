"""Fixed, validated operations shared by CLI and the protected command worker."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ftr.knowledge.contracts import DomainImport
from ftr.knowledge.models import (
    EditRequest,
    JobRequest,
    KnowledgeError,
    PublishRequest,
    ReviewRequest,
    StrictModel,
)
from ftr.knowledge.processing import read_blob
from ftr.knowledge.service import Knowledge


class Empty(StrictModel):
    pass


class Identity(StrictModel):
    id: str


class Start(Identity):
    executor: Literal["host", "api"] = "host"


class Blob(StrictModel):
    blob_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class Impact(Identity):
    topic_id: str
    resolution: Literal["create_job", "no_impact"] = "create_job"
    reason: str = Field(min_length=1)


class Batch(StrictModel):
    operations: list[dict] = Field(min_length=1, max_length=100)


CONTRACTS = {
    "seed": Empty,
    "batch": Batch,
    "domain.import": DomainImport,
    "index.rebuild": Empty,
    "index.refresh": Identity,
    "job.create": JobRequest,
    "job.start": Start,
    "job.retry": Identity,
    "job.cancel": Identity,
    "job.submit": Blob,
    "object.edit": EditRequest,
    "review": ReviewRequest,
    "publish": PublishRequest,
    "impact.scan": Empty,
    "impact.resolve": Impact,
}


class Operation(StrictModel):
    operation: str
    arguments: dict = Field(default_factory=dict)

    def checked(self):
        if self.operation not in CONTRACTS:
            raise KnowledgeError("OPERATION_INVALID", "操作类型未登记")
        return CONTRACTS[self.operation].model_validate(self.arguments).model_dump(mode="json")


def dispatch(repo, payload, settings, prepared_index=None):
    op = Operation.model_validate(payload)
    a = op.checked()
    k = Knowledge(repo)
    name = op.operation
    if name == "batch":
        results = []
        for index, value in enumerate(a["operations"]):
            child = Operation.model_validate(value)
            if child.operation not in ("review", "publish"):
                raise KnowledgeError("OPERATION_INVALID", "批量仅支持逐项人工审核或正式启用/发布")
            child.checked()
            sp = "batch_" + str(index)
            repo.db.execute("SAVEPOINT " + sp)
            try:
                result = dispatch(repo, value, settings)
                repo.db.execute("RELEASE " + sp)
                results.append({"index": index, "status": "APPLIED", "result": result})
            except ValueError as exc:
                repo.db.execute("ROLLBACK TO " + sp)
                repo.db.execute("RELEASE " + sp)
                results.append(
                    {
                        "index": index,
                        "status": "FAILED",
                        "code": getattr(exc, "code", "INVALID"),
                        "message": str(exc),
                    }
                )
        return {"results": results}
    if name == "seed":
        return k.seed()
    if name == "domain.import":
        return k.import_domains(a)
    if name == "index.rebuild":
        return k.reindex(prepared_index)
    if name == "index.refresh":
        from ftr.knowledge.indexing import fingerprint
        from ftr.models import DecisionRequest, DocumentRecord, digest

        value = prepared_index
        if not value:
            raise KnowledgeError("INDEX_REQUIRED", "请通过 CLI 或后台队列准备重解析")
        original = repo.get_document(value["original_id"])
        latest = repo.db.execute(
            "SELECT id FROM documents WHERE source_id=? AND canonical_url=? ORDER BY source_version DESC,extraction_version DESC LIMIT 1",
            (original.source_id, original.canonical_url),
        ).fetchone()[0]
        if (
            latest != value["original_id"]
            or fingerprint(original.model_dump(mode="json")) != value["fingerprint"]
        ):
            raise KnowledgeError("INPUT_STALE", "原资料已变化；请重新准备")
        record = DocumentRecord.model_validate(value["record"])
        task = repo.db.execute(
            "SELECT task_id FROM documents WHERE id=?", (value["original_id"],)
        ).fetchone()[0]
        rid, _ = repo.add_document(task, record, base_record_id=value["original_id"])
        raw = repo.db.execute("SELECT record_json FROM documents WHERE id=?", (rid,)).fetchone()[0]
        repo.add_decision(
            DecisionRequest(
                task_id=task,
                record_id=rid,
                input_digest=digest(raw.encode()),
                excerpt=record.body_text[:1500],
                evidence_id=record.evidence_id,
            )
        )
        return {"record_id": rid, "quality_state": "collected", "needs_review": True}
    if name == "job.create":
        return k.create_job(a.pop("job_type"), a.pop("topic_id"), a.pop("records"), **a)
    if name == "job.start":
        return k.start(a["id"], a["executor"], settings.processing.model_dump(mode="json"))
    if name == "job.retry":
        return k.retry(a["id"])
    if name == "job.cancel":
        return k.cancel(a["id"])
    if name == "job.submit":
        value = read_blob(k.root, a["blob_id"])
        result = k.submit(value)
        attempt = value["attempt_id"]
        import json
        import re

        if re.fullmatch("[a-f0-9]{32}", attempt):
            receipt = k.root / "processing_receipts" / (attempt + ".json")
            if receipt.is_file() and not receipt.is_symlink() and not receipt.parent.is_symlink():
                data = json.loads(receipt.read_text(encoding="utf-8"))
                if data["blob_id"] == a["blob_id"]:
                    repo.db.execute(
                        "UPDATE kw_attempts SET usage_json=? WHERE id=?",
                        (json.dumps(data.get("usage")), attempt),
                    )
        return result
    if name == "object.edit":
        return k.edit(a["object_id"], a["content"], a["expected_revision"], title=a["title"])
    if name == "review":
        return k.review(a["version_id"], a["decision"], a["reason"], actor="human")
    if name == "publish":
        return k.publish(a["version_id"], a["expected_revision"], a["expected_current"])
    if name == "impact.scan":
        return k.scan()
    if name == "impact.resolve":
        impact = k.row("kw_impacts", a["id"])
        if impact["state"] != "pending":
            return {"state": impact["state"]}
        job = None
        if a["resolution"] == "create_job":
            if impact["version_id"]:
                v = k.version(impact["version_id"])
                o = k.object(v["object_id"])
                if o["kind"] == "page":
                    versions = []
                    for s in v["content"]["sections"]:
                        for vid in s["knowledge_versions"]:
                            current = k.object(k.version(vid)["object_id"])["current_version"]
                            if current and current not in versions:
                                versions.append(current)
                    job = k.create_job(
                        "revise", a["topic_id"], [], knowledge_versions=versions, object_id=o["id"]
                    )
                else:
                    refs = [
                        d[0]
                        for d in repo.db.execute(
                            "SELECT target_id FROM kw_dependencies WHERE version_id=? AND kind='record'",
                            (v["id"],),
                        )
                    ]
                    latest = [
                        repo.db.execute(
                            "SELECT id FROM documents WHERE source_id=? AND canonical_url=? ORDER BY source_version DESC,extraction_version DESC LIMIT 1",
                            (repo.get_document(r).source_id, repo.get_document(r).canonical_url),
                        ).fetchone()[0]
                        for r in refs
                    ]
                    job = k.create_job("extract", a["topic_id"], latest, object_id=o["id"])
            else:
                job = k.create_job("extract", a["topic_id"], [impact["source_id"]])
        import json

        data = json.loads(impact["data_json"])
        data.update(reason=a["reason"], job_id=job["id"] if job else None)
        repo.db.execute(
            "UPDATE kw_impacts SET state=?,data_json=? WHERE id=?",
            ("claimed" if job else "dismissed", json.dumps(data, ensure_ascii=False), a["id"]),
        )
        return {"impact_id": a["id"], "job": job}
    raise KnowledgeError("OPERATION_INVALID", "未知操作")


def record_rejection(repo, payload, error):
    """Persist rejected identities after the enclosing command transaction rolls back."""
    from ftr.knowledge.indexing import encoded, fingerprint
    from ftr.knowledge.models import ProcessingResult
    from ftr.knowledge.service import now

    if payload.get("operation") != "job.submit":
        return
    try:
        raw = read_blob(repo.path.parent, payload["arguments"]["blob_id"])
        value = ProcessingResult.model_validate(raw)
    except (ValueError, KeyError, OSError):
        return
    if not repo.db.execute("SELECT 1 FROM kw_jobs WHERE id=?", (value.job_id,)).fetchone():
        return
    serialized = value.model_dump(mode="json")
    digest = fingerprint({key: v for key, v in serialized.items() if key != "submission_key"})
    repo.db.execute(
        "INSERT OR IGNORE INTO kw_submissions(job_id,attempt_id,submission_key,result_digest,accepted,result_json,created_at) VALUES(?,?,?,?,0,?,?)",
        (
            value.job_id,
            value.attempt_id,
            value.submission_key,
            digest,
            encoded(
                {
                    "code": getattr(error, "code", "INVALID_RESULT"),
                    "message": "回传校验失败；修正后使用新提交键",
                }
            ),
            now(),
        ),
    )
    if getattr(error, "code", None) in ("EDIT_CONFLICT", "INPUT_STALE"):
        repo.db.execute(
            "UPDATE kw_jobs SET state='stale' WHERE id=? AND attempt_id=? AND state NOT IN ('completed','cancelled')",
            (value.job_id, value.attempt_id),
        )
