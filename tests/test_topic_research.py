"""Topic research contracts use isolated stores and deterministic public-shaped fixtures."""

from datetime import date

import pytest
from test_knowledge import (
    candidate,
    result_for,
)
from test_knowledge import kb as kb  # noqa: PLC0414 -- pytest fixture re-export

from ftr.models import TaskRequest
from ftr.repository import Repository


def targeted_request(**changes):
    data = {
        "source_ids": ["mof"],
        "date_from": date(2015, 1, 1),
        "date_to": date(2026, 11, 6),
        "mode": "targeted",
        "official_urls": [
            {
                "source_id": "mof",
                "url": "https://szs.mof.gov.cn/example.htm",
                "title": "公开政策",
                "topic_id": "cit-rd",
                "reason": "研发专题依据",
            }
        ],
    }
    data.update(changes)
    return TaskRequest.model_validate(data)


def test_targeted_queue_is_frozen_without_listing_scan(tmp_path):
    request = targeted_request(idempotency_key="one")
    repo = Repository(tmp_path)
    try:
        task = repo.create_task(request)
        assert repo.create_task(request) == task
        assert len(repo.pending_refs(task, "mof")) == 1
        run = repo.source_run(task, "mof")
        assert run["discovery_done"] == 1
        assert run["pages_count"] == 0
    finally:
        repo.close()


@pytest.mark.parametrize(
    "url", ["https://example.com/a", "http://szs.mof.gov.cn/a", "https://u:p@szs.mof.gov.cn/a"]
)
def test_targeted_input_rejects_outside_or_credential_urls(url):
    with pytest.raises(ValueError):
        targeted_request(
            official_urls=[
                {"source_id": "mof", "url": url, "title": "x", "topic_id": "cit-rd", "reason": "x"}
            ]
        )


def test_preview_can_compile_candidates_but_cannot_publish(kb):
    k, record = kb
    item = candidate(k, record)
    job = k.create_job(
        "compile", "cit-conditions", [], knowledge_versions=[item], output_mode="preview"
    )
    result = result_for(k, k.create_job("extract", "cit-conditions", [record.record_id]))
    package = k.package(job["id"])
    result.update(
        job_id=package["job_id"],
        attempt_id=package["attempt_id"],
        input_digest=package["input_digest"],
    )
    result["outputs"] = [
        {
            "kind": "page",
            "title": "候选专题",
            "content": {
                "sections": [
                    {
                        "section_id": "s",
                        "title": "条件",
                        "text": "仅满足条件时适用",
                        "knowledge_versions": [item],
                    }
                ],
                "limitations": [],
            },
        }
    ]
    page = k.submit(result)["versions"][0]
    obj = k.object(k.version(page)["object_id"])
    assert "候选预览" in k.export(obj["id"], page, preview=True)
    with pytest.raises(ValueError):
        k.publish(page, obj["edit_revision"], obj["current_version"])


def test_open_ended_applicability_requires_explicit_evidence_state(kb):
    from test_knowledge import approve

    k, record = kb
    item = candidate(k, record)
    obj = k.object(k.version(item)["object_id"])
    content = k.version(item)["content"]
    content.update(valid_from="2023-01-01", valid_to_kind="open_ended", applicability_verified=True)
    edited = k.edit(obj["id"], content, obj["edit_revision"], title="无固定终止日期")
    approve(k, edited["version_id"])
    assert k.search("无固定终止日期", kind="item", as_of="2026-11-06")["items"]


def test_legacy_report_checks_changed_source(kb):
    from ftr.models import digest, new_id
    from ftr.research import ResearchDraft, verify_and_render

    k, record = kb
    task = k.db.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
    changed = record.model_copy(deep=True)
    changed.record_id = new_id()
    changed.body_text += "本条已经变化。"
    changed.body_sha256 = digest(changed.body_text.encode())
    k.repo.add_document(task, changed)
    draft = ResearchDraft.model_validate(
        {
            "question": "可否申请",
            "as_of": "2026-11-06",
            "claims": [
                {
                    "text": "可申请",
                    "kind": "source_rule",
                    "citations": [
                        {
                            "record_id": record.record_id,
                            "evidence_id": record.evidence_id,
                            "quote": "符合条件的主体可以申请。",
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(ValueError, match="变化|受限"):
        verify_and_render(k.repo, draft)


def test_report_exposes_required_authority_and_year_gap(kb):
    from ftr.research import ResearchDraft, verify_and_render

    k, record = kb
    draft = ResearchDraft.model_validate(
        {
            "question": "税负是否5%",
            "as_of": "2026-11-06",
            "context": {"required_authorities": ["中华人民共和国增值税法"]},
            "claims": [
                {
                    "text": "税负5%",
                    "kind": "synthesis",
                    "citations": [
                        {
                            "record_id": record.record_id,
                            "evidence_id": record.evidence_id,
                            "quote": "符合条件的主体可以申请。",
                        }
                    ],
                }
            ],
        }
    )
    rendered = verify_and_render(k.repo, draft)
    assert "指定法源未匹配" in rendered
    assert "所属期" in rendered
    assert "待核实" in rendered
    assert "> 符合条件的主体可以申请。" in rendered
    assert "字符区间" in rendered


def test_policy_identity_preserves_issuer_year_and_joint_order():
    from ftr.policy import policy_identity

    first = policy_identity("财政部 税务总局公告2023年第12号", "a", "one")
    mirror = policy_identity("税务总局 财政部公告２０２３年第１２号", "a", "two")
    assert first["policy_key"] == mirror["policy_key"]
    for number in ("国家税务总局公告2023年第12号", "财政部 税务总局公告2022年第12号"):
        assert policy_identity(number, "a", "three")["policy_key"] != first["policy_key"]
    assert (
        policy_identity(None, "a", "one")["policy_key"]
        != policy_identity(None, "a", "two")["policy_key"]
    )


def test_targeted_collector_skips_listing_and_replays_without_download(tmp_path, monkeypatch):
    from ftr.runtime import Collector

    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, url):
            calls.append(url)
            return (
                "<h1>公开政策</h1><div class='TRS_Editor'>这是完整测试正文。</div>".encode(),
                url,
                "text/html",
            )

        def close(self):
            pass

    def forbidden_listing(*args):
        raise AssertionError("定向任务不能枚举栏目")

    monkeypatch.setattr("ftr.runtime.BoundedClient", Client)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *args: "szs.mof.gov.cn")
    monkeypatch.setattr("ftr.adapters.mof.MofAdapter.discover", forbidden_listing)
    collector = Collector(tmp_path, proxy=None)
    try:
        result = collector.collect(targeted_request(idempotency_key="targeted"))
        assert result.data["processed_documents"] == 1
        assert result.data["report"]["sources"][0]["discovery_strategy"] == "explicit_url_list"
        collector.resume(result.task_id)
        assert calls == ["https://szs.mof.gov.cn/example.htm"]
    finally:
        collector.close()


def test_old_task_idempotency_keeps_frozen_json(tmp_path):
    import json

    from ftr.models import digest

    repo = Repository(tmp_path)
    try:
        request = TaskRequest(
            source_ids=["mof"],
            date_from=date(2020, 1, 1),
            date_to=date(2020, 1, 2),
            idempotency_key="old",
        )
        task = repo.create_task(request)
        old = request.model_dump(mode="json")
        old.pop("official_urls")
        raw = json.dumps(old, ensure_ascii=False)
        repo.db.execute(
            "UPDATE tasks SET request_json=?,request_digest=? WHERE id=?",
            (raw, digest(raw.encode()), task),
        )
        repo.db.commit()
        assert repo.create_task(request) == task
        assert repo.task(task)["request_json"] == raw
    finally:
        repo.close()


def test_empty_report_is_not_ai_checked(kb):
    from ftr.research import ResearchDraft, assess_and_render

    k, _ = kb
    result = assess_and_render(
        k.repo, ResearchDraft(question="无来源", as_of="2026-11-06", claims=[])
    )
    assert result["report_state"] == "needs_review"
    assert "尚无可核对研究陈述" in result["markdown"]


def test_precheck_includes_temporal_attachment_anchor(kb):
    k, record = kb
    job = k.create_job("extract", "cit-conditions", [record.record_id])
    package = k.package(job["id"])
    segment = next(s for s in package["segments"] if s["source_part"] == "attachment")
    temporal_anchor = {
        key: segment[key]
        for key in [
            "segment_id",
            "record_id",
            "evidence_id",
            "source_part",
            "text_version_digest",
            "page_number",
        ]
    }
    temporal_anchor.update(quote=segment["text"], start_offset=0, end_offset=len(segment["text"]))
    result = result_for(k, job)
    result["outputs"][0]["content"]["temporal_basis"] = [
        {"kind": "transition", "value": "虚构时间依据", "citations": [temporal_anchor]}
    ]
    version = k.submit(result)["versions"][0]
    check_job = k.create_job("precheck", "cit-conditions", [], target_version=version)
    check_package = k.package(check_job["id"])
    assert temporal_anchor["segment_id"] in {s["segment_id"] for s in check_package["segments"]}
    submission = {key: check_package[key] for key in ["job_id", "attempt_id", "input_digest"]}
    submission.update(
        submission_key="time-anchor",
        outputs=[],
        checks=[
            {
                "statement": result["outputs"][0]["content"]["statement"],
                "dimension": "temporal",
                "status": "insufficient",
                "reason": "虚构附件没有真实期间，保留缺口",
                "citations": [temporal_anchor],
            }
        ],
    )
    assert k.submit(submission)["checks"][0]["status"] == "insufficient"


def test_report_preview_does_not_block_other_http_requests(kb, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from ftr.web.app import create_app
    from ftr.web.management import ManagementSession

    k, _ = kb
    entered, release = threading.Event(), threading.Event()

    def slow_preview(*args):
        entered.set()
        assert release.wait(5)
        return {"report_state": "needs_review", "markdown": "等待核对"}

    monkeypatch.setattr("ftr.research.assess_and_render", slow_preview)
    session = ManagementSession()
    with (
        TestClient(
            create_app(k.root, management_session=session),
            base_url="http://127.0.0.1",
            client=("127.0.0.1", 12345),
        ) as client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        headers = {"Origin": "http://127.0.0.1"}
        assert (
            client.post(
                "/api/manage/session", json={"token": session.issue()}, headers=headers
            ).status_code
            == 200
        )
        pending = executor.submit(
            client.post,
            "/api/manage/research/preview",
            json={"question": "示例", "as_of": "2026-11-06", "claims": []},
            headers=headers,
        )
        try:
            assert entered.wait(2)
            other = executor.submit(client.get, "/api/knowledge/overview")
            assert other.result(timeout=2).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=2).status_code == 200


def test_v5_accepted_result_replays_after_migration(kb):
    import json

    from ftr.knowledge.indexing import fingerprint
    from ftr.knowledge.models import ProcessingResult
    from ftr.knowledge.service import Knowledge

    k, record = kb
    job = k.create_job("extract", "cit-conditions", [record.record_id])
    submission = result_for(k, job)
    accepted = k.submit(submission)
    legacy = ProcessingResult.model_validate(submission).model_dump(
        mode="json", exclude={"submission_key", "checks"}
    )
    old_result = {key: value for key, value in accepted.items() if key != "checks"}
    k.db.execute(
        "UPDATE kw_submissions SET result_digest=?,result_json=? WHERE attempt_id=?",
        (fingerprint(legacy), json.dumps(old_result), submission["attempt_id"]),
    )
    k.db.execute("DROP TABLE policy_records")
    k.db.execute("PRAGMA user_version=5")
    k.db.commit()
    count = k.db.execute("SELECT COUNT(*) FROM kw_versions").fetchone()[0]
    k.repo.close()
    migrated = Repository(k.root)
    try:
        assert Knowledge(migrated).submit(submission) == old_result
        assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert migrated.db.execute("SELECT COUNT(*) FROM kw_versions").fetchone()[0] == count
        changed = {**submission, "findings": ["不同内容"]}
        with pytest.raises(ValueError, match="同一提交键"):
            Knowledge(migrated).submit(changed)
    finally:
        migrated.close()


def test_legal_relation_stays_pending_until_real_revision_publication(kb):
    from test_knowledge import approve

    from ftr.evidence import EvidenceStore
    from ftr.knowledge.commands import dispatch
    from ftr.models import digest, new_id

    k, record = kb
    original = candidate(k, record)
    approve(k, original)
    evidence = EvidenceStore(k.root).save(
        "虚构新规改变原条件。".encode(),
        "https://www.mof.gov.cn/new",
        "https://www.mof.gov.cn/new",
        "text/html",
    )
    k.repo.save_evidence(evidence)
    new = record.model_copy(
        deep=True,
        update={
            "record_id": new_id(),
            "source_url": evidence.source_url,
            "canonical_url": evidence.source_url,
            "title": "虚构修订文件",
            "evidence_id": evidence.evidence_id,
            "body_text": "虚构新规改变原条件。",
            "body_sha256": digest("虚构新规改变原条件。".encode()),
            "attachments": [],
        },
    )
    task = k.db.execute("SELECT id FROM tasks LIMIT 1").fetchone()[0]
    k.repo.add_document(task, new)
    k.reindex()
    job = k.create_job("extract", "cit-conditions", [new.record_id, record.record_id])
    result = result_for(k, job)
    source = next(s for s in k.package(job["id"])["segments"] if s["record_id"] == new.record_id)
    anchor = {
        key: source[key]
        for key in [
            "segment_id",
            "record_id",
            "evidence_id",
            "source_part",
            "text_version_digest",
            "page_number",
        ]
    }
    anchor.update(quote=source["text"], start_offset=0, end_offset=len(source["text"]))
    result["outputs"][0]["content"]["citations"] = [anchor]
    result["outputs"][0]["content"]["legal_relations"] = [
        {
            "kind": "amends",
            "target_record_id": record.record_id,
            "scope": "原条件",
            "citations": [anchor],
        }
    ]
    k.submit(result)
    impact = next(
        i
        for i in k.listing("impacts")
        if i["reason"] == "legal_relation_candidate" and i["version_id"] == original
    )
    assert k.dependency(original) == "needs_review"
    args = {"id": impact["id"], "topic_id": "cit-conditions", "reason": "处理虚构修订"}
    created = dispatch(k.repo, {"operation": "impact.resolve", "arguments": args}, None)
    assert k.row("kw_impacts", impact["id"])["state"] == "claimed"
    assert k.dependency(original) == "needs_review"
    with pytest.raises(ValueError, match="新版本"):
        dispatch(
            k.repo,
            {
                "operation": "impact.resolve",
                "arguments": {**args, "resolution": "revision_published"},
            },
            None,
        )
    revision_job = created["job"]
    revised = result_for(k, revision_job, object_id=k.version(original)["object_id"])
    revised["outputs"][0]["content"]["statement"] = "按虚构新规核对条件"
    revised["outputs"][0]["content"]["citations"] = [anchor]
    replacement = k.submit(revised)["versions"][0]
    approve(k, replacement)
    closed = dispatch(
        k.repo,
        {"operation": "impact.resolve", "arguments": {**args, "resolution": "revision_published"}},
        None,
    )
    assert closed == {"state": "resolved", "version_id": replacement}
    assert k.version(original)["content"]["statement"] == "符合条件才可申请"
    assert k.dependency(replacement) == "checked"


def test_empty_url_file_does_not_fall_back_to_column_collection(tmp_path):
    from ftr.cli import parser, run

    urls = tmp_path / "empty.json"
    urls.write_text("[]")
    data = tmp_path / "untouched"
    args = parser().parse_args(
        [
            "--data-dir",
            str(data),
            "collect",
            "--urls-file",
            str(urls),
            "--date-from",
            "2026-01-01",
            "--date-to",
            "2026-01-31",
        ]
    )
    with pytest.raises(ValueError, match="不能为空"):
        run(args)
    assert not data.exists()


def test_legacy_free_text_cutoff_remains_readable_with_gap(kb):
    from ftr.research import ResearchDraft, assess_and_render

    k, _ = kb
    report = assess_and_render(
        k.repo, ResearchDraft(question="旧稿", as_of="截至已收录资料", claims=[])
    )
    assert "截至已收录资料" in report["markdown"]
    assert "截止日需核实" in report["markdown"]
    assert report["report_state"] == "needs_review"
