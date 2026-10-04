"""Local management uses isolated stores; never requests official policy sites."""

import json
import os
from contextlib import closing

import pytest

from ftr.config import RuntimeSettings, SchedulerSettings
from ftr.management import Command, effective_plan, enqueue, operations, process_commands
from ftr.repository import Repository


def test_queue_is_readonly_and_apply_idempotent(tmp_path):
    with closing(Repository(tmp_path)):
        pass
    before = (tmp_path / "database.sqlite3").read_bytes()
    command = Command(
        id="a" * 32,
        action="plan.save",
        expected_revision=0,
        payload=SchedulerSettings(times=["10:00"]).model_dump(mode="json"),
    )
    enqueue(tmp_path, command)
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    assert operations(tmp_path)[0]["state"] == "QUEUED"
    assert effective_plan(tmp_path)["saved_revision"] == 1
    assert effective_plan(tmp_path)["applied_revision"] == 0
    process_commands(RuntimeSettings(data_dir=tmp_path))
    assert effective_plan(tmp_path)["revision"] == 1
    enqueue(tmp_path, command)
    process_commands(RuntimeSettings(data_dir=tmp_path))
    assert effective_plan(tmp_path)["revision"] == 1
    assert operations(tmp_path)[0]["state"] == "APPLIED"


def test_stale_plan_conflict(tmp_path):
    with closing(Repository(tmp_path)):
        pass
    for index in range(2):
        enqueue(
            tmp_path,
            Command(
                id=str(index) * 32,
                action="plan.save",
                expected_revision=0,
                payload=SchedulerSettings().model_dump(mode="json"),
            ),
        )
        process_commands(RuntimeSettings(data_dir=tmp_path))
    assert operations(tmp_path)[0]["state"] == "FAILED"
    assert effective_plan(tmp_path)["revision"] == 1


def test_invalid_command_has_no_side_effect(tmp_path):
    with pytest.raises(ValueError):
        Command(id="../bad", action="arbitrary", payload={"url": "https://evil.test"})
    assert list(tmp_path.iterdir()) == []


import time
from datetime import UTC, date, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from ftr.backup import create_backup, restore_backup
from ftr.evidence import EvidenceStore
from ftr.management import review_detail, review_queue
from ftr.models import Attachment, DocumentRecord, DocumentType, TaskRequest
from ftr.runtime import data_lock
from ftr.scheduler import Scheduler, read_states, status, window
from ftr.web.app import create_app
from ftr.web.management import ManagementSession
from ftr.worker import ensure_worker, identity, stop_owned


def make_record(root, *, limited=False, missing=False):
    with closing(Repository(root)) as repo:
        task = repo.create_task(
            TaskRequest(source_ids=["mof"], date_from=date(2026, 1, 1), date_to=date(2026, 12, 31))
        )
        evidence = EvidenceStore(root).save(
            b"controlled original",
            "https://www.mof.gov.cn/a",
            "https://www.mof.gov.cn/a",
            "text/html",
        )
        repo.save_evidence(evidence)
        record = DocumentRecord(
            source_id="mof",
            source_url="https://www.mof.gov.cn/a",
            canonical_url="https://www.mof.gov.cn/a",
            listing_title="测试政策",
            title="测试政策",
            document_type=DocumentType.POLICY_FILE,
            listing_date_kind="column_date",
            evidence_id=evidence.evidence_id,
            body_text="受控政策正文",
            limitations=["内容受限"] if limited else [],
            attachments=[Attachment(url="https://www.mof.gov.cn/a.pdf", label="缺失附件")]
            if missing
            else [],
        )
        rid, _ = repo.add_document(task, record)
    return rid


def execute(root, action, payload, revision=0, **kwargs):
    command = Command(id=uuid4().hex, action=action, payload=payload, expected_revision=revision)
    enqueue(root, command)
    process_commands(RuntimeSettings(data_dir=root), **kwargs)
    return next(x for x in operations(root) if x["id"] == command.id)


def conclusion(root, rid, result="PASS", reasons=None):
    item = review_detail(root, rid)
    return {
        "record_id": rid,
        "input_digest": item["input_digest"],
        "result": result,
        "reasons": reasons or ["已核对原件和正文"],
        "evidence_ids": item["evidence_ids"],
    }


def test_draft_review_reopen_immutable_rounds(tmp_path):
    rid = make_record(tmp_path)
    before = review_detail(tmp_path, rid)
    draft = execute(tmp_path, "review.draft", conclusion(tmp_path, rid))
    assert draft["state"] == "APPLIED"
    assert review_detail(tmp_path, rid)["item"]["quality_state"] == "collected"
    assert execute(tmp_path, "review.submit", conclusion(tmp_path, rid), 1)["state"] == "APPLIED"
    first = review_detail(tmp_path, rid)
    assert first["item"]["quality_state"] == "validated"
    assert len(first["history"]) == 1
    assert first["history"][0]["snapshot_json"]["quality_state"] == "collected"
    assert execute(tmp_path, "review.submit", conclusion(tmp_path, rid), 2)["state"] == "FAILED"
    assert (
        execute(
            tmp_path, "review.reopen", {"record_id": rid, "input_digest": first["input_digest"]}, 2
        )["state"]
        == "APPLIED"
    )
    assert (
        execute(tmp_path, "review.submit", conclusion(tmp_path, rid, "UNCERTAIN"), 3)["state"]
        == "APPLIED"
    )
    now = review_detail(tmp_path, rid)
    assert now["history"][0] == first["history"][0]
    assert now["round"] == 2
    assert review_queue(tmp_path, "further")[0]["record_id"] == rid
    assert before["input_digest"] != now["input_digest"]


@pytest.mark.parametrize("limited,missing", [(True, False), (False, True)])
def test_hard_limits_forbid_pass_without_existing_decision(tmp_path, limited, missing):
    rid = make_record(tmp_path, limited=limited, missing=missing)
    assert not review_detail(tmp_path, rid)["can_pass"]
    assert execute(tmp_path, "review.submit", conclusion(tmp_path, rid))["state"] == "FAILED"
    assert (
        execute(tmp_path, "review.submit", conclusion(tmp_path, rid, "UNCERTAIN"))["state"]
        == "APPLIED"
    )


def test_stale_version_preserves_draft(tmp_path):
    rid = make_record(tmp_path)
    payload = conclusion(tmp_path, rid)
    execute(tmp_path, "review.draft", payload)
    with closing(Repository(tmp_path)) as repo:
        record = repo.get_document(rid).model_copy(
            update={"record_id": uuid4().hex, "body_text": "新版正文"}
        )
        task = repo.db.execute("SELECT task_id FROM documents WHERE id=?", (rid,)).fetchone()[0]
        new, _ = repo.add_document(task, record)
    op = execute(tmp_path, "review.submit", payload, 1)
    assert op["result"]["code"] == "CONFLICT"
    assert review_detail(tmp_path, rid)["draft"]["input_digest"] == payload["input_digest"]
    assert review_queue(tmp_path)[0]["record_id"] == new


def test_tampered_evidence_and_missing_confirmation(tmp_path):
    rid = make_record(tmp_path)
    payload = conclusion(tmp_path, rid)
    payload["evidence_ids"] = []
    assert execute(tmp_path, "review.submit", payload)["state"] == "FAILED"
    for p in (tmp_path / "evidence").rglob("*"):
        if p.is_file():
            p.write_bytes(b"tampered")
    assert execute(tmp_path, "review.submit", conclusion(tmp_path, rid))["state"] == "FAILED"


def test_backup_pending_commands_excludes_sessions(tmp_path):
    rid = make_record(tmp_path)
    execute(tmp_path, "review.draft", conclusion(tmp_path, rid))
    enqueue(
        tmp_path,
        Command(
            id="b" * 32, action="plan.save", payload=SchedulerSettings().model_dump(mode="json")
        ),
    )
    (tmp_path / ".worker.json").write_text('{"token":"secret"}')
    backup = tmp_path.parent / (tmp_path.name + "-backup")
    target = tmp_path.parent / (tmp_path.name + "-restore")
    create_backup(tmp_path, backup)
    restore_backup(backup, target)
    assert not (target / ".worker.json").exists()
    assert operations(target)[0]["state"] == "QUEUED"
    process_commands(RuntimeSettings(data_dir=target))
    assert effective_plan(target)["revision"] == 1
    assert review_detail(target, rid)["draft"]


def test_session_single_use_origin_readonly_and_remote(tmp_path, monkeypatch):
    make_record(tmp_path)
    session = ManagementSession()
    app = create_app(tmp_path, management_session=session)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    command = Command(id="c" * 32, action="plan.disable").model_dump(mode="json")
    origin = {"Origin": "http://127.0.0.1"}
    assert client.post("/api/manage/commands", json=command, headers=origin).status_code == 403
    token = session.issue()
    assert client.post("/api/manage/session", json={"token": token}).status_code == 403
    response = client.post("/api/manage/session", json={"token": token}, headers=origin)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert (
        client.post("/api/manage/session", json={"token": token}, headers=origin).status_code == 403
    )
    assert (
        client.post(
            "/api/manage/commands", json=command, headers={"Origin": "http://evil.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/manage/commands", json=command, headers={**origin, "Sec-Fetch-Site": "cross-site"}
        ).status_code
        == 403
    )
    monkeypatch.setattr("ftr.web.management.ensure_worker", lambda *_: {"state": "RUNNING"})
    before = (tmp_path / "database.sqlite3").read_bytes()
    assert client.post("/api/manage/commands", json=command, headers=origin).status_code == 202
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    other = TestClient(
        create_app(tmp_path, management_session=ManagementSession()),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    other.cookies.update(client.cookies)
    assert other.post("/api/manage/commands", json=command, headers=origin).status_code == 403
    remote = TestClient(
        create_app(
            tmp_path,
            settings=__import__("ftr.config", fromlist=["WebSettings"]).WebSettings(host="0.0.0.0"),
            management_session=session,
        ),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
    )
    assert not remote.get("/api/manage/session").json()["management_enabled"]


def wait_for(predicate, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("后台状态未在期限内完成")


def test_detached_worker_duplicate_identity_lock_and_restart(tmp_path):
    make_record(tmp_path)
    settings = RuntimeSettings(data_dir=tmp_path)
    try:
        first = ensure_worker(settings)
        second = ensure_worker(settings)
        assert first["instance_id"] == second["instance_id"]
        with data_lock(tmp_path):
            enqueue(tmp_path, Command(id="d" * 32, action="plan.disable"))
            time.sleep(0.6)
            assert operations(tmp_path)[0]["state"] == "QUEUED"
        wait_for(lambda: operations(tmp_path)[0]["state"] == "APPLIED")
        assert effective_plan(tmp_path)["settings"]["enabled"] is False
        stop_owned(tmp_path)
        wait_for(lambda: identity(tmp_path) is None)
        restarted = ensure_worker(settings)
        assert restarted["instance_id"] != first["instance_id"]
        assert effective_plan(tmp_path)["revision"] == 1
    finally:
        stop_owned(tmp_path)
        wait_for(lambda: identity(tmp_path) is None)


def test_independent_scheduler_refuses_takeover(tmp_path):
    import portalocker

    make_record(tmp_path)
    with (
        portalocker.Lock(str(tmp_path / ".scheduler.lock"), timeout=0),
        pytest.raises(ValueError, match="独立调度器"),
    ):
        ensure_worker(RuntimeSettings(data_dir=tmp_path))
    assert not (tmp_path / ".worker.json").exists()


def test_atomic_rollback_on_crash_and_replay(tmp_path, monkeypatch):
    from ftr import management

    make_record(tmp_path)
    command = Command(
        id="e" * 32, action="plan.save", payload=SchedulerSettings().model_dump(mode="json")
    )
    enqueue(tmp_path, command)
    original = management.apply_command

    def crash(*args):
        original(*args)
        raise KeyboardInterrupt("simulated hard exit before completion")

    monkeypatch.setattr(management, "apply_command", crash)
    with pytest.raises(KeyboardInterrupt):
        process_commands(RuntimeSettings(data_dir=tmp_path))
    assert effective_plan(tmp_path)["revision"] == 0
    monkeypatch.setattr(management, "apply_command", original)
    process_commands(RuntimeSettings(data_dir=tmp_path))
    assert effective_plan(tmp_path)["revision"] == 1


def test_one_off_disabled_and_source_pause(tmp_path):
    from test_scheduler import ControlledCollector

    make_record(tmp_path)
    config = RuntimeSettings(data_dir=tmp_path)
    due = window(datetime.now(UTC), config.scheduler)
    execute(tmp_path, "plan.now", {**due, "sources": ["mof"]})
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    execute(tmp_path, "source.pause", {"source_id": "mof"})
    assert scheduler.tick()["state"] == "IDLE"
    execute(tmp_path, "source.resume", {"source_id": "mof"}, 1)
    assert scheduler.tick()["source_id"] == "mof"
    assert scheduler.tick()["state"] == "DISABLED"
    with closing(Repository(tmp_path, readonly=True)) as repo:
        state = read_states(repo.db)["mof"]
        assert not state["active_task_id"] and not state["explicit_run"]


def test_recovery_atomic_version_and_no_arbitrary_url(tmp_path):
    rid = make_record(tmp_path, missing=True)
    payload = {"record_id": rid, "input_digest": review_detail(tmp_path, rid)["input_digest"]}
    with pytest.raises(ValueError):
        Command(
            id=uuid4().hex, action="review.recover", payload={**payload, "url": "https://evil.test"}
        )

    def controlled_recover(repo, record_id):
        record = repo.get_document(record_id).model_copy(
            deep=True, update={"record_id": uuid4().hex}
        )
        evidence = EvidenceStore(tmp_path).save(
            b"attachment",
            "https://www.mof.gov.cn/a.pdf",
            "https://www.mof.gov.cn/a.pdf",
            "application/pdf",
        )
        repo.save_evidence(evidence)
        record.attachments[0].download_state = "saved"
        record.attachments[0].evidence_id = evidence.evidence_id
        task = repo.db.execute("SELECT task_id FROM documents WHERE id=?", (rid,)).fetchone()[0]
        new, created = repo.add_document(task, record, base_record_id=rid)
        return {"record_id": new, "created": created, "needs_review": True}

    result = execute(tmp_path, "review.recover", payload, recover=controlled_recover)
    assert result["state"] == "APPLIED"
    assert result["result"]["created"]
    assert not review_detail(tmp_path, rid)["latest"]
    assert review_detail(tmp_path, result["result"]["record_id"])["category"] == "pending"


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需 Chromium 验收")
def test_browser_management_review_draft_plan_narrow_and_detached(tmp_path, monkeypatch):
    import socket

    import httpx
    from playwright.sync_api import expect, sync_playwright

    from ftr.config import WebSettings
    from ftr.workbench import _state, manage

    rid = make_record(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = RuntimeSettings(
        data_dir=tmp_path,
        web=WebSettings(port=port, poll_interval_ms=300),
        update_check={"enabled": False},
    )
    result = manage(
        tmp_path, "start", management=True, runtime_settings=settings, web_settings=settings.web
    )
    url = result["url"]
    private = _state(tmp_path)
    try:
        with httpx.Client(trust_env=False) as client:
            token = client.get(
                url + "/_control/session", headers={"X-FTR-Token": private["token"]}
            ).json()["token"]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "#manage-token=" + token)
            expect(page.locator("#management-mode")).to_contain_text("本机管理")
            expect(page.locator("#schedule-form")).to_contain_text("执行时间")
            assert "token" not in page.url
            page.locator('[name="times"]').fill("10:15, 19:00")
            page.get_by_role("button", name="预览计划", exact=True).click()
            expect(page.locator("#schedule-preview")).to_contain_text("Asia/Shanghai")
            page.get_by_role("button", name="确认预览并保存").click()
            expect(page.locator("#operation-list")).to_contain_text("已生效")
            expect(page.locator("#plan-state")).to_contain_text("保存版本 1")
            assert effective_plan(tmp_path)["settings"]["times"] == ["10:15", "19:00"]
            assert not effective_plan(tmp_path)["settings"]["enabled"]
            page.get_by_role("link", name="资料复核", exact=True).click()
            expect(page.locator("#review-detail")).to_contain_text("受控政策正文")
            page.locator('[name="reasons"]').fill("草稿刷新恢复")
            page.get_by_role("link", name="定时任务", exact=True).click()
            page.get_by_role("link", name="资料复核", exact=True).click()
            expect(page.locator('[name="reasons"]')).to_have_value("草稿刷新恢复")
            page.get_by_role("button", name="保存草稿").click()
            wait_for(lambda: review_detail(tmp_path, rid)["draft"] is not None)
            page.reload()
            expect(page.locator('[name="reasons"]')).to_have_value("草稿刷新恢复")
            page.locator('[name="confirmed"]').check()
            page.get_by_role("button", name="提交人工结论").click()
            wait_for(lambda: review_detail(tmp_path, rid)["submitted"])
            expect(page.locator("#review-detail")).to_contain_text("已复核")
            page.get_by_role("button", name="开启新一轮复核").click()
            wait_for(lambda: review_detail(tmp_path, rid)["round"] == 2)
            expect(page.locator("#review-detail")).to_contain_text("第 2 轮")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.locator('[data-view="reviews"] svg').is_visible()
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            page.locator('[name="reasons"]').fill("窄屏进一步核查")
            page.locator('[name="result"]').select_option("UNCERTAIN")
            page.locator('[name="confirmed"]').check()
            page.get_by_role("button", name="提交人工结论").click()
            wait_for(lambda: len(review_detail(tmp_path, rid)["history"]) == 2)
            page.locator("#review-category").select_option("further")
            expect(page.locator("#review-list")).to_contain_text("测试政策")
            response = page.evaluate(
                """async(rid)=>{const d=await (await fetch('/api/reviews/'+rid)).json();const r=await fetch('/api/manage/commands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:'f'.repeat(32),action:'review.submit',payload:{record_id:rid,input_digest:d.input_digest,result:'REJECT',reasons:['重复提交已处理轮次'],evidence_ids:d.evidence_ids},expected_revision:d.revision})});return r.status;}""",
                rid,
            )
            assert response == 202
            expect(page.locator("#operation-list")).to_contain_text("失败")
            assert not errors
            browser.close()
        # Closing browser and workbench leaves the authenticated writer available.
        manage(tmp_path, "stop")
        assert identity(tmp_path)
        enqueue(tmp_path, Command(id="9" * 32, action="plan.disable", expected_revision=1))
        wait_for(lambda: effective_plan(tmp_path)["revision"] == 2)
    finally:
        manage(tmp_path, "stop")
        stop_owned(tmp_path)
        wait_for(lambda: identity(tmp_path) is None)


@pytest.mark.parametrize("body_missing", [False, True])
def test_real_collector_targeted_recovery_atomic(tmp_path, monkeypatch, body_missing):
    import threading
    from importlib.resources import files

    from ftr.models import DiscoveredRef
    from ftr.network import BoundedClient
    from ftr.worker import recover_record

    rid = make_record(tmp_path, missing=not body_missing)
    if body_missing:
        with closing(Repository(tmp_path)) as repo:
            row = repo.db.execute("SELECT * FROM documents WHERE id=?", (rid,)).fetchone()
            record = repo.get_document(rid)
            record.body_text = ""
            record.limitations = ["正文区域未可靠定位"]
            repo.db.execute(
                "UPDATE documents SET record_json=? WHERE id=?", (record.model_dump_json(), rid)
            )
            repo.save_page(
                row["task_id"],
                "mof",
                [
                    DiscoveredRef(
                        source_id="mof",
                        url=record.source_url,
                        listing_title=record.title,
                        listing_date=date(2026, 6, 25),
                        listing_date_kind="column_date",
                        discovered_from="https://www.mof.gov.cn/list",
                    )
                ],
                None,
            )
            repo.db.commit()
    calls = []
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )

    def get(self, url):
        calls.append(url)
        if body_missing:
            return sample["detail"].encode(), url, "text/html"
        # Non-PDF registered original needs no unsupported parser to become saved.
        return b"controlled attachment", url, "application/octet-stream"

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    if not body_missing:
        with closing(Repository(tmp_path)) as repo:
            record = repo.get_document(rid)
            record.attachments[0].url = "https://www.mof.gov.cn/a.docx"
            repo.db.execute(
                "UPDATE documents SET record_json=? WHERE id=?", (record.model_dump_json(), rid)
            )
            repo.db.commit()
    settings = RuntimeSettings(
        data_dir=tmp_path, network={"proxy_mode": "direct", "interval_seconds": 0}
    )
    result = execute(
        tmp_path,
        "review.recover",
        {"record_id": rid, "input_digest": review_detail(tmp_path, rid)["input_digest"]},
        recover=lambda repo, record_id: recover_record(
            repo, settings, record_id, threading.Event()
        ),
    )
    assert result["state"] == "APPLIED", result
    assert len(calls) == 1
    assert not any("list" in url for url in calls)
    new = review_detail(tmp_path, result["result"]["record_id"])
    assert new["latest"]
    assert new["item"]["quality_state"] != "validated"
    if body_missing:
        assert new["item"]["body_text"]
    else:
        assert new["item"]["attachments"][0]["download_state"] == "saved"
    assert not review_detail(tmp_path, rid)["latest"]
    # Entire record + operation completion share one transaction, including legacy commits.
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_plan_edit_preserves_frozen_request_backlog_and_disable(tmp_path):
    from test_scheduler import ControlledCollector

    from ftr.scheduler import save_state

    ControlledCollector.calls = []
    ControlledCollector.outcomes = {"mof": "budget", "chinatax": "complete"}
    settings = RuntimeSettings(data_dir=tmp_path, scheduler=SchedulerSettings(enabled=True))
    scheduler = Scheduler(settings, collector_factory=ControlledCollector)
    scheduler.tick(datetime(2026, 6, 1, 1, tzinfo=UTC))
    with closing(Repository(tmp_path)) as repo:
        state = read_states(repo.db)["mof"]
        frozen = repo.task(state["active_task_id"])["request_json"]
        state["pending_window"] = {
            "date_from": "2026-05-03",
            "date_to": "2026-06-05",
            "slot": datetime(2026, 6, 5, 1, tzinfo=UTC).isoformat(),
        }
        save_state(repo.db, "mof", state)
    execute(
        tmp_path,
        "plan.save",
        SchedulerSettings(enabled=False, times=["12:00"], lookback_days=7).model_dump(mode="json"),
    )
    assert scheduler.tick()["state"] == "IDLE"
    with closing(Repository(tmp_path, readonly=True)) as repo:
        state = read_states(repo.db)["mof"]
        assert state["pending_window"]["date_from"] == "2026-05-03"
        assert repo.task(state["active_task_id"])["request_json"] == frozen
    assert effective_plan(tmp_path)["settings"]["lookback_days"] == 7
    ControlledCollector.outcomes = {}


def test_source_failure_pause_persists_and_manual_cancel_not_cleared(tmp_path):
    from ftr.scheduler import initial_state, save_state

    rid = make_record(tmp_path)
    with closing(Repository(tmp_path)) as repo:
        task = repo.db.execute("SELECT task_id FROM documents WHERE id=?", (rid,)).fetchone()[0]
        repo.db.execute("UPDATE tasks SET cancel_requested=1 WHERE id=?", (task,))
        state = initial_state() | {"active_task_id": task, "paused_reason": "ACCESS_RESTRICTED"}
        save_state(repo.db, "mof", state)
    result = execute(tmp_path, "source.resume", {"source_id": "mof"})
    assert result["result"]["code"] == "CONFLICT"
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert read_states(repo.db)["mof"]["paused_reason"] == "ACCESS_RESTRICTED"
        assert repo.task(task)["cancel_requested"] == 1


def test_owned_collector_yields_for_disable_and_hard_exit_replay(tmp_path):
    import secrets
    import subprocess
    import sys

    from ftr.worker import public_status

    make_record(tmp_path)
    settings = RuntimeSettings(
        data_dir=tmp_path,
        network={"proxy_mode": "direct", "interval_seconds": 0},
        collection={"max_duration_seconds": 30},
    )
    enqueue(
        tmp_path,
        Command(
            id="8" * 32,
            action="plan.now",
            payload={**window(datetime.now(UTC), settings.scheduler), "sources": ["mof"]},
        ),
    )
    code = """
import json,sys,time
from pathlib import Path
from importlib.resources import files
from ftr.config import RuntimeSettings
from ftr.network import BoundedClient
import ftr.runtime
from ftr.worker import run
settings=RuntimeSettings.model_validate_json(sys.argv[1])
root=settings.data_dir
sample=next(x for x in json.loads((files('ftr')/'data/rule-fixtures.json').read_text()) if x['source_id']=='mof')
def controlled(self,url):
    (root/'request-started').write_text('started')
    while True:
        self.checkpoint()
        time.sleep(.02)
BoundedClient.get=controlled
ftr.runtime.check_url=lambda *_:None
run(settings,sys.argv[2],sys.argv[3])
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            settings.model_dump_json(),
            secrets.token_hex(32),
            secrets.token_hex(16),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: (tmp_path / "request-started").exists())
        enqueue(tmp_path, Command(id="7" * 32, action="plan.disable"))
        wait_for(
            lambda: any(
                x["id"] == "7" * 32 and x["state"] == "APPLIED" for x in operations(tmp_path)
            )
        )
        with closing(Repository(tmp_path, readonly=True)) as repo:
            active = read_states(repo.db)["mof"]["active_task_id"]
            assert active
            assert repo.task(active)["state"] == "PARTIAL"
            audit = [
                json.loads(row[0])
                for row in repo.db.execute(
                    "SELECT details_json FROM audit WHERE task_id=? AND event='batch_stopped'",
                    (active,),
                )
            ]
            assert "INTERRUPTED" in audit[-1]["reasons"]
        # Kill the handle owned by this test while a durable command waits for another writer.
        with data_lock(tmp_path):
            enqueue(tmp_path, Command(id="6" * 32, action="plan.disable", expected_revision=1))
            wait_for(
                lambda: public_status(tmp_path).get("activity", {}).get("state") == "WAITING_LOCK"
            )
            process.kill()
            process.wait(timeout=5)
            assert any(x["id"] == "6" * 32 and x["state"] == "QUEUED" for x in operations(tmp_path))
        ensure_worker(settings)
        wait_for(lambda: effective_plan(tmp_path)["revision"] == 2)
        with closing(Repository(tmp_path, readonly=True)) as repo:
            assert read_states(repo.db)["mof"]["active_task_id"] == active
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        stop_owned(tmp_path)
        wait_for(lambda: identity(tmp_path) is None)


def test_early_http_conflict_invalid_session_and_remote_peer(tmp_path, monkeypatch):
    make_record(tmp_path)
    session = ManagementSession()
    app = create_app(tmp_path, management_session=session)
    local = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000))
    origin = {"Origin": "http://127.0.0.1"}
    assert local.post("/api/manage/session", json={"token": []}, headers=origin).status_code == 422
    assert local.post("/api/manage/session", content="invalid", headers=origin).status_code == 422
    token = session.issue()
    assert (
        local.post("/api/manage/session", json={"token": token}, headers=origin).status_code == 200
    )

    def forbidden(*_):
        raise AssertionError("stale input must not start a worker")

    monkeypatch.setattr("ftr.web.management.ensure_worker", forbidden)
    before = (tmp_path / "database.sqlite3").read_bytes()
    assert (
        local.post(
            "/api/manage/commands",
            json=Command(id="5" * 32, action="plan.disable", expected_revision=99).model_dump(
                mode="json"
            ),
            headers=origin,
        ).status_code
        == 409
    )
    assert not (tmp_path / "commands").exists()
    remote = TestClient(app, base_url="http://127.0.0.1", client=("192.168.1.9", 50000))
    remote.cookies.update(local.cookies)
    assert (
        remote.post(
            "/api/manage/commands",
            json=Command(id="4" * 32, action="plan.disable").model_dump(mode="json"),
            headers=origin,
        ).status_code
        == 403
    )
    assert (tmp_path / "database.sqlite3").read_bytes() == before


def test_old_v3_readonly_review_before_incremental_migration(tmp_path):
    import sqlite3

    rid = make_record(tmp_path)
    with sqlite3.connect(tmp_path / "database.sqlite3") as db:
        for table in ("managed_plan", "management_operations", "review_rounds", "review_workspace"):
            db.execute("DROP TABLE " + table)
        db.execute("PRAGMA user_version=3")
    before = (tmp_path / "database.sqlite3").read_bytes()
    assert review_detail(tmp_path, rid)["revision"] == 0
    assert effective_plan(tmp_path)["config_source"] == "yaml_default"
    assert review_queue(tmp_path)[0]["record_id"] == rid
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    execute(tmp_path, "review.draft", conclusion(tmp_path, rid))
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert review_detail(tmp_path, rid)["draft"]


def test_real_recovery_budget_partial_checkpoint_and_resume(tmp_path, monkeypatch):
    import threading

    from ftr.network import BoundedClient
    from ftr.worker import recover_record

    rid = make_record(tmp_path, missing=True)
    with closing(Repository(tmp_path)) as repo:
        record = repo.get_document(rid)
        record.attachments = [
            Attachment(url=f"https://www.mof.gov.cn/{i}.docx", label=str(i)) for i in range(2)
        ]
        repo.db.execute(
            "UPDATE documents SET record_json=? WHERE id=?", (record.model_dump_json(), rid)
        )
        repo.db.commit()
    calls = []

    def get(self, url):
        calls.append(url)
        return b"0123456789", url, "application/octet-stream"

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    settings = RuntimeSettings(
        data_dir=tmp_path,
        network={"proxy_mode": "direct", "interval_seconds": 0},
        collection={"max_bytes_per_source": 15},
        scheduler={"batch_interval_seconds": 0.01},
    )
    command = Command(
        id="3" * 32,
        action="review.recover",
        payload={"record_id": rid, "input_digest": review_detail(tmp_path, rid)["input_digest"]},
    )
    enqueue(tmp_path, command)
    recover = lambda repo, record_id: recover_record(repo, settings, record_id, threading.Event())
    process_commands(settings, recover=recover)
    op = next(x for x in operations(tmp_path) if x["id"] == command.id)
    assert op["state"] == "RUNNING"
    new = op["result"]["record_id"]
    assert new != rid
    assert review_detail(tmp_path, new)["item"]["attachments"][0]["download_state"] == "saved"
    time.sleep(0.02)
    process_commands(settings, recover=recover)
    op = next(x for x in operations(tmp_path) if x["id"] == command.id)
    assert op["state"] == "APPLIED"
    latest = review_detail(tmp_path, op["result"]["record_id"])
    assert all(a["download_state"] == "saved" for a in latest["item"]["attachments"])
    assert latest["item"]["quality_state"] != "validated"
    assert calls.count("https://www.mof.gov.cn/0.docx") == 1
    before = len(calls)
    process_commands(settings, recover=recover)
    assert len(calls) == before


def test_pause_visible_before_any_scheduled_run(tmp_path):
    from ftr.scheduler import status

    make_record(tmp_path)
    execute(tmp_path, "source.pause", {"source_id": "mof"})
    report = status(tmp_path)
    assert report["sources"][0]["source_id"] == "mof"
    assert report["sources"][0]["user_paused"]
    assert report["sources"][0]["management_revision"] == 1
    execute(tmp_path, "source.resume", {"source_id": "mof"}, 1)
    assert not status(tmp_path)["sources"][0]["user_paused"]


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需 Chromium 受控来源验收")
def test_browser_enable_pause_run_now_and_recover_controlled_sources(tmp_path, monkeypatch):
    import socket
    import threading

    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from test_scheduler import ControlledCollector

    from ftr.config import WebSettings
    from ftr.network import BoundedClient
    from ftr.worker import recover_record

    rid = make_record(tmp_path, missing=True)
    with closing(Repository(tmp_path)) as repo:
        record = repo.get_document(rid)
        record.attachments[0].url = "https://www.mof.gov.cn/missing.docx"
        repo.db.execute(
            "UPDATE documents SET record_json=? WHERE id=?", (record.model_dump_json(), rid)
        )
        repo.db.commit()
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)
    monkeypatch.setattr(
        BoundedClient,
        "get",
        lambda self, url: (b"controlled office original", url, "application/octet-stream"),
    )
    monkeypatch.setattr("ftr.web.management.ensure_worker", lambda *_: {"state": "RUNNING"})
    monkeypatch.setattr(
        "ftr.web.management.public_status", lambda *_: {"state": "RUNNING", "activity": {}}
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = RuntimeSettings(
        data_dir=tmp_path,
        network={"proxy_mode": "direct", "interval_seconds": 0},
        web=WebSettings(port=port, poll_interval_ms=200),
        scheduler={"batch_interval_seconds": 0.05},
    )
    session = ManagementSession()
    token = session.issue()
    app = create_app(
        tmp_path,
        settings.web,
        settings.scheduler,
        management_session=session,
        runtime_settings=settings,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
    )
    web_thread = threading.Thread(target=server.run, daemon=True)
    web_thread.start()
    stop = threading.Event()
    ControlledCollector.calls = []
    ControlledCollector.outcomes = {"mof": "budget"}
    failures = []

    def controlled_writer():
        scheduler = Scheduler(settings, collector_factory=ControlledCollector)
        while not stop.is_set():
            try:
                process_commands(
                    settings,
                    recover=lambda repo, record_id: recover_record(repo, settings, record_id, stop),
                )
                scheduler.tick()
            except Exception as exc:  # noqa: BLE001 - surface daemon failures to the test
                failures.append(str(exc))
                stop.set()
            stop.wait(0.02)

    writer = threading.Thread(target=controlled_writer, daemon=True)
    writer.start()
    url = f"http://127.0.0.1:{port}"
    try:
        wait_for(lambda: server.started)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url + "#manage-token=" + token)
            expect(page.locator("#management-mode")).to_contain_text("本机管理")
            page.get_by_role("button", name="启用计划", exact=True).click()
            wait_for(lambda: effective_plan(tmp_path)["settings"]["enabled"])
            wait_for(lambda: len(ControlledCollector.calls) >= 2)
            with closing(Repository(tmp_path, readonly=True)) as repo:
                frozen = read_states(repo.db)["mof"]["prepared_request"]
            page.locator('[data-managed-source="mof"][data-source-action="pause"]').click()
            wait_for(
                lambda: next(x for x in status(tmp_path)["sources"] if x["source_id"] == "mof").get(
                    "user_paused"
                )
            )
            page.locator('[data-managed-source="mof"][data-source-action="resume"]').click()
            wait_for(
                lambda: (
                    not next(x for x in status(tmp_path)["sources"] if x["source_id"] == "mof").get(
                        "user_paused"
                    )
                )
            )
            page.get_by_role("button", name="停用计划", exact=True).click()
            wait_for(lambda: not effective_plan(tmp_path)["settings"]["enabled"])
            ControlledCollector.outcomes = {}
            page.locator('[name="now-source"][value="chinatax"]').uncheck()
            page.get_by_role("button", name="预览本轮窗口", exact=True).click()
            expect(page.locator("#run-window")).to_contain_text("mof")
            page.get_by_role("button", name="确认并获取本轮", exact=True).click()
            wait_for(
                lambda: any(
                    x["action"] == "plan.now" and x["state"] == "APPLIED"
                    for x in operations(tmp_path)
                )
            )
            wait_for(lambda: any(x == "mof" for x in ControlledCollector.calls))
            assert not effective_plan(tmp_path)["settings"]["enabled"]
            with closing(Repository(tmp_path, readonly=True)) as repo:
                row = repo.db.execute(
                    "SELECT request_json FROM tasks WHERE idempotency_key=?",
                    (frozen["idempotency_key"],),
                ).fetchone()
                assert json.loads(row[0]) == frozen
            page.get_by_role("link", name="资料复核", exact=True).click()
            page.locator("#review-category").select_option("limited")
            expect(page.locator("#review-detail")).to_contain_text("缺失附件")
            page.get_by_role("button", name="补取已登记缺失内容").click()
            wait_for(lambda: not review_detail(tmp_path, rid)["latest"])
            expect(page.locator("#operation-list")).to_contain_text("补取完成")
            page.get_by_role("link", name="查看补取版本与复核").first.click()
            expect(page.locator("#review-detail")).to_contain_text("下载 已保存")
            assert not errors
            browser.close()
        assert not failures
    finally:
        stop.set()
        writer.join(timeout=5)
        server.should_exit = True
        web_thread.join(timeout=5)
        ControlledCollector.outcomes = {}


def test_managed_restart_recovers_pending_commands_without_extra_click(tmp_path):
    import socket

    from ftr.config import WebSettings
    from ftr.workbench import manage

    make_record(tmp_path)
    enqueue(tmp_path, Command(id="2" * 32, action="plan.disable"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = RuntimeSettings(
        data_dir=tmp_path, web=WebSettings(port=port), update_check={"enabled": False}
    )
    try:
        ordinary = manage(tmp_path, "start", runtime_settings=settings, web_settings=settings.web)
        assert ordinary["state"] == "RUNNING" and identity(tmp_path) is None
        manage(tmp_path, "stop")
        managed = manage(
            tmp_path, "start", management=True, runtime_settings=settings, web_settings=settings.web
        )
        assert managed["background"]["state"] == "RUNNING"
        wait_for(lambda: effective_plan(tmp_path)["revision"] == 1)
        manage(tmp_path, "stop")
        assert identity(tmp_path)
    finally:
        manage(tmp_path, "stop")
        stop_owned(tmp_path)
        wait_for(lambda: identity(tmp_path) is None)


def test_worker_tampered_port_never_contacts_remote(tmp_path, monkeypatch):
    make_record(tmp_path)
    (tmp_path / ".worker.json").write_text(
        json.dumps(
            {"port": "80@evil.invalid", "directory": str(tmp_path.resolve()), "token": "private"}
        )
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid state must not make a request")

    monkeypatch.setattr("ftr.worker.httpx.Client", forbidden)
    assert identity(tmp_path) is None


def test_loopback_worker_binding_never_resolves_dns(monkeypatch):
    import socket
    from http.server import BaseHTTPRequestHandler

    from ftr.worker import LoopbackControlServer

    def forbidden(*_args, **_kwargs):
        raise AssertionError("loopback startup must not resolve DNS")

    monkeypatch.setattr(socket, "getfqdn", forbidden)
    monkeypatch.setattr(socket, "gethostbyaddr", forbidden)
    with LoopbackControlServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as server:
        assert server.server_name == "127.0.0.1"
        assert server.server_port > 0
