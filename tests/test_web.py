import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from ftr.cli import parser, run
from ftr.evidence import EvidenceStore
from ftr.models import Attachment, DiscoveredRef, DocumentRecord, DocumentType, TaskRequest, digest
from ftr.repository import Repository
from ftr.runtime import data_lock
from ftr.web.query import ReadQueries

web_app = pytest.importorskip("ftr.web.app", exc_type=ImportError)
create_app, serve = web_app.create_app, web_app.serve
TestClient = pytest.importorskip("fastapi.testclient").TestClient


@pytest.fixture
def web_data(tmp_path):
    repo = Repository(tmp_path)
    evidence_store = EvidenceStore(tmp_path)
    first = repo.create_task(
        TaskRequest(
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            source_ids=["mof"],
        )
    )
    current = repo.create_task(
        TaskRequest(
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            source_ids=["mof"],
        )
    )
    repo.set_task_state(first, "PARTIAL")
    repo.set_task_state(current, "RUNNING")

    def add(title, url, body, quality="collected", day=3, attachments=None):
        evidence = evidence_store.save(body.encode(), url, url, "text/html")
        repo.save_evidence(evidence)
        record = DocumentRecord(
            source_id="mof",
            source_url=url,
            canonical_url=url,
            listing_title=title,
            title=title,
            document_type=DocumentType.POLICY_FILE,
            listing_date=date(2026, 9, day) if day else None,
            listing_date_kind="column_date",
            document_number="财税〔2026〕1号" if title.startswith("政策") else None,
            evidence_id=evidence.evidence_id,
            body_text=body,
            body_sha256=digest(body.encode()),
            quality_state=quality,
            attachments=attachments or [],
        )
        repo.add_document(first, record)
        return record

    old = add("政策旧版", "https://www.mof.gov.cn/policy.html", "旧版增值税正文", "validated", 1)
    attached = evidence_store.save(
        b"office attachment",
        "https://www.mof.gov.cn/form.doc",
        "https://www.mof.gov.cn/form.doc",
        "application/msword",
    )
    repo.save_evidence(attached)
    latest = add(
        "政策新版",
        old.canonical_url,
        "新版增值税正文",
        day=5,
        attachments=[
            Attachment(
                url=attached.source_url,
                label="申请表",
                download_state="saved",
                extraction_state="unsupported",
                content_role="supplement",
                evidence_id=attached.evidence_id,
            )
        ],
    )
    checked = add("已验证说明", "https://www.mof.gov.cn/checked.html", "验证正文", "validated", 2)
    add("格式受限", "https://www.mof.gov.cn/limited.html", "受限正文", "quarantined", 4)
    add("已排除", "https://www.mof.gov.cn/rejected.html", "排除正文", "rejected", None)
    reused_id, created = repo.add_document(current, latest)
    assert not created and reused_id == latest.record_id
    refs = []
    for index, state in enumerate(("SAVED", "FAILED", "PENDING", "OUT_OF_SCOPE", "UNCHANGED_SKIP")):
        url = (
            "http://www.mof.gov.cn/policy.html#body"
            if state == "SAVED"
            else f"https://www.mof.gov.cn/item-{index}.html"
        )
        ref = DiscoveredRef(
            source_id="mof",
            url=url,
            listing_title=f"队列{index}",
            listing_date=date(2026, 9, 3),
            listing_date_kind="column_date",
            discovered_from="list",
        )
        refs.append(ref)
    repo.save_page(current, "mof", refs, 2)
    for ref, state in zip(refs, ("SAVED", "FAILED", "PENDING", "OUT_OF_SCOPE", "UNCHANGED_SKIP")):
        repo.set_ref_state(
            current, "mof", ref.url, state, "来源请求失败" if state == "FAILED" else None
        )
    repo.add_failure(
        current, "mof", "ACCESS_RESTRICTED", {"url": refs[1].url, "error": "来源请求失败"}
    )
    client = TestClient(create_app(tmp_path), base_url="http://127.0.0.1")
    yield {
        "root": tmp_path,
        "repo": repo,
        "client": client,
        "first": first,
        "current": current,
        "old": old,
        "latest": latest,
        "checked": checked,
    }
    client.close()
    repo.close()


def test_default_policies_include_all_quality_states_and_only_latest(web_data):
    client = web_data["client"]
    data = client.get("/api/policies").json()
    assert data["total"] == 4 and data["page_size"] == 20
    assert [item["title"] for item in data["items"]] == [
        "政策新版",
        "格式受限",
        "已验证说明",
        "已排除",
    ]
    assert {item["quality_state"] for item in data["items"]} == {
        "collected",
        "validated",
        "quarantined",
        "rejected",
    }
    assert "body_text" not in data["items"][0]
    assert data["queried_at"]
    overview = client.get("/api/overview").json()
    assert overview["policies_total"] == 4
    assert overview["task_counts"] == {"PARTIAL": 1, "RUNNING": 1}
    assert client.get("/api/sources").json()["items"][0]["policies_count"] == 4
    # Web 全量浏览不能改变研究检索的仅已复核规则。
    assert [d.title for d in web_data["repo"].search("")] == ["已验证说明"]


@pytest.mark.parametrize(
    "params,total",
    [
        ({"q": "增值税"}, 1),
        ({"q": "财税〔2026〕1号"}, 1),
        ({"q": "政策旧版"}, 0),
        ({"q": "%' OR 1=1--"}, 0),
        ({"source_id": "chinatax"}, 0),
        ({"quality_state": "validated"}, 1),
        (
            {
                "source_id": "mof",
                "document_type": "policy_file",
                "quality_state": "collected",
                "date_from": "2026-09-04",
                "date_to": "2026-09-05",
            },
            1,
        ),
    ],
)
def test_search_and_combined_filters(web_data, params, total):
    result = web_data["client"].get("/api/policies", params=params)
    assert result.status_code == 200 and result.json()["total"] == total


def test_pagination_and_versions_are_actual_records(web_data):
    client = web_data["client"]
    data = client.get("/api/policies?page=2&page_size=2").json()
    assert data["total"] == 4 and len(data["items"]) == 2 and data["page"] == 2
    latest, old = web_data["latest"], web_data["old"]
    detail = client.get(f"/api/policies/{latest.record_id}").json()["item"]
    assert detail["body_text"] == "新版增值税正文" and detail["source_version"] == 2
    attachment = detail["attachments"][0]
    assert (
        attachment["download_state"] == "saved" and attachment["extraction_state"] == "unsupported"
    )
    assert (
        client.get(f"/api/evidence/{attachment['evidence_id']}/download").content
        == b"office attachment"
    )
    versions = client.get(f"/api/policies/{latest.record_id}/versions").json()["items"]
    assert [v["record_id"] for v in versions] == [latest.record_id, old.record_id]
    old_detail = client.get(f"/api/policies/{old.record_id}").json()["item"]
    assert old_detail["body_text"] == "旧版增值税正文" and old_detail["source_version"] == 1


def test_queue_counts_and_reused_document_are_not_new_versions(web_data):
    client, task = web_data["client"], web_data["current"]
    summary = client.get(f"/api/tasks/{task}").json()["item"]
    assert summary["state"] == "RUNNING" and summary["last_state_at"]
    assert summary["queue_counts"] == {
        "SAVED": 1,
        "FAILED": 1,
        "PENDING": 1,
        "OUT_OF_SCOPE": 1,
        "UNCHANGED_SKIP": 1,
    }
    source = summary["sources"][0]
    assert source["new_versions_count"] == 0 and source["next_page"] == 2
    assert not source["discovery_done"] and source["discovered_count"] == 5
    queue = client.get(f"/api/tasks/{task}/items?state=SAVED").json()["items"]
    assert queue[0]["record_id"] == web_data["latest"].record_id
    assert not queue[0]["record_created_by_task"]
    failure = client.get(f"/api/tasks/{task}/items?state=FAILED").json()["items"][0]
    assert failure["error"] == "来源请求失败"
    assert client.get(f"/api/tasks/{task}/items?q=队列2").json()["total"] == 1
    assert client.get(f"/api/tasks/{task}/items?source_id=chinatax").json()["total"] == 0
    events = client.get(f"/api/tasks/{task}/events?page_size=1").json()
    assert events["total"] == 2 and len(events["items"]) == 1
    assert {e["kind"] for e in client.get(f"/api/tasks/{task}/events").json()["items"]} == {
        "state",
        "failure",
    }


@pytest.mark.parametrize(
    "url",
    [
        "/api/policies?page=0",
        "/api/policies?page_size=101",
        "/api/policies?quality_state=invalid",
        "/api/policies?date_from=2026-09-30&date_to=2026-09-01",
        "/api/policies?date_from=invalid",
        "/api/tasks?state=invalid",
    ],
)
def test_invalid_filters_are_rejected(web_data, url):
    assert web_data["client"].get(url).status_code == 422


def test_missing_records_and_no_mutating_endpoints(web_data):
    client = web_data["client"]
    for path in (
        "/api/policies/missing",
        "/api/policies/missing/versions",
        "/api/tasks/missing",
        "/api/tasks/missing/items",
        "/api/tasks/missing/events",
        "/api/evidence/missing/download",
    ):
        assert client.get(path).status_code == 404
    assert client.post("/api/tasks").status_code == 405


def test_queries_do_not_write_or_take_the_collector_lock(web_data):
    root, client, repo = web_data["root"], web_data["client"], web_data["repo"]
    before = (root / "database.sqlite3").read_bytes()
    with data_lock(root):
        for path in ("/api/overview", "/api/policies", "/api/tasks"):
            assert client.get(path).status_code == 200
    assert (root / "database.sqlite3").read_bytes() == before
    # 新的请求读取采集进程刚提交的状态；没有缓存旧快照或共享线程连接。
    repo.set_task_state(web_data["current"], "PARTIAL")
    assert client.get(f"/api/tasks/{web_data['current']}").json()["item"]["state"] == "PARTIAL"
    with (
        ReadQueries(root).snapshot() as connection,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        connection.execute("DELETE FROM tasks")


def test_missing_database_is_distinct_from_empty_database(tmp_path):
    root = tmp_path / "not-created"
    client = TestClient(create_app(root), base_url="http://127.0.0.1")
    result = client.get("/api/overview")
    assert result.status_code == 503 and result.json()["error"]["code"] == "DATABASE_MISSING"
    assert not root.exists() and client.get("/").status_code == 200
    repo = Repository(root)
    repo.close()
    assert client.get("/api/overview").json()["policies_total"] == 0


def test_database_corruption_and_lock_are_explicit(web_data):
    root = web_data["root"]
    corrupt = root / "corrupt"
    corrupt.mkdir()
    (corrupt / "database.sqlite3").write_bytes(b"not a sqlite database")
    client = TestClient(create_app(corrupt), base_url="http://127.0.0.1")
    assert client.get("/api/overview").json()["error"]["code"] == "DATABASE_CORRUPT"
    connection = sqlite3.connect(root / "database.sqlite3")
    connection.execute("BEGIN EXCLUSIVE")
    try:
        result = web_data["client"].get("/api/overview")
        assert result.status_code == 503 and result.json()["error"]["code"] == "DATABASE_BUSY"
    finally:
        connection.rollback()
        connection.close()


def test_evidence_download_attachment_missing_and_corrupted(web_data):
    client, latest = web_data["client"], web_data["latest"]
    url = f"/api/evidence/{latest.evidence_id}/download"
    result = client.get(url)
    assert result.content == latest.body_text.encode()
    assert result.headers["content-type"] == "application/octet-stream"
    assert result.headers["content-disposition"].startswith("attachment;")
    assert result.headers["x-content-type-options"] == "nosniff"
    evidence = web_data["repo"].get_evidence(latest.evidence_id)
    path = web_data["root"] / "evidence" / evidence.relative_path
    path.write_bytes(b"tampered")
    assert client.get(url).json()["error"]["code"] == "EVIDENCE_CORRUPT"
    path.unlink()
    assert client.get(url).json()["error"]["code"] == "EVIDENCE_FILE_MISSING"


@pytest.mark.parametrize("symlink", [False, True])
def test_registered_evidence_cannot_escape_directory(web_data, symlink):
    root, repo, latest = web_data["root"], web_data["repo"], web_data["latest"]
    outside = root / "outside.txt"
    outside.write_bytes(latest.body_text.encode())
    evidence = repo.get_evidence(latest.evidence_id).model_dump(mode="json")
    if symlink:
        link = root / "evidence" / "escape"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                pytest.skip("Windows 当前身份无符号链接创建权限，负向路径测试未执行")
            raise
        evidence["relative_path"] = "escape"
    else:
        evidence["relative_path"] = "../outside.txt"
    repo.db.execute(
        "UPDATE evidence SET metadata_json=? WHERE id=?",
        (json.dumps(evidence), latest.evidence_id),
    )
    repo.db.commit()
    result = web_data["client"].get(f"/api/evidence/{latest.evidence_id}/download")
    assert result.status_code == 409 and result.json()["error"]["code"] == "EVIDENCE_PATH_INVALID"


def test_static_assets_and_host_boundary(web_data):
    client = web_data["client"]
    for path in ("/", "/static/app.js", "/static/style.css", "/static/favicon.svg"):
        result = client.get(path)
        assert result.status_code == 200
        assert "script-src 'self'" in result.headers["content-security-policy"]
    assert client.get("/api/overview", headers={"host": "foreign.example"}).status_code == 400


def test_serve_port_errors_do_not_touch_database(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path / "not-created"))
    with pytest.raises(ValueError, match="端口"):
        run(parser().parse_args(["serve", "--port", "0"]))
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        with pytest.raises(RuntimeError, match="端口被占用"):
            serve(Path(tmp_path / "not-created"), occupied.getsockname()[1])
    assert not (tmp_path / "not-created").exists()


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需运行真实浏览器验收")
def test_browser_reading_monitor_refresh_and_recovery(web_data):
    import httpx
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "ftr.cli", "serve", "--port", str(port)],
        env={
            **os.environ,
            "FTR_DATA_DIR": str(web_data["root"]),
            "FTR_WEB__POLL_INTERVAL_MS": "1500",
            "FTR_WEB__REQUEST_TIMEOUT_MS": "2000",
        },
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
            pytest.fail("浏览器测试服务没有启动")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script("""(() => {
                const original = window.setTimeout;
                window.observedTimeouts = [];
                window.setTimeout = (callback, milliseconds, ...args) => {
                    window.observedTimeouts.push(milliseconds);
                    return original(callback, milliseconds, ...args);
                };
            })();""")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator(".policy-card")).to_have_count(4)
            expect(page.locator("#query-time")).to_contain_text("每 1.5 秒刷新")
            assert 2000 in page.evaluate("window.observedTimeouts")
            page.get_by_label("搜索政策").fill("增值税")
            expect(page.locator(".policy-card")).to_have_count(1)
            page.locator(".policy-card").click()
            expect(page.locator(".policy-body")).to_have_text("新版增值税正文")
            page.get_by_role("tab", name="版本 2").click()
            page.get_by_role("button", name="阅读此版本").click()
            expect(page.locator(".policy-body")).to_have_text("旧版增值税正文")
            page.get_by_role("tab", name="版本 2").click()
            page.get_by_role("button", name="阅读此版本").click()
            page.get_by_role("tab", name="附件 1").click()
            expect(page.locator(".attachment")).to_contain_text("格式暂不支持")
            with page.expect_download() as downloaded:
                page.get_by_role("link", name="下载保存原件").click()
            assert Path(downloaded.value.path()).read_bytes() == b"office attachment"
            # 验证不执行资料正文中的 HTML，同时验证轮询不重置阅读位置。
            latest = web_data["latest"]
            record = latest.model_dump(mode="json")
            record["body_text"] = "<script>window.injected=true</script>\n" + "实际原文\n" * 300
            web_data["repo"].db.execute(
                "UPDATE documents SET record_json=? WHERE id=?",
                (json.dumps(record), latest.record_id),
            )
            web_data["repo"].db.commit()
            page.get_by_role("tab", name="正文", exact=True).click()
            page.get_by_role("button", name="刷新", exact=True).click()
            expect(page.locator(".policy-body")).to_contain_text("<script>")
            assert page.evaluate("window.injected") is None
            page.locator("#policy-detail").evaluate("el => el.scrollTop=450")
            page.wait_for_timeout(5500)
            assert page.locator("#policy-detail").evaluate("el => el.scrollTop") == 450
            assert page.get_by_label("搜索政策").input_value() == "增值税"
            # 模拟断连；旧正文保留，恢复后重新取得数据。
            page.route("**/api/**", lambda route: route.abort())
            page.get_by_role("button", name="刷新", exact=True).click()
            expect(page.locator("#error-banner")).to_be_visible()
            expect(page.locator(".policy-body")).to_contain_text("实际原文")
            page.unroute("**/api/**")
            page.get_by_role("button", name="刷新", exact=True).click()
            expect(page.locator("#error-banner")).to_be_hidden()
            page.wait_for_load_state("networkidle")
            requests = []
            page.on("request", lambda request: requests.append(request.url))
            page.evaluate("""() => {
                window.testHidden = true;
                Object.defineProperty(document, 'hidden', {
                    configurable: true, get: () => window.testHidden
                });
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            page.wait_for_timeout(5500)
            assert not any("/api/" in request for request in requests)
            page.evaluate("""() => {
                window.testHidden = false;
                document.dispatchEvent(new Event('visibilitychange'));
            }""")
            expect(page.locator("#error-banner")).to_be_hidden()
            # 政策 → 关联任务 → 失败队列 → 状态事件。
            page.get_by_role("button", name="关联任务").click()
            expect(page.locator("#queue-items .queue-item")).to_have_count(0)
            page.locator(f'[data-task="{web_data["current"]}"]').click()
            page.get_by_label("队列状态", exact=True).select_option("FAILED")
            expect(page.locator(".queue-error")).to_have_text("来源请求失败")
            expect(page.locator("#task-detail")).to_contain_text("本任务新增资料版本 0 个")
            page.get_by_role("tab", name="事件与失败").click()
            expect(page.locator(".event-row.failure")).to_contain_text("来源请求失败")
            web_data["repo"].set_task_state(web_data["current"], "PARTIAL")
            expect(page.locator("#task-detail .task-heading-row")).to_contain_text(
                "部分完成",
                timeout=10000,
            )
            page.get_by_role("button", name="查看采集来源").click()
            expect(page.get_by_role("dialog")).to_be_visible()
            expect(page.locator("#sources-content")).to_contain_text("来源可用性未进行实时检测")
            page.get_by_role("button", name="关闭来源详情").click()
            # 窄屏独立详情及返回列表，不产生横向溢出。
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(url + f"/#policies/{latest.record_id}")
            expect(page.locator(".policy-body")).to_be_visible()
            expect(page.locator("#policy-workspace .list-pane")).to_be_hidden()
            page.get_by_role("button", name="返回政策列表").click()
            expect(page.locator("#policy-workspace .list-pane")).to_be_visible()
            assert page.evaluate("document.body.scrollWidth <= innerWidth")
            assert errors == []
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
