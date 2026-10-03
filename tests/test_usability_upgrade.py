"""Audit regressions; isolated databases and source fixtures, never business data."""

import json
from datetime import date
from importlib.resources import files

import pytest
from pydantic import ValidationError

from ftr.cli import parser, run
from ftr.models import DecisionRequest, SemanticDecision, TaskRequest, digest
from ftr.network import BoundedClient, TransientFailure
from ftr.repository import Repository
from ftr.rules import Rules, extract_listing
from ftr.runtime import Collector


def queued(root, *, missing_date=False, attachment=True):
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )
    c = Collector(root, proxy=None)
    task = c.repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    refs, _ = extract_listing(Rules(source_id="mof"), sample["listing"].encode(), sample["url"], 1)
    refs = [refs[0]]
    if missing_date:
        refs[0].listing_date = None
    c.repo.save_page(task, "mof", refs, None)
    detail = sample["detail"]
    if attachment:
        detail = detail.replace(
            "</div>", '<a href="https://www.mof.gov.cn/form.xls">附件</a></div>', 1
        )
    return c, task, detail.encode()


def test_unknown_request_field_rejected_without_writing(tmp_path):
    payload = {"sources": ["mof"], "date_from": "2026-09-01", "date_to": "2026-09-30"}
    with pytest.raises(ValidationError):
        TaskRequest.model_validate(payload)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    root = tmp_path / "data"
    with pytest.raises(ValidationError):
        run(parser().parse_args(["--data-dir", str(root), "collect", "--request", str(path)]))
    assert not root.exists()


def test_attachment_recovery_new_collector_preserves_old_version(tmp_path, monkeypatch):
    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    calls = []

    def fail(_, url):
        calls.append(url)
        if url.endswith(".xls"):
            raise TransientFailure("HTTP 503")
        return detail, url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", fail)
    first = c.resume(task)
    old = c.repo.db.execute("SELECT id,record_json FROM documents").fetchone()
    assert first.status == "PARTIAL"
    c.close()
    calls.clear()
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda _, url: calls.append(url) or (b"xls", url, "application/vnd.ms-excel"),
    )
    c = Collector(tmp_path, proxy=None)
    try:
        second = c.resume(task)
        assert calls == ["https://www.mof.gov.cn/form.xls"]
        assert second.status != "PARTIAL"
        assert c.repo.get_document(old["id"]).model_dump_json() == old["record_json"]
        latest = c.repo.db.execute(
            "SELECT record_json FROM documents ORDER BY extraction_version DESC LIMIT 1"
        ).fetchone()[0]
        assert json.loads(latest)["attachments"][0]["download_state"] == "saved"
        calls.clear()
        c.resume(task)
        assert calls == []
        assert c.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    finally:
        c.close()


def test_missing_date_is_collectable(tmp_path, monkeypatch):
    c, task, detail = queued(tmp_path, missing_date=True, attachment=False)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(BoundedClient, "get", lambda _, url: (detail, url, "text/html"))
    try:
        result = c.resume(task)
        record = json.loads(c.repo.db.execute("SELECT record_json FROM documents").fetchone()[0])
        assert record["listing_date"] is None
        assert record["quality_state"] == "collected"
        assert "筛选日期缺失，范围待确认" not in record["limitations"]
        assert result.status == "WAITING_DECISION"
    finally:
        c.close()


def test_reject_is_rejected(tmp_path):
    c, task, detail = queued(tmp_path, attachment=False)
    from ftr.adapters.common import parse_detail

    ref = c.repo.pending_refs(task, "mof")[0]["ref_json"]
    from ftr.models import DiscoveredRef

    record = parse_detail(DiscoveredRef.model_validate_json(ref), detail, "raw")
    c.repo.add_document(task, record)
    request = DecisionRequest(
        task_id=task,
        record_id=record.record_id,
        input_digest=digest(record.model_dump_json().encode()),
        excerpt="正文",
        evidence_id="raw",
    )
    c.repo.add_decision(request)
    try:
        assert (
            c.repo.submit_decision(
                SemanticDecision(
                    decision_id=request.decision_id,
                    input_digest=request.input_digest,
                    result="REJECT",
                    reasons=["不适用"],
                    evidence_ids=["raw"],
                )
            ).quality_state
            == "rejected"
        )
    finally:
        c.close()


def test_search_never_recovers_or_writes(tmp_path):
    repo = Repository(tmp_path)
    task = repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    repo.set_task_state(task, "RUNNING")
    repo.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    run(parser().parse_args(["--data-dir", str(tmp_path), "search", "--query", "测试"]))
    assert (tmp_path / "database.sqlite3").read_bytes() == before


@pytest.mark.parametrize(
    "error,category,status",
    [
        ("HTTP 429 请求频率受限", "ACCESS_RESTRICTED", 429),
        ("HTTP 403 禁止访问", "ACCESS_RESTRICTED", 403),
        ("ConnectTimeout", "TRANSIENT_NETWORK", None),
        ("浏览器启动失败", "BROWSER_ERROR", None),
        ("未识别错误", "UNKNOWN", None),
    ],
)
def test_failure_facts_survive_new_process_and_are_redacted(
    tmp_path, monkeypatch, error, category, status
):
    from ftr.diagnostics import task_report
    from ftr.network import AccessBlocked

    c, task, _ = queued(tmp_path, attachment=False)
    exc_type = (
        AccessBlocked
        if category == "ACCESS_RESTRICTED"
        else TransientFailure
        if category == "TRANSIENT_NETWORK"
        else RuntimeError
    )
    fid = c._failure(
        task,
        "mof",
        exc_type(
            error
            + "\nCookie: session=secret\nAuthorization: Bearer abc\npassword=hidden https://user:pass@www.mof.gov.cn/path?token=value"
        ),
        "discover",
        url="https://u:p@www.mof.gov.cn/path?token=value",
    )
    c.close()
    repo = Repository(tmp_path, readonly=True)
    try:
        report = task_report(repo.db, task)
        failure = report["failures"][0]
        assert failure["failure_id"] == fid
        assert failure["category"] == category
        assert failure["details"]["http_status"] == status
        assert failure["next_step"]
        assert failure["certainty"] == (
            "insufficient_evidence" if category == "UNKNOWN" else "confirmed"
        )
        assert failure["url"] == "https://www.mof.gov.cn/path"
        encoded = json.dumps(report)
        assert all(
            value not in encoded
            for value in (
                "session=secret",
                "Bearer abc",
                "password=hidden",
                "token=value",
                "user:pass",
                "u:p@",
            )
        )
        assert report["unseen_matches"] == 0  # This fixture already enumerated its list.
    finally:
        repo.close()


def test_old_missing_failure_cause_is_not_invented(tmp_path):
    from ftr.diagnostics import task_report

    c, task, _ = queued(tmp_path, attachment=False)
    c.repo.add_failure(
        task, "mof", "ACCESS_RESTRICTED", {"stage": "discover", "error_type": "AccessBlocked"}
    )
    try:
        f = task_report(c.repo.db, task)["failures"][0]
        assert f["certainty"] == "insufficient_evidence"
        assert "证据不足" in f["reason"]
        assert "429" not in f["label"]
    finally:
        c.close()


def test_attachment_access_limit_stops_source_and_can_resume(tmp_path, monkeypatch):
    from ftr.network import AccessBlocked

    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def get(_, url):
        if url.endswith(".xls"):
            raise AccessBlocked("HTTP 403")
        return detail, url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", get)
    first = c.resume(task)
    assert first.status == "PARTIAL"
    assert first.data["report"]["missing_items"][0]["kind"] == "attachment"
    assert first.data["report"]["failures"][0]["details"]["http_status"] == 403
    c.close()
    c = Collector(tmp_path, proxy=None)
    monkeypatch.setattr(
        BoundedClient, "get", lambda _, url: (b"xls", url, "application/octet-stream")
    )
    try:
        second = c.resume(task)
        assert second.data["report"]["completion"]["downloads"] == "COMPLETE"
        assert second.data["report"]["failures"][0]["resolved"]
        assert second.data["report"]["completion"]["readability"] == "LIMITED"
    finally:
        c.close()


def test_attachment_budget_recovers_only_missing_and_cancel_blocks(tmp_path, monkeypatch):
    from ftr.config import RuntimeSettings

    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda _, url: (detail if not url.endswith(".xls") else b"x" * 1000, url, "text/html"),
    )
    c.settings.collection.max_bytes_per_source = len(detail) + 5
    first = c.resume(task)
    assert first.status == "PARTIAL"
    assert first.data["pending_attachments"] == 1
    assert first.data["report"]["failures"] == []
    c.close()
    calls = []
    c = Collector(tmp_path, RuntimeSettings(data_dir=tmp_path), proxy=None)
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda _, url: calls.append(url) or (b"xls", url, "application/octet-stream"),
    )
    second = c.resume(task)
    assert calls == ["https://www.mof.gov.cn/form.xls"]
    assert second.data["pending_attachments"] == 0
    c.repo.request_stop(task, True)
    calls.clear()
    assert c.resume(task).status == "CANCELLED"
    assert calls == []
    c.close()


def test_pagination_old_pinned_unknown_and_boundary_do_not_end_early(tmp_path, monkeypatch):
    from ftr.config import load_sources

    entry = load_sources().sources["mof"].entry
    dates = ["2024-01-01", None, "2026-09-01", "2026-09-30", "2026-10-01"]
    lists = {}
    for n, value in enumerate(dates):
        url = entry if n == 0 else entry + f"index_{n}.htm"
        text = f'<ul><li><a href="https://www.mof.gov.cn/zhengcefabu/202609/t20260901_{n}.htm">通知 {n}</a><span>{value or ""}</span></li></ul><script>var countPage = 5</script>'
        lists[url] = text.encode()
    calls = []

    def get(_, url):
        calls.append(url)
        return (
            (
                lists[url]
                if url in lists
                else '<h2>关于发布政策的通知</h2><div class="TRS_Editor"><p>政策正文</p></div>'.encode()
            ),
            url,
            "text/html",
        )

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    c = Collector(tmp_path, proxy=None)
    try:
        result = c.collect(
            TaskRequest(
                source_ids=["mof"],
                date_from=date(2026, 9, 1),
                date_to=date(2026, 9, 30),
                max_pages=3,
            )
        )
        assert result.status == "PARTIAL"
        assert result.data["report"]["unseen_matches"] is None
        rows = c.repo.db.execute(
            "SELECT request_url,final_url FROM listing_pages ORDER BY page"
        ).fetchall()
        assert [r[0] for r in rows] == [entry, entry + "index_1.htm", entry + "index_2.htm"]
        assert [r[1] for r in rows] == [r[0] for r in rows]
        calls.clear()
        result = c.resume(result.task_id)
        assert result.data["report"]["completion"]["coverage"] == "COMPLETE"
        assert result.data["report"]["sources"][0]["documents_saved"] == 3
        assert result.data["report"]["sources"][0]["date_unknown_documents"] == 1
        assert entry not in calls
        assert (
            c.repo.db.execute(
                "SELECT COUNT(*) FROM discovered WHERE state='OUT_OF_SCOPE'"
            ).fetchone()[0]
            == 2
        )
    finally:
        c.close()


@pytest.mark.parametrize(
    "decision,expected",
    [("PASS", "validated"), ("REJECT", "rejected"), ("UNCERTAIN", "quarantined")],
)
def test_review_mapping_and_idempotency(tmp_path, decision, expected):
    from ftr.adapters.common import parse_detail
    from ftr.models import DiscoveredRef

    c, task, detail = queued(tmp_path, attachment=False)
    ref = DiscoveredRef.model_validate_json(c.repo.pending_refs(task, "mof")[0]["ref_json"])
    record = parse_detail(ref, detail, "raw")
    c.repo.add_document(task, record)
    req = DecisionRequest(
        task_id=task,
        record_id=record.record_id,
        input_digest=digest(record.model_dump_json().encode()),
        excerpt="正文",
        evidence_id="raw",
    )
    c.repo.add_decision(req)
    value = SemanticDecision(
        decision_id=req.decision_id,
        input_digest=req.input_digest,
        result=decision,
        reasons=["判断"],
        evidence_ids=["raw"],
    )
    try:
        assert c.repo.submit_decision(value).quality_state == expected
        assert c.repo.submit_decision(value).quality_state == expected
        with pytest.raises(ValueError):
            c.repo.submit_decision(value.model_copy(update={"input_digest": "stale"}))
        assert (len(c.repo.search("")) == 1) == (decision == "PASS")
    finally:
        c.close()


def test_old_schema_readonly_migration_backup_and_legacy_request(tmp_path):
    import sqlite3

    from ftr.backup import create_backup, restore_backup
    from ftr.diagnostics import task_report

    c, task, _ = queued(tmp_path, attachment=False)
    c.close()
    db = tmp_path / "database.sqlite3"
    with sqlite3.connect(db) as con:
        con.execute("DROP TABLE attachment_work")
        con.execute("DROP TABLE listing_pages")
        con.execute("PRAGMA user_version=0")
        payload = json.loads(
            con.execute("SELECT request_json FROM tasks WHERE id=?", (task,)).fetchone()[0]
        )
        payload["legacy_field"] = "retained"
        con.execute("UPDATE tasks SET request_json=? WHERE id=?", (json.dumps(payload), task))
    before = db.read_bytes()
    repo = Repository(tmp_path, readonly=True)
    assert task_report(repo.db, task)["request"]["legacy_field"] == "retained"
    assert TaskRequest.from_saved(repo.task(task)["request_json"]).source_ids == ["mof"]
    old_digest = repo.task(task)["request_digest"]
    repo.close()
    assert db.read_bytes() == before
    backup = tmp_path.parent / (tmp_path.name + "-backup")
    create_backup(tmp_path, backup)
    restored = tmp_path.parent / (tmp_path.name + "-restored")
    restore_backup(backup, restored)
    repo = Repository(restored)
    try:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert repo.task(task)["request_digest"] == old_digest
        assert "legacy_field" in repo.task(task)["request_json"]
    finally:
        repo.close()


def test_all_read_commands_under_writer_lock_preserve_database(tmp_path):
    from ftr.runtime import data_lock

    c, task, _ = queued(tmp_path, attachment=False)
    c.repo.set_task_state(task, "RUNNING")
    c.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    commands = [
        ["search", "--query", "测试"],
        ["decision", "list", "--task", task],
        ["task", "status", "--task", task],
        ["task", "report", "--task", task],
        ["task", "missing", "--task", task],
        ["task", "list"],
        ["research", "prepare", "--query", "测试"],
    ]
    with data_lock(tmp_path):
        for command in commands:
            run(parser().parse_args(["--data-dir", str(tmp_path), *command]))
    assert (tmp_path / "database.sqlite3").read_bytes() == before


def test_filtered_web_retains_unknown_dates_and_report_is_readonly(tmp_path, monkeypatch):
    from ftr.web.query import ReadQueries

    c, task, detail = queued(tmp_path, missing_date=True, attachment=False)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(BoundedClient, "get", lambda _, url: (detail, url, "text/html"))
    c.resume(task)
    c.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    q = ReadQueries(tmp_path)
    result = q.policies(date_from="2026-09-01", date_to="2026-09-30")
    assert result["date_unknown_count"] == 1
    assert result["total"] == 1
    assert result["items"][0]["listing_date"] is None
    assert q.task(task)["item"]["report"]["sources"][0]["date_unknown_documents"] == 1
    assert (tmp_path / "database.sqlite3").read_bytes() == before


def test_saved_and_missing_attachments_recover_without_redownloading_saved(tmp_path, monkeypatch):
    from ftr.adapters.common import parse_detail
    from ftr.models import Attachment, DiscoveredRef

    c, task, detail = queued(tmp_path, attachment=False)
    ref = DiscoveredRef.model_validate_json(c.repo.pending_refs(task, "mof")[0]["ref_json"])
    record = parse_detail(ref, detail, "raw")
    record.attachments = [
        Attachment(
            url="https://www.mof.gov.cn/saved.xls",
            label="已有",
            download_state="saved",
            evidence_id="old",
            extraction_state="unsupported",
        ),
        Attachment(
            url="https://www.mof.gov.cn/missing.xls",
            label="缺失",
            download_state="failed",
            extraction_state="failed",
        ),
    ]
    record.limitations = ["部分附件未保存"]
    record.quality_state = "quarantined"
    c.repo.add_document(task, record)
    c.repo.set_ref_state(task, "mof", ref.url, "SAVED")
    old_json = c.repo.get_document(record.record_id).model_dump_json()
    c.close()
    calls = []
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda _, url: calls.append(url) or (b"new", url, "application/octet-stream"),
    )
    c = Collector(tmp_path, proxy=None)
    try:
        result = c.resume(task)
        assert calls == ["https://www.mof.gov.cn/missing.xls"]
        assert result.data["report"]["completion"]["downloads"] == "COMPLETE"
        assert c.repo.get_document(record.record_id).model_dump_json() == old_json
        pending = c.repo.pending_decisions(task)
        assert len(pending) == 1 and pending[0].record_id != record.record_id
    finally:
        c.close()


def test_legacy_failed_attachment_rebuilt_on_write_only(tmp_path, monkeypatch):
    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def fail(_, url):
        if url.endswith(".xls"):
            raise TransientFailure("HTTP 503")
        return detail, url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", fail)
    c.resume(task)
    c.repo.db.execute("DELETE FROM attachment_work")
    c.repo.db.commit()
    c.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    assert run(
        parser().parse_args(["--data-dir", str(tmp_path), "task", "missing", "--task", task])
    ).data["items"]
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    calls = []
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda _, url: calls.append(url) or (b"xls", url, "application/octet-stream"),
    )
    c = Collector(tmp_path, proxy=None)
    try:
        c.resume(task)
        assert calls == ["https://www.mof.gov.cn/form.xls"]
    finally:
        c.close()


@pytest.mark.skipif(
    __import__("os").environ.get("FTR_WEB_BROWSER") != "1", reason="按需运行真实浏览器验收"
)
def test_browser_diagnosis_and_unknown_date_group(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx
    from playwright.sync_api import expect, sync_playwright

    from ftr.network import AccessBlocked

    c, task, detail = queued(tmp_path, missing_date=True, attachment=False)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(BoundedClient, "get", lambda _, url: (detail, url, "text/html"))
    c.resume(task)
    c._failure(
        task,
        "mof",
        AccessBlocked("HTTP 429 请求频率受限"),
        "discover",
        url="https://www.mof.gov.cn/",
    )
    c.repo.db.execute(
        "UPDATE source_runs SET discovery_done=0,next_page=2 WHERE task_id=?", (task,)
    )
    c.repo.db.commit()
    c.repo.set_task_state(task, "PARTIAL")
    c.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("FTR_")}
    env["FTR_DATA_DIR"] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "ftr.cli", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url, timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("测试服务启动失败")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            expect(page.locator("#policy-list")).to_contain_text("日期未知")
            page.goto(url + f"/#tasks/{task}")
            expect(page.locator("#task-detail")).to_contain_text("官网限流（HTTP 429）")
            expect(page.locator("#task-detail")).to_contain_text("匹配数量未知")
            expect(page.locator("#task-detail")).to_contain_text("等待官网限流解除")
            page.get_by_role("tab", name="事件与失败").click()
            expect(page.locator(".event-row.failure")).to_contain_text("技术详情")
            expect(page.locator(".event-row.failure")).to_contain_text("HTTP 429")
            output = os.environ.get("POLICY_AUDIT_EVIDENCE")
            if output:
                root = Path(output)
                root.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(root / "diagnosis-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#task-detail")).to_contain_text("等待官网限流解除")
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            if output:
                page.screenshot(path=str(root / "diagnosis-mobile.png"), full_page=True)
            assert errors == []
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert (tmp_path / "database.sqlite3").read_bytes() == before


def test_old_pending_decision_digest_remains_valid_after_new_optional_field(tmp_path):
    from ftr.adapters.common import parse_detail
    from ftr.models import DiscoveredRef

    c, task, detail = queued(tmp_path, attachment=False)
    record = parse_detail(
        DiscoveredRef.model_validate_json(c.repo.pending_refs(task, "mof")[0]["ref_json"]),
        detail,
        "raw",
    )
    c.repo.add_document(task, record)
    old = record.model_dump_json(exclude={"date_range_status"})
    c.repo.db.execute("UPDATE documents SET record_json=? WHERE id=?", (old, record.record_id))
    c.repo.db.commit()
    req = DecisionRequest(
        task_id=task,
        record_id=record.record_id,
        input_digest=digest(old.encode()),
        excerpt="正文",
        evidence_id="raw",
    )
    c.repo.add_decision(req)
    try:
        value = SemanticDecision(
            decision_id=req.decision_id,
            input_digest=req.input_digest,
            result="PASS",
            reasons=["旧依据完整"],
            evidence_ids=["raw"],
        )
        assert c.repo.submit_decision(value).quality_state == "validated"
    finally:
        c.close()


def test_suspected_access_reason_is_not_reported_as_confirmed():
    from ftr.diagnostics import failure_explanation

    result = failure_explanation(
        "ACCESS_RESTRICTED",
        {"message": "税务列表请求未出现，可能存在访问限制", "stage": "discover"},
    )
    assert result["certainty"] == "suspected"
    assert "尚不能确定" in result["label"]
    assert "页面故障" in result["next_step"]


def test_failure_resolution_keeps_distinct_query_resources(tmp_path):
    c, task, _ = queued(tmp_path, attachment=False)
    try:
        c._failure(
            task,
            "mof",
            TransientFailure("HTTP 503"),
            "attachment",
            url="https://www.mof.gov.cn/download?id=1",
        )
        c._failure(
            task,
            "mof",
            TransientFailure("HTTP 503"),
            "attachment",
            url="https://www.mof.gov.cn/download?id=2",
        )
        c.repo.resolve_failures(task, "mof", "attachment", "https://www.mof.gov.cn/download?id=1")
        rows = c.repo.db.execute("SELECT details_json FROM failures ORDER BY rowid").fetchall()
        assert [json.loads(r[0])["resolved"] for r in rows] == [True, False]
    finally:
        c.close()


def test_recovered_new_version_drops_legacy_date_limit_keeps_old_record(tmp_path, monkeypatch):
    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def fail(_, url):
        if url.endswith(".xls"):
            raise TransientFailure("HTTP 503")
        return detail, url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", fail)
    c.resume(task)
    row = c.repo.db.execute("SELECT id,record_json FROM documents").fetchone()
    old = json.loads(row["record_json"])
    old["listing_date"] = None
    old["limitations"].append("筛选日期缺失，范围待确认")
    encoded = json.dumps(old, ensure_ascii=False)
    c.repo.db.execute("UPDATE documents SET record_json=? WHERE id=?", (encoded, row["id"]))
    c.repo.db.commit()
    c.close()
    monkeypatch.setattr(
        BoundedClient, "get", lambda _, url: (b"xls", url, "application/octet-stream")
    )
    c = Collector(tmp_path, proxy=None)
    try:
        c.resume(task)
        stored = c.repo.db.execute(
            "SELECT record_json FROM documents WHERE id=?", (row["id"],)
        ).fetchone()[0]
        assert stored == encoded
        latest = json.loads(
            c.repo.db.execute(
                "SELECT record_json FROM documents ORDER BY extraction_version DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert latest["quality_state"] == "collected"
        assert latest["listing_date"] is None
        assert "筛选日期缺失，范围待确认" not in latest["limitations"]
    finally:
        c.close()


def test_other_task_new_version_does_not_replace_resume_evidence(tmp_path, monkeypatch):
    from ftr.models import DocumentRecord, new_id

    c, task, detail = queued(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def fail(_, url):
        if url.endswith(".xls"):
            raise TransientFailure("HTTP 503")
        return detail, url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", fail)
    c.resume(task)
    original = DocumentRecord.model_validate_json(
        c.repo.db.execute("SELECT record_json FROM documents").fetchone()[0]
    )
    other = c.repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    changed = original.model_copy(
        deep=True,
        update={
            "record_id": new_id(),
            "evidence_id": "other-raw",
            "body_text": "另一任务的修订正文",
            "body_sha256": "changed",
        },
    )
    c.repo.add_document(other, changed)
    c.close()
    monkeypatch.setattr(
        BoundedClient, "get", lambda _, url: (b"xls", url, "application/octet-stream")
    )
    c = Collector(tmp_path, proxy=None)
    try:
        result = c.resume(task)
        row = c.repo.db.execute(
            "SELECT record_json FROM documents WHERE task_id=? ORDER BY extraction_version DESC LIMIT 1",
            (task,),
        ).fetchone()
        recovered = json.loads(row[0])
        assert recovered["evidence_id"] == original.evidence_id
        assert recovered["body_text"] == original.body_text
        assert result.data["report"]["completion"]["downloads"] == "COMPLETE"
        assert c.repo.get_document(changed.record_id).body_text == "另一任务的修订正文"
        latest = c.repo.db.execute(
            "SELECT id FROM documents ORDER BY source_version DESC,extraction_version DESC LIMIT 1"
        ).fetchone()[0]
        assert latest == changed.record_id
    finally:
        c.close()


def test_cli_locked_writer_reports_action_and_preserves_database(tmp_path):
    import os
    import subprocess
    import sys

    from ftr.runtime import data_lock

    c, task, _ = queued(tmp_path, attachment=False)
    c.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    env = {k: v for k, v in os.environ.items() if not k.startswith("FTR_")}
    with data_lock(tmp_path):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ftr.cli",
                "--data-dir",
                str(tmp_path),
                "task",
                "resume",
                "--task",
                task,
            ],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    payload = json.loads(result.stdout)
    assert result.returncode == 5
    assert payload["errors"][0]["code"] == "DATA_LOCKED"
    assert "不要删除锁文件" in payload["errors"][0]["next_step"]
    assert "等待" in payload["errors"][0]["next_step"]
    assert (tmp_path / "database.sqlite3").read_bytes() == before
