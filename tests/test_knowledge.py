"""Knowledge/Wiki contracts run against fictional evidence in isolated stores."""

from contextlib import closing
from datetime import date

import pytest

from ftr.evidence import EvidenceStore
from ftr.knowledge.models import KnowledgeError
from ftr.knowledge.service import Knowledge
from ftr.models import Attachment, DocumentRecord, DocumentType, TaskRequest
from ftr.repository import Repository


@pytest.fixture
def kb(tmp_path):
    with closing(Repository(tmp_path)) as repo:
        task = repo.create_task(
            TaskRequest(source_ids=["mof"], date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
        )
        ev = EvidenceStore(tmp_path).save(
            b"fictional evidence",
            "https://www.mof.gov.cn/test",
            "https://www.mof.gov.cn/test",
            "text/html",
        )
        att = EvidenceStore(tmp_path).save(
            b"fictional attachment",
            "https://www.mof.gov.cn/a",
            "https://www.mof.gov.cn/a",
            "text/plain",
        )
        repo.save_evidence(ev)
        repo.save_evidence(att)
        record = DocumentRecord(
            source_id="mof",
            source_url=ev.source_url,
            canonical_url=ev.source_url,
            title="虚构企业所得税优惠",
            listing_title="虚构",
            listing_date_kind="listing",
            document_type=DocumentType.POLICY_FILE,
            body_text="虚构政策：符合条件的主体可以申请。",
            evidence_id=ev.evidence_id,
            quality_state="validated",
            attachments=[
                Attachment(
                    url=att.source_url,
                    label="虚构例外",
                    evidence_id=att.evidence_id,
                    download_state="saved",
                    extraction_state="text",
                    text="虚构例外：发票不全时不得申请。",
                )
            ],
        )
        repo.add_document(task, record)
        k = Knowledge(repo)
        k.seed()
        k.reindex()
        yield k, record


def result_for(k, job, *, title="优惠条件", object_id=None):
    p = k.package(job["id"])
    s = p["segments"][0]
    citation = {
        key: s[key]
        for key in ("segment_id", "record_id", "evidence_id", "source_part", "text_version_digest")
    }
    citation.update(
        quote=s["text"], start_offset=0, end_offset=len(s["text"]), page_number=s["page_number"]
    )
    return {
        "job_id": p["job_id"],
        "attempt_id": p["attempt_id"],
        "input_digest": p["input_digest"],
        "submission_key": "first",
        "outputs": [
            {
                "kind": "item",
                "object_id": object_id,
                "title": title,
                "content": {
                    "statement": "符合条件才可申请",
                    "nature": "source_rule",
                    "conditions": ["满足主体条件"],
                    "exceptions": ["材料不全不得申请"],
                    "limitations": [],
                    "citations": [citation],
                },
            }
        ],
        "executor": {"mode": "host", "model": "unknown"},
    }


def candidate(k, record):
    j = k.create_job("extract", "cit-conditions", [record.record_id])
    result = k.submit(result_for(k, j))
    return result["versions"][0]


def approve(k, version):
    k.review(version, "approved", "已人工核对全部条件和例外", actor="human")
    obj = k.object(k.version(version)["object_id"])
    return k.publish(version, obj["edit_revision"], obj["current_version"])


def test_attachment_search_and_anchor(kb):
    k, record = kb
    assert k.search("发票")["items"]
    assert k.search("企业所得税")["items"]
    job = k.create_job("extract", "cit-conditions", [record.record_id])
    r = result_for(k, job)
    r["outputs"][0]["content"]["citations"][0]["evidence_id"] = "wrong"
    with pytest.raises(KnowledgeError):
        k.submit(r)
    r["submission_key"] = "corrected"
    r["outputs"][0]["content"]["citations"][0]["evidence_id"] = k.package(job["id"])["segments"][0][
        "evidence_id"
    ]
    a = k.submit(r)
    assert k.submit(r) == a
    r["outputs"][0]["title"] = "changed"
    with pytest.raises(KnowledgeError):
        k.submit(r)


def test_human_gate_and_draft_conflict(kb):
    k, r = kb
    v = candidate(k, r)
    with pytest.raises(KnowledgeError):
        k.publish(v, 0, None)
    k.review(v, "approved", "AI预审", actor="ai")
    with pytest.raises(KnowledgeError):
        k.publish(v, 0, None)
    approve(k, v)
    k.review(v, "withdrawn", "暂时撤回，需重新核对")
    assert not k.detail(k.version(v)["object_id"])["formally_available"]
    with pytest.raises(KnowledgeError):
        o = k.object(k.version(v)["object_id"])
        k.publish(v, o["edit_revision"], v)
    k.review(v, "approved", "人工重新核对完成")
    obj = k.object(k.version(v)["object_id"])
    j = k.create_job("extract", "cit-conditions", [r.record_id], object_id=obj["id"])
    out = result_for(k, j, object_id=obj["id"])
    k.edit(obj["id"], k.version(v)["content"], obj["edit_revision"], title="人工草稿")
    with pytest.raises(KnowledgeError):
        k.submit(out)
    assert k.object(obj["id"])["current_version"] == v


def test_wiki_publish_and_export(kb):
    k, r = kb
    v = candidate(k, r)
    approve(k, v)
    j = k.create_job("compile", "cit-conditions", [], knowledge_versions=[v])
    p = k.package(j["id"])
    out = {
        "job_id": j["id"],
        "attempt_id": p["attempt_id"],
        "input_digest": p["input_digest"],
        "submission_key": "wiki",
        "outputs": [
            {
                "kind": "page",
                "title": "企业所得税优惠条件",
                "content": {
                    "sections": [
                        {
                            "section_id": "conditions",
                            "title": "条件",
                            "text": "按已核对条件申请。",
                            "knowledge_versions": [v],
                            "human_maintained": False,
                        }
                    ],
                    "limitations": ["虚构测试内容"],
                },
            }
        ],
        "executor": {"mode": "host"},
    }
    pv = k.submit(out)["versions"][0]
    approve(k, pv)
    md = k.export(k.version(pv)["object_id"])
    assert r.record_id in md and "虚构测试内容" in md and "知识核验" in md
    assert str(k.root) not in md
    assert k.detail(k.version(pv)["object_id"])["dependency_state"] == "checked"


def test_source_change_blocks_published_and_scan_idempotent(kb):
    k, r = kb
    v = candidate(k, r)
    approve(k, v)
    changed = r.model_copy(
        update={"record_id": "new-version", "body_text": "新条件", "evidence_id": r.evidence_id}
    )
    task = k.repo.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, changed)
    assert k.detail(k.version(v)["object_id"])["dependency_state"] == "needs_review"
    k.scan()
    n = len(k.listing("impacts"))
    k.scan()
    assert len(k.listing("impacts")) == n
    assert not k.search("符合条件", kind="item")["items"]


def test_readonly_old_database_does_not_migrate(tmp_path):
    with closing(Repository(tmp_path)) as repo:
        repo.db.execute("PRAGMA user_version=0")
        repo.db.commit()
    before = (tmp_path / "database.sqlite3").read_bytes()
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert Knowledge(repo).listing("domains") == [] or Knowledge(repo).listing("domains")
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    with closing(Repository(tmp_path)) as repo:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 5


def test_api_success_and_no_write_lock_during_call(kb):
    import json

    import httpx

    from ftr.config import ProcessingSettings, RuntimeSettings
    from ftr.knowledge.processing import execute_next
    from ftr.runtime import data_lock

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    result = result_for(k, j)
    settings = RuntimeSettings(
        data_dir=k.root,
        processing=ProcessingSettings(
            enabled=True, base_url="http://127.0.0.1:9999/v1", model="fixture", auth_mode="none"
        ),
        network={"proxy_mode": "direct"},
    )
    k.start(j["id"], "api", settings.processing.model_dump())

    def handler(request):
        with data_lock(k.root):
            pass
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 8192
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(result, ensure_ascii=False)},
                    }
                ],
                "usage": {"total_tokens": 100},
            },
        )

    out = execute_next(settings, transport=httpx.MockTransport(handler))
    assert out["state"] == "completed"
    assert k.job(j["id"])["state"] == "completed"
    assert not k.approved(out["versions"][0])


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("auth", "AUTH_FAILED"),
        ("timeout", "REMOTE_OUTCOME_UNKNOWN"),
        ("refusal", "MODEL_REFUSAL"),
        ("truncated", "OUTPUT_INCOMPLETE"),
        ("invalid", "INVALID_RESULT"),
        ("limited", "API_HTTP_ERROR"),
    ],
)
def test_api_failure_is_not_candidate(kb, failure, expected):
    import httpx

    from ftr.config import ProcessingSettings, RuntimeSettings
    from ftr.knowledge.processing import execute_next

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id], max_calls=1)
    settings = RuntimeSettings(
        data_dir=k.root,
        processing=ProcessingSettings(
            enabled=True, base_url="http://127.0.0.1:9999/v1", model="fixture", auth_mode="none"
        ),
        network={"proxy_mode": "direct"},
    )
    k.start(j["id"], "api", settings.processing.model_dump())

    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("hidden", request=request)
        if failure in ("auth", "limited"):
            return httpx.Response(401 if failure == "auth" else 429)
        if failure == "invalid":
            return httpx.Response(200, json={"bad": "response"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length" if failure == "truncated" else "stop",
                        "message": {
                            "refusal": "refused" if failure == "refusal" else None,
                            "content": "",
                        },
                    }
                ]
            },
        )

    out = execute_next(settings, transport=httpx.MockTransport(handler))
    assert out["code"] == expected
    assert not k.listing("objects")


@pytest.mark.parametrize("intervening", ["completed", "cancelled", "retried"])
def test_late_api_failure_preserves_current_job_state(kb, intervening):
    import httpx

    from ftr.config import ProcessingSettings, RuntimeSettings
    from ftr.knowledge.processing import execute_next
    from ftr.runtime import data_lock

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    value = result_for(k, j)
    settings = RuntimeSettings(
        data_dir=k.root,
        processing=ProcessingSettings(
            enabled=True, base_url="http://127.0.0.1:9999/v1", model="fixture", auth_mode="none"
        ),
        network={"proxy_mode": "direct"},
    )
    k.start(j["id"], "api", settings.processing.model_dump())
    accepted = None

    def handler(request):
        nonlocal accepted
        with data_lock(k.root), closing(Repository(k.root)) as repo:
            other = Knowledge(repo)
            if intervening == "completed":
                accepted = other.submit(value)
            else:
                other.cancel(j["id"])
                if intervening == "retried":
                    other.retry(j["id"])
        raise httpx.ReadTimeout("late timeout", request=request)

    out = execute_next(settings, transport=httpx.MockTransport(handler))
    current = k.job(j["id"])
    expected = "queued" if intervening == "retried" else intervening
    assert out["state"] == current["state"] == expected
    assert current["error"] == {}
    original = k.db.execute(
        "SELECT state,calls FROM kw_attempts WHERE id=?", (j["attempt_id"],)
    ).fetchone()
    assert original["state"] == ("completed" if intervening == "completed" else "running")
    assert original["calls"] == 1
    if intervening == "completed":
        assert current["result"] == accepted
        assert out["versions"] == accepted["versions"]
        assert (
            k.db.execute("SELECT COUNT(*) FROM kw_submissions WHERE accepted=1").fetchone()[0] == 1
        )
    elif intervening == "retried":
        assert current["attempt_id"] != j["attempt_id"]
        assert (
            k.db.execute(
                "SELECT state FROM kw_attempts WHERE id=?", (current["attempt_id"],)
            ).fetchone()[0]
            == "queued"
        )


def test_long_result_upload_backup_and_readonly_http(kb, tmp_path):
    import json

    from fastapi.testclient import TestClient

    from ftr.backup import create_backup, restore_backup
    from ftr.knowledge.processing import put_blob, read_blob
    from ftr.web.app import create_app
    from ftr.web.management import ManagementSession

    k, _ = kb
    raw = json.dumps({"text": "测试" * 50000}, ensure_ascii=False).encode()
    bid = put_blob(k.root, raw)
    assert read_blob(k.root, bid)["text"] == "测试" * 50000
    before = (k.root / "database.sqlite3").read_bytes()
    session = ManagementSession()
    client = TestClient(
        create_app(k.root, management_session=session),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 12345),
    )
    assert client.get("/wiki").status_code == 200
    assert client.get("/api/knowledge/overview").status_code == 200
    assert client.post("/api/manage/knowledge/upload", content=raw).status_code == 403
    token = session.issue()
    headers = {"Origin": "http://127.0.0.1"}
    assert (
        client.post("/api/manage/session", json={"token": token}, headers=headers).status_code
        == 200
    )
    response = client.post(
        "/api/manage/knowledge/upload",
        content=raw,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.json()["blob_id"] == bid
    assert (k.root / "database.sqlite3").read_bytes() == before
    backup = tmp_path.parent / (tmp_path.name + "-backup")
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    create_backup(k.root, backup)
    restore_backup(backup, restored)
    assert read_blob(restored, bid) == read_blob(k.root, bid)
    with pytest.raises(KnowledgeError):
        read_blob(k.root, "../bad")
    with pytest.raises(KnowledgeError):
        put_blob(k.root, b" " * (4 * 1024 * 1024 + 1))


def test_source_qualification_and_historical_dates(kb):
    from ftr.adapters.common import parse_date

    assert parse_date("1995年1月2日") == date(1995, 1, 2)
    k, r = kb
    with pytest.raises(KnowledgeError, match="YYYY-MM-DD"):
        k.search("符合条件", as_of="invalid")
    v = candidate(k, r)
    c = k.version(v)["content"]
    c.update(valid_from="1995-01-01", valid_to="1999-12-31", applicability_verified=True)
    o = k.object(k.version(v)["object_id"])
    v = k.edit(o["id"], c, 0, title=o["title"])["version_id"]
    approve(k, v)
    assert k.search("符合条件", kind="item", as_of="1996-01-01")["items"]
    assert not k.search("符合条件", kind="item", as_of="2000-01-01")["items"]
    k.repo.db.execute(
        "UPDATE documents SET quality_state='rejected',record_json=json_set(record_json,'$.quality_state','rejected') WHERE id=?",
        (r.record_id,),
    )
    k.repo.db.commit()
    assert not k.search("符合条件", kind="item")["items"]
    with pytest.raises(KnowledgeError):
        approve(k, v)


def test_formal_search_tracks_published_title_and_ignores_draft_title(kb):
    k, r = kb
    initial = candidate(k, r)
    approve(k, initial)
    object_id = k.version(initial)["object_id"]
    job = k.create_job("extract", "cit-conditions", [r.record_id], object_id=object_id)
    revised = k.submit(result_for(k, job, title="RevisedOfficialTitle", object_id=object_id))[
        "versions"
    ][0]
    assert not k.search("RevisedOfficialTitle", kind="item")["items"]
    assert k.search("优惠条件", kind="item")["items"][0]["id"] == object_id
    approve(k, revised)
    assert k.object(object_id)["title"] == "RevisedOfficialTitle"
    assert (
        k.search("RevisedOfficialTitle", kind="item")["items"][0]["title"] == "RevisedOfficialTitle"
    )
    assert not k.search("优惠条件", kind="item")["items"]
    k.edit(
        object_id,
        k.version(revised)["content"],
        k.object(object_id)["edit_revision"],
        title="UnpublishedDraftTitle",
    )
    assert not k.search("UnpublishedDraftTitle", kind="item")["items"]
    assert (
        k.search("RevisedOfficialTitle", kind="item")["items"][0]["title"] == "RevisedOfficialTitle"
    )


def test_date_boundaries_have_separate_idempotent_impact_reminders(kb, monkeypatch):
    from datetime import UTC, datetime

    from ftr.config import RuntimeSettings
    from ftr.knowledge.commands import dispatch

    k, r = kb
    version = candidate(k, r)
    content = k.version(version)["content"]
    content.update(valid_from="2026-01-10", valid_to="2026-01-20", applicability_verified=True)
    obj = k.object(k.version(version)["object_id"])
    version = k.edit(obj["id"], content, obj["edit_revision"], title=obj["title"])["version_id"]
    approve(k, version)
    instant = datetime(2026, 1, 9, tzinfo=UTC)
    monkeypatch.setattr("ftr.knowledge.service.utc_now", lambda: instant)

    def reminders():
        return [
            impact
            for impact in k.listing("impacts")
            if impact["version_id"] == version and impact["reason"] == "date_check"
        ]

    k.scan()
    assert not reminders()
    instant = datetime(2026, 1, 11, tzinfo=UTC)
    k.scan()
    start = reminders()[0]
    dispatch(
        k.repo,
        {
            "operation": "impact.resolve",
            "arguments": {
                "id": start["id"],
                "topic_id": "cit-conditions",
                "resolution": "no_impact",
                "reason": "已人工检查生效日期",
            },
        },
        RuntimeSettings(data_dir=k.root),
    )
    k.db.commit()
    k.scan()
    assert len(reminders()) == 1 and reminders()[0]["state"] == "dismissed"
    instant = datetime(2026, 1, 21, tzinfo=UTC)
    k.scan()
    due = reminders()
    assert len(due) == 2
    assert {impact["data"]["boundary"] for impact in due} == {"valid_from", "valid_to"}
    end = next(impact for impact in due if impact["data"]["boundary"] == "valid_to")
    assert end["id"] != start["id"] and end["state"] == "pending"
    assert end["data"]["date"] == "2026-01-20"
    assert next(impact for impact in due if impact["id"] == start["id"])["state"] == "dismissed"
    k.scan()
    assert len(reminders()) == 2


def test_pure_html_change_requires_equivalence_record(kb):
    k, r = kb
    v = candidate(k, r)
    approve(k, v)
    ev = EvidenceStore(k.root).save(
        b"new html navigation",
        "https://www.mof.gov.cn/test",
        "https://www.mof.gov.cn/test",
        "text/html",
    )
    k.repo.save_evidence(ev)
    fresh = r.model_copy(
        update={
            "record_id": "nav-change",
            "evidence_id": ev.evidence_id,
            "quality_state": "collected",
        }
    )
    task = k.repo.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, fresh)
    assert k.dependency(v) == "needs_review"
    k.scan()
    assert k.dependency(v) == "checked"
    assert k.repo.get_document(fresh.record_id).quality_state == "collected"


def test_page_publication_fault_is_atomic(kb, monkeypatch):
    k, r = kb
    v = candidate(k, r)
    k.review(v, "approved", "人工已核对")
    original = k.validate_content

    def fail(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("injected")

    monkeypatch.setattr(k, "validate_content", fail)
    with pytest.raises(RuntimeError):
        k.publish(v, 0, None)
    assert k.object(k.version(v)["object_id"])["current_version"] is None
    assert not k.listing("publications")


def test_two_domains_reuse_knowledge(kb):
    k, r = kb
    v = candidate(k, r)
    approve(k, v)
    k.import_domains(
        {
            "domains": [{"id": "second", "name": "第二领域", "topics": ["cit-conditions"]}],
            "topics": [],
        }
    )
    a = k.search("符合条件", kind="item", domain="cit-incentives")["items"]
    b = k.search("符合条件", kind="item", domain="second")["items"]
    assert a[0]["id"] == b[0]["id"]


def test_schemas_reject_privileged_result_and_human_forgery(kb):
    from ftr.management import Command

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    value = result_for(k, j)
    value["approved"] = True
    with pytest.raises(ValueError):
        k.submit(value)
    with pytest.raises(ValueError):
        Command(
            id="a" * 32,
            action="knowledge.apply",
            payload={
                "operation": "review",
                "arguments": {
                    "version_id": "fake",
                    "decision": "approved",
                    "reason": "fake",
                    "actor": "human",
                    "confirmed": True,
                },
            },
        )


def test_pdf_page_anchors_and_historical_reparse(kb):
    import io

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    k, r = kb
    writer = PdfWriter()
    for text in ("FirstCondition", "AttachmentOnlyException"):
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(("BT /F1 12 Tf 10 10 Td (" + text + ") Tj ET").encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = io.BytesIO()
    writer.write(buf)
    ev = EvidenceStore(k.root).save(
        buf.getvalue(),
        "https://www.mof.gov.cn/text.pdf",
        "https://www.mof.gov.cn/text.pdf",
        "application/pdf",
    )
    k.repo.save_evidence(ev)
    new = r.model_copy(deep=True, update={"record_id": "pdf-version"})
    new.attachments = [
        Attachment(
            url=ev.source_url,
            label="PDF",
            evidence_id=ev.evidence_id,
            download_state="saved",
            extraction_state="text",
            text="FirstCondition\nAttachmentOnlyException",
        )
    ]
    task = k.repo.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, new)
    k.reindex()
    j = k.create_job("extract", "cit-conditions", [new.record_id])
    p = k.package(j["id"])
    ss = [s for s in p["segments"] if s["source_part"] == "attachment"]
    assert [s["page_number"] for s in ss] == [1, 2]
    assert k.search("AttachmentOnlyException")["items"]
    value = result_for(k, j)
    citation = {
        key: ss[1][key]
        for key in (
            "segment_id",
            "record_id",
            "source_part",
            "evidence_id",
            "text_version_digest",
            "page_number",
        )
    }
    citation.update(start_offset=0, end_offset=len(ss[1]["text"]), quote=ss[1]["text"])
    value["outputs"][0]["content"]["citations"] = [citation]
    assert k.submit(value)["versions"]
    bad = new.model_copy(deep=True, update={"record_id": "outdated-parser"})
    bad.attachments[0].text = "old text"
    k.repo.add_document(task, bad)
    report = k.reindex()
    assert report["issues"][-1]["code"] == "EXTRACTION_CHANGED"
    with pytest.raises(KnowledgeError):
        k.create_job("extract", "cit-conditions", [bad.record_id])


def test_rebuild_failure_preserves_active_generation(kb, monkeypatch):
    k, _ = kb
    before = k.db.execute("SELECT value FROM kw_meta WHERE key='index'").fetchone()[0]
    from ftr.knowledge.indexing import prepare_index

    prepared = prepare_index(k.root)
    prepared[0]["fingerprint"] = "changed"
    with pytest.raises(KnowledgeError):
        k.reindex(prepared)
    assert k.db.execute("SELECT value FROM kw_meta WHERE key='index'").fetchone()[0] == before
    assert k.search("发票")["items"]


def test_stale_retry_freezes_new_baseline_and_repeat_result_key(kb):
    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    value = result_for(k, j)
    result = k.submit(value)
    value["submission_key"] = "duplicate-key"
    assert k.submit(value) == result
    old = k.job(j["id"])["attempt_id"]
    new = k.retry(j["id"])
    assert new["attempt_id"] != old
    assert k.db.execute("SELECT count(*) FROM kw_submissions WHERE accepted=1").fetchone()[0] == 1


def test_unknown_applicability_and_unparsed_attachment(kb):
    k, r = kb
    v = candidate(k, r)
    approve(k, v)
    assert not k.search("符合条件", kind="item", as_of="2026-01-01")["items"]
    r.attachments[0].extraction_state = "unsupported"
    r.record_id = "unsupported"
    task = k.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, r)
    with pytest.raises(KnowledgeError):
        k.create_job("extract", "cit-conditions", [r.record_id])


def test_knowledge_cli_and_web_share_atomic_commands(kb, tmp_path):
    import json

    from ftr.cli import parser, run

    k, _ = kb
    before = (k.root / "database.sqlite3").read_bytes()
    query = run(
        parser().parse_args(["--data-dir", str(k.root), "knowledge", "search", "--query", "发票"])
    )
    assert query.data["items"]
    assert (k.root / "database.sqlite3").read_bytes() == before
    source = tmp_path / "request.json"
    source.write_text(
        json.dumps(
            {
                "job_type": "extract",
                "topic_id": "cit-conditions",
                "records": [k.repo.search("")[0].record_id],
            }
        ),
        encoding="utf-8",
    )
    created = run(
        parser().parse_args(
            ["--data-dir", str(k.root), "knowledge", "job", "create", "--file", str(source)]
        )
    )
    assert created.data["state"] == "queued"


@pytest.mark.skipif(
    __import__("os").environ.get("FTR_WEB_BROWSER") != "1", reason="需要完整 Chromium 模式"
)
@pytest.mark.parametrize("width", [1440, 390])
def test_browser_full_knowledge_wiki_flow(kb, tmp_path, width):
    import json
    import os
    import socket
    import threading
    import time
    from pathlib import Path

    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    from ftr.config import RuntimeSettings, WebSettings
    from ftr.web.app import create_app
    from ftr.web.management import ManagementSession
    from ftr.worker import stop_owned

    k, r = kb
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = RuntimeSettings(
        data_dir=k.root, web=WebSettings(port=port), update_check={"enabled": False}
    )
    session = ManagementSession()
    token = session.issue()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(k.root, management_session=session, runtime_settings=settings),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    def wait(fn):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(0.05)
        pytest.fail("后台流程未完成")

    try:
        wait(lambda: server.started)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/#manage-token=" + token)
            expect(page.locator("#management-mode")).to_contain_text("本机管理")
            page.get_by_role("link", name="知识与 Wiki", exact=True).click()
            expect(page.locator("#wiki-mode")).to_contain_text("本机管理")
            page.locator(".wiki-management summary").click()
            page.locator("#wiki-materials input").first.check()
            page.locator("#wiki-topic").select_option("cit-conditions")
            page.get_by_role("button", name="创建待处理任务", exact=True).click()
            jid = wait(lambda: (k.listing("jobs") or [{}])[0].get("id"))
            page.get_by_role("button", name="刷新", exact=True).click()
            page.get_by_role("button", name="加工任务", exact=True).click()
            page.locator("[data-job]").first.click()
            page.get_by_role("button", name="交给 Codex/Hermes", exact=True).click()
            wait(lambda: k.job(jid)["state"] == "awaiting_result")
            with page.expect_download() as dl:
                page.get_by_role("button", name="下载任务包").click()
            package = json.loads(Path(dl.value.path()).read_text())
            assert package["job_id"] == jid and package["segments"]
            value = result_for(k, k.job(jid))
            source = tmp_path / "host-result.json"
            source.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            page.locator("#wiki-result-file").set_input_files(source)
            wait(lambda: k.job(jid)["state"] == "completed")
            page.get_by_role("button", name="刷新", exact=True).click()
            page.get_by_role("button", name="知识条目", exact=True).click()
            page.locator("[data-object]").first.click()
            vid = k.job(jid)["result"]["versions"][0]
            page.locator("#wiki-review-reason").fill("已核对虚构条件、例外与原文依据")
            page.locator("#wiki-confirm").check()
            page.locator("[data-review][data-decision=approved]").first.click()
            wait(lambda: k.approved(vid))
            page.locator("[data-publish]").first.click()
            oid = k.version(vid)["object_id"]
            wait(lambda: k.object(oid)["current_version"] == vid)
            page.get_by_role("button", name="刷新", exact=True).click()
            page.locator("#wiki-knowledge input").first.check()
            page.locator("#wiki-job-form [name=job_type]").select_option("compile")
            page.locator("#wiki-materials input").first.uncheck()
            page.get_by_role("button", name="创建待处理任务", exact=True).click()
            wjid = wait(
                lambda: next(
                    (j["id"] for j in k.listing("jobs") if j["job_type"] == "compile"), None
                )
            )
            page.get_by_role("button", name="刷新", exact=True).click()
            page.get_by_role("button", name="加工任务", exact=True).click()
            page.locator('[data-job="' + wjid + '"]').click()
            package = k.package(wjid)
            result = {
                "job_id": wjid,
                "attempt_id": package["attempt_id"],
                "input_digest": package["input_digest"],
                "submission_key": "wiki",
                "outputs": [
                    {
                        "kind": "page",
                        "title": "虚构企业所得税优惠专题",
                        "content": {
                            "sections": [
                                {
                                    "section_id": "conditions",
                                    "title": "适用条件",
                                    "text": "按已核对条件申请；资料不全时不适用。",
                                    "knowledge_versions": [vid],
                                }
                            ],
                            "limitations": ["虚构政策，仅供测试"],
                        },
                    }
                ],
            }
            source.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            page.locator("#wiki-result-file").set_input_files(source)
            wait(lambda: k.job(wjid)["state"] == "completed")
            page.get_by_role("button", name="刷新", exact=True).click()
            page.get_by_role("button", name="专题 Wiki", exact=True).click()
            page.locator("[data-object]").first.click()
            pvid = k.job(wjid)["result"]["versions"][0]
            poid = k.version(pvid)["object_id"]
            page.locator("#wiki-review-reason").fill("已核对专题组织、引用和缺口")
            page.locator("#wiki-confirm").check()
            page.locator("[data-review][data-decision=approved]").first.click()
            wait(lambda: k.approved(pvid))
            page.locator("[data-publish]").first.click()
            wait(lambda: k.object(poid)["current_version"] == pvid)
            page.get_by_role("button", name="刷新", exact=True).click()
            expect(page.locator("#wiki-detail")).to_contain_text("当前正式版本")
            with page.expect_download() as dl:
                page.get_by_role("link", name="导出 Markdown", exact=True).first.click()
            assert r.record_id in Path(dl.value.path()).read_text(encoding="utf-8")
            page.reload()
            expect(page.locator("#wiki-mode")).to_contain_text("本机管理")
            assert not errors
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            evidence = os.environ.get("KNOWLEDGE_AUDIT_EVIDENCE")
            if evidence:
                out = Path(evidence)
                out.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(out / f"wiki-{width}.png"), full_page=True)
            browser.close()
    finally:
        stop_owned(k.root)
        server.should_exit = True
        thread.join(5)


def test_atomic_early_migration_failure_rolls_back(tmp_path, monkeypatch):
    import sqlite3

    import ftr.knowledge.schema
    from ftr.repository import SCHEMA

    db = sqlite3.connect(tmp_path / "database.sqlite3")
    db.executescript(SCHEMA)
    db.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    monkeypatch.setattr(
        ftr.knowledge.schema, "SCHEMA", "CREATE TABLE migration_partial(id); INVALID SQL;"
    )
    with pytest.raises(sqlite3.OperationalError):
        Repository(tmp_path)
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not repo.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='attachment_work'"
        ).fetchone()


def test_precheck_is_separate_and_cannot_approve(kb):
    k, r = kb
    v = candidate(k, r)
    j = k.create_job(
        "precheck", "cit-conditions", [], target_version=v, object_id=k.version(v)["object_id"]
    )
    p = k.package(j["id"])
    assert p["sources"] and p["segments"]
    value = {
        "job_id": j["id"],
        "attempt_id": p["attempt_id"],
        "input_digest": p["input_digest"],
        "submission_key": "precheck",
        "outputs": [],
        "findings": ["请核对例外是否完整"],
    }
    k.submit(value)
    assert not k.approved(v)
    assert k.listing("reviews")[0]["actor"] == "ai"
    k.review(v, "rejected", "缺少部分例外，人工退回")
    with pytest.raises(KnowledgeError):
        k.publish(v, 0, None)


def test_interrupted_api_attempt_not_automatically_rebilled(kb):
    import httpx

    from ftr.config import RuntimeSettings
    from ftr.knowledge.processing import execute_next

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    k.db.execute("UPDATE kw_jobs SET state='running' WHERE id=?", (j["id"],))
    k.db.execute("UPDATE kw_attempts SET state='running',calls=1 WHERE id=?", (j["attempt_id"],))
    k.db.commit()

    def network(_):
        pytest.fail("不得自动重新调用模型")

    result = execute_next(RuntimeSettings(data_dir=k.root), transport=httpx.MockTransport(network))
    assert result["state"] == "idle" and k.job(j["id"])["state"] == "uncertain"
    assert (
        k.db.execute("SELECT calls FROM kw_attempts WHERE id=?", (j["attempt_id"],)).fetchone()[0]
        == 1
    )


def test_batch_returns_per_item_results(kb):
    from ftr.config import RuntimeSettings
    from ftr.knowledge.commands import dispatch

    k, r = kb
    v = candidate(k, r)
    result = dispatch(
        k.repo,
        {
            "operation": "batch",
            "arguments": {
                "operations": [
                    {
                        "operation": "review",
                        "arguments": {
                            "version_id": v,
                            "decision": "approved",
                            "reason": "逐项核对完成",
                            "confirmed": True,
                        },
                    },
                    {
                        "operation": "publish",
                        "arguments": {
                            "version_id": v,
                            "expected_revision": 99,
                            "expected_current": None,
                            "confirmed": True,
                        },
                    },
                ]
            },
        },
        RuntimeSettings(data_dir=k.root),
    )
    assert [r["status"] for r in result["results"]] == ["APPLIED", "FAILED"]
    assert k.approved(v)
    assert not k.object(k.version(v)["object_id"])["current_version"]


def test_multi_policy_conditions_exceptions_and_unclaimed(kb):
    k, r = kb
    ev = EvidenceStore(k.root).save(
        b"fictional second policy",
        "https://www.mof.gov.cn/second",
        "https://www.mof.gov.cn/second",
        "text/html",
    )
    k.repo.save_evidence(ev)
    other = r.model_copy(
        deep=True,
        update={
            "record_id": "second-policy",
            "title": "虚构优惠例外",
            "source_url": ev.source_url,
            "canonical_url": ev.source_url,
            "evidence_id": ev.evidence_id,
            "body_text": "虚构例外：不包括材料缺失的甲类主体。",
            "attachments": [],
        },
    )
    task = k.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, other)
    k.reindex()
    k.scan()
    assert any(
        i["reason"] == "unclaimed_material" and i["source_id"] == other.record_id
        for i in k.listing("impacts")
    )
    j = k.create_job("extract", "cit-conditions", [r.record_id, other.record_id])
    value = result_for(k, j)
    s = next(x for x in k.package(j["id"])["segments"] if x["record_id"] == other.record_id)
    anchor = {
        key: s[key]
        for key in (
            "record_id",
            "evidence_id",
            "source_part",
            "segment_id",
            "text_version_digest",
            "page_number",
        )
    }
    anchor.update(start_offset=0, end_offset=len(s["text"]), quote=s["text"])
    value["outputs"][0]["content"]["citations"].append(anchor)
    v = k.submit(value)["versions"][0]
    approve(k, v)
    assert (
        k.db.execute("SELECT count(*) FROM kw_dependencies WHERE version_id=?", (v,)).fetchone()[0]
        == 2
    )
    exported = k.export(k.version(v)["object_id"])
    assert r.record_id in exported and other.record_id in exported


def test_explicit_pdf_refresh_creates_new_unreviewed_version(kb):
    import io

    from pypdf import PdfWriter

    from ftr.config import RuntimeSettings
    from ftr.knowledge.commands import dispatch
    from ftr.knowledge.indexing import prepare_refresh

    k, r = kb
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = io.BytesIO()
    writer.write(buf)
    ev = EvidenceStore(k.root).save(
        buf.getvalue(),
        "https://www.mof.gov.cn/reparse.pdf",
        "https://www.mof.gov.cn/reparse.pdf",
        "application/pdf",
    )
    k.repo.save_evidence(ev)
    record = r.model_copy(deep=True, update={"record_id": "old-parse"})
    record.attachments = [
        Attachment(
            url=ev.source_url,
            label="PDF",
            evidence_id=ev.evidence_id,
            download_state="saved",
            extraction_state="text",
            text="outdated text",
        )
    ]
    task = k.db.execute("SELECT task_id FROM documents LIMIT 1").fetchone()[0]
    k.repo.add_document(task, record)
    prepared = prepare_refresh(k.root, record.record_id)
    result = dispatch(
        k.repo,
        {"operation": "index.refresh", "arguments": {"id": record.record_id}},
        RuntimeSettings(data_dir=k.root),
        prepared,
    )
    assert (
        result["needs_review"]
        and k.repo.get_document(result["record_id"]).quality_state == "collected"
    )
    assert k.repo.get_document(record.record_id).attachments[0].text == "outdated text"


def test_navigation_seed_preserves_user_edits(kb):
    k, _ = kb
    k.import_domains(
        {
            "domains": [
                {"id": "cit-incentives", "name": "人工领域名", "topics": ["cit-conditions"]}
            ],
            "topics": [],
        }
    )
    k.seed()
    assert (
        next(d for d in k.listing("domains") if d["id"] == "cit-incentives")["data"]["name"]
        == "人工领域名"
    )


def test_api_single_executor_and_saved_result_recovery(kb):
    import json
    import threading

    import httpx

    from ftr.config import ProcessingSettings, RuntimeSettings
    from ftr.knowledge.processing import execute_next

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    value = result_for(k, j)
    settings = RuntimeSettings(
        data_dir=k.root,
        processing=ProcessingSettings(
            enabled=True, base_url="http://127.0.0.1:9999/v1", model="fixture", auth_mode="none"
        ),
        network={"proxy_mode": "direct"},
    )
    k.start(j["id"], "api", settings.processing.model_dump())
    entered = threading.Event()
    release = threading.Event()
    outputs = []

    def handler(_):
        entered.set()
        assert release.wait(5)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(value, ensure_ascii=False)},
                    }
                ]
            },
        )

    worker = threading.Thread(
        target=lambda: outputs.append(
            execute_next(settings, transport=httpx.MockTransport(handler))
        )
    )
    worker.start()
    try:
        assert entered.wait(5)
        assert (
            execute_next(
                settings, transport=httpx.MockTransport(lambda _: pytest.fail("重复调用"))
            )["state"]
            == "busy"
        )
    finally:
        release.set()
        worker.join(5)
    assert outputs[0]["state"] == "completed"
    assert (
        k.db.execute("SELECT calls FROM kw_attempts WHERE id=?", (j["attempt_id"],)).fetchone()[0]
        == 1
    )
    assert (k.root / "processing_receipts" / (j["attempt_id"] + ".json")).exists()
    # Simulate a crash after the response was saved but before its result was imported.
    from ftr.knowledge.processing import put_blob
    from ftr.rules import atomic_json

    j2 = k.create_job("extract", "cit-conditions", [r.record_id])
    result2 = result_for(k, j2)
    blob = put_blob(k.root, json.dumps(result2, ensure_ascii=False).encode())
    atomic_json(
        k.root / "processing_receipts" / (j2["attempt_id"] + ".json"),
        {"blob_id": blob, "attempt_id": j2["attempt_id"], "usage": None},
    )
    k.db.execute("UPDATE kw_jobs SET state='running' WHERE id=?", (j2["id"],))
    k.db.execute("UPDATE kw_attempts SET state='running',calls=1 WHERE id=?", (j2["attempt_id"],))
    k.db.commit()
    execute_next(settings, transport=httpx.MockTransport(lambda _: pytest.fail("不应重新调用模型")))
    assert k.job(j2["id"])["state"] == "completed"


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("citation", "CITATION_MISMATCH"),
        ("schema", "INVALID_RESULT"),
        ("missing_blob", "INVALID_RESULT"),
        ("corrupt_blob", "BLOB_INVALID"),
        ("corrupt_receipt", "INVALID_RESULT"),
        ("receipt_shape", "INVALID_RESULT"),
        ("missing_receipt", "REMOTE_OUTCOME_UNKNOWN"),
    ],
)
def test_bad_saved_result_does_not_block_other_recovery_or_queued_job(kb, failure, expected):
    import json

    import httpx

    from ftr.config import ProcessingSettings, RuntimeSettings
    from ftr.knowledge.processing import blob_path, execute_next, put_blob
    from ftr.rules import atomic_json

    k, r = kb
    settings = RuntimeSettings(
        data_dir=k.root,
        processing=ProcessingSettings(
            enabled=True, base_url="http://127.0.0.1:9999/v1", model="fixture", auth_mode="none"
        ),
        network={"proxy_mode": "direct"},
    )
    interrupted = []
    receipts = k.root / "processing_receipts"
    receipts.mkdir()
    for title in ("invalid saved result", "valid saved result"):
        j = k.create_job("extract", "cit-conditions", [r.record_id])
        k.start(j["id"], "api", settings.processing.model_dump())
        value = result_for(k, j, title=title)
        if not interrupted:
            if failure == "citation":
                value["outputs"][0]["content"]["citations"][0]["evidence_id"] = "wrong"
            elif failure == "schema":
                value["outputs"][0]["kind"] = "invalid"
        blob = put_blob(k.root, json.dumps(value, ensure_ascii=False).encode())
        receipt = receipts / (j["attempt_id"] + ".json")
        atomic_json(receipt, {"blob_id": blob, "attempt_id": j["attempt_id"], "usage": None})
        if not interrupted:
            if failure == "missing_blob":
                blob_path(k.root, blob).unlink()
            elif failure == "corrupt_blob":
                blob_path(k.root, blob).write_bytes(b"{}")
            elif failure == "corrupt_receipt":
                receipt.write_text("{", encoding="utf-8")
            elif failure == "receipt_shape":
                receipt.write_text("[]", encoding="utf-8")
            elif failure == "missing_receipt":
                receipt.unlink()
        k.db.execute("UPDATE kw_jobs SET state='running' WHERE id=?", (j["id"],))
        k.db.execute(
            "UPDATE kw_attempts SET state='running',calls=1 WHERE id=?", (j["attempt_id"],)
        )
        interrupted.append(j)
    k.db.commit()
    queued = k.create_job("extract", "cit-conditions", [r.record_id])
    k.start(queued["id"], "api", settings.processing.model_dump())
    result = result_for(k, queued, title="explicitly started job")
    calls = []

    def handler(request):
        package = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(package["job_id"])
        assert package["job_id"] == queued["id"]
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
            },
        )

    out = execute_next(settings, transport=httpx.MockTransport(handler))
    assert out["state"] == k.job(queued["id"])["state"] == "completed"
    assert k.job(interrupted[1]["id"])["state"] == "completed"
    rejected = k.job(interrupted[0]["id"])
    state = "uncertain" if failure == "missing_receipt" else "failed"
    assert rejected["state"] == state
    assert rejected["error"]["code"] == expected
    assert rejected["result"] is None
    attempt = k.db.execute(
        "SELECT state,calls FROM kw_attempts WHERE id=?", (rejected["attempt_id"],)
    ).fetchone()
    assert attempt["state"] == state and attempt["calls"] == 1
    submissions = k.db.execute(
        "SELECT accepted,result_json FROM kw_submissions WHERE attempt_id=?",
        (rejected["attempt_id"],),
    ).fetchall()
    assert not any(row["accepted"] for row in submissions)
    if failure == "citation":
        assert len(submissions) == 1
        assert json.loads(submissions[0]["result_json"])["code"] == expected
    assert {o["title"] for o in k.listing("objects")} == {
        "valid saved result",
        "explicitly started job",
    }
    assert calls == [queued["id"]]
    assert execute_next(settings, transport=httpx.MockTransport(handler))["state"] == "idle"
    assert calls == [queued["id"]]


def test_research_uses_exact_attachment_anchor(kb):
    from ftr.research import Citation, Claim, ResearchDraft, verify_and_render

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    p = k.package(j["id"])
    s = next(s for s in p["segments"] if s["source_part"] == "attachment")
    a = {
        key: s[key]
        for key in (
            "record_id",
            "evidence_id",
            "source_part",
            "segment_id",
            "text_version_digest",
            "page_number",
        )
    }
    a.update(start_offset=0, end_offset=len(s["text"]), quote=s["text"])
    draft = ResearchDraft(
        question="虚构附件引用",
        as_of="2026-10-22",
        claims=[
            Claim(
                text="虚构例外",
                kind="fixture",
                citations=[
                    Citation(
                        record_id=r.record_id,
                        evidence_id=s["evidence_id"],
                        quote=s["text"],
                        anchor=a,
                    )
                ],
            )
        ],
    )
    assert s["evidence_id"] in verify_and_render(k.repo, draft)
    draft.claims[0].citations = [
        Citation(record_id=r.record_id, evidence_id=r.evidence_id, quote=s["text"])
    ]
    with pytest.raises(ValueError):
        verify_and_render(k.repo, draft)


def test_rejected_queued_result_retains_submission_identity(kb):
    import json

    from ftr.config import RuntimeSettings
    from ftr.knowledge.processing import put_blob
    from ftr.management import Command, enqueue, process_commands
    from ftr.models import new_id

    k, r = kb
    j = k.create_job("extract", "cit-conditions", [r.record_id])
    value = result_for(k, j)
    correct = value["outputs"][0]["content"]["citations"][0]["evidence_id"]
    value["outputs"][0]["content"]["citations"][0]["evidence_id"] = "wrong"

    def submit(payload):
        bid = put_blob(k.root, json.dumps(payload, ensure_ascii=False).encode())
        cmd = Command(
            id=new_id(),
            action="knowledge.apply",
            payload={"operation": "job.submit", "arguments": {"blob_id": bid}},
        )
        enqueue(k.root, cmd)
        return process_commands(RuntimeSettings(data_dir=k.root))[-1]

    submit(value)
    assert (
        k.db.execute(
            "SELECT accepted FROM kw_submissions WHERE attempt_id=?", (j["attempt_id"],)
        ).fetchone()[0]
        == 0
    )
    value["outputs"][0]["content"]["citations"][0]["evidence_id"] = correct
    assert submit(value)["code"] == "IDEMPOTENCY_CONFLICT"
    value["submission_key"] = "fixed"
    assert submit(value)["versions"]


def test_task_freezes_manual_content_and_protects_sections(kb):
    k, r = kb
    iv = candidate(k, r)
    approve(k, iv)
    job = k.create_job("compile", "cit-conditions", [], knowledge_versions=[iv])
    p = k.package(job["id"])
    content = {
        "sections": [
            {
                "section_id": "manual",
                "title": "人工章节",
                "text": "人工保留的核对说明",
                "knowledge_versions": [iv],
                "human_maintained": True,
            }
        ],
        "limitations": ["虚构内容"],
    }
    value = {
        "job_id": job["id"],
        "attempt_id": p["attempt_id"],
        "input_digest": p["input_digest"],
        "submission_key": "page",
        "outputs": [{"kind": "page", "title": "虚构专题", "content": content}],
    }
    pv = k.submit(value)["versions"][0]
    approve(k, pv)
    obj = k.object(k.version(pv)["object_id"])
    revision = k.create_job(
        "revise", "cit-conditions", [], object_id=obj["id"], knowledge_versions=[iv]
    )
    package = k.package(revision["id"])
    assert (
        package["existing_content"]["formal_version"]["content"]["sections"][0]["text"]
        == "人工保留的核对说明"
    )
    value.update(
        job_id=revision["id"],
        attempt_id=package["attempt_id"],
        input_digest=package["input_digest"],
    )
    value["outputs"][0]["object_id"] = obj["id"]
    value["outputs"][0]["content"]["sections"][0]["text"] = "AI想覆盖"
    with pytest.raises(KnowledgeError, match="人工维护"):
        k.submit(value)
    assert k.object(obj["id"])["current_version"] == pv
