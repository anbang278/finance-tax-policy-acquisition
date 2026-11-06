"""Multi-source integration with fictional HTML and intercepted HTTP only."""

import json
import os
import socket
import threading
import time
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from ftr.config import RuntimeSettings, SourceConfig, load_sources
from ftr.models import Attachment, DocumentRecord, DocumentType, TaskRequest
from ftr.network import BoundedClient
from ftr.repository import Repository
from ftr.runtime import Collector
from ftr.web.app import create_app


@pytest.fixture
def source_world(monkeypatch):
    from ftr import config, runtime, sources
    from ftr.adapters import public

    registry = load_sources().model_copy(deep=True)
    profile = {
        "listing": {"container": "//ul[@id='items']", "items": "./li", "date": ".//time"},
        "pagination": {"mode": "none"},
        "detail": {"title": "//h1", "body": "//main", "published_date": "//time[@id='published']"},
    }
    pages = {}
    for key, region in (("public_a", "甲省"), ("public_b", "乙省")):
        registry.sources[key] = SourceConfig(
            name=key,
            authority_id=key,
            publisher="虚构发布机构",
            region=region,
            categories=["tax"],
            channel="policy",
            adapter="public_html",
            entry=f"https://official.example.test/{key}/",
            allowed_hosts=["official.example.test"],
            listing_date_kind="published_date",
            profile=profile,
        )
        pages[f"https://official.example.test/{key}/"] = (
            f"<ul id='items'><li><a href='one.html'>{key}通知</a><time>2026-11-01</time></li></ul>"
        ).encode()
        pages[f"https://official.example.test/{key}/one.html"] = (
            f"<html><h1>{key}通知</h1><time id='published'>2026-11-01</time><main>第一条 虚构测试正文。</main></html>"
        ).encode()
    for module in (config, runtime, sources):
        monkeypatch.setattr(module, "load_sources", lambda: registry)

    def trusted(url, hosts, *_):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname in hosts
        return parsed.hostname

    for module in (runtime, public):
        monkeypatch.setattr(module, "check_url", trusted)
    monkeypatch.setattr("ftr.network.check_url", trusted)
    calls = []
    clients = []
    original = BoundedClient.__init__

    def initialize(client, *args, **kwargs):
        original(client, *args, **kwargs)
        client._client.close()
        clients.append(client)

        def handle(request):
            calls.append(str(request.url))
            assert str(request.url) in pages, f"Unexpected fictional URL: {request.url}"
            return httpx.Response(
                200,
                content=pages[str(request.url)],
                headers={"content-type": "text/html; charset=utf-8"},
            )

        client._client = httpx.Client(transport=httpx.MockTransport(handle))

    monkeypatch.setattr(BoundedClient, "__init__", initialize)
    return registry, pages, calls, clients


def collect(root, ids, **collection):
    settings = RuntimeSettings(
        data_dir=root,
        network={"proxy_mode": "direct", "interval_seconds": 0},
        collection=collection,
    )
    return Collector(root, settings)


def request(ids, **values):
    return TaskRequest(
        source_ids=ids, date_from=date(2026, 11, 1), date_to=date(2026, 11, 6), **values
    )


def test_selected_bad_public_profile_does_not_block_next_source(tmp_path, source_world):
    registry, _pages, calls, _clients = source_world
    registry.sources["public_a"].profile = {"execute": "forbidden"}
    collector = collect(tmp_path, ["public_a", "public_b"])
    try:
        result = collector.collect(request(["public_a", "public_b"]))
        assert (
            collector.repo.db.execute(
                "SELECT COUNT(*) FROM documents WHERE source_id='public_b'"
            ).fetchone()[0]
            == 1
        )
        assert (
            collector.repo.db.execute(
                "SELECT COUNT(*) FROM failures WHERE source_id='public_a'"
            ).fetchone()[0]
            == 1
        )
        assert "SOURCE_FAILURE" in result.data["stop_reasons"]
        assert not any("/public_a/" in url for url in calls)
    finally:
        collector.close()


def test_malformed_selected_legacy_rules_do_not_block_public_source(tmp_path, source_world):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "mof.json").write_text("not-json")
    collector = collect(tmp_path, ["mof", "public_b"])
    try:
        collector.collect(request(["mof", "public_b"]))
        assert (
            collector.repo.db.execute(
                "SELECT COUNT(*) FROM documents WHERE source_id='public_b'"
            ).fetchone()[0]
            == 1
        )
        assert (
            collector.repo.db.execute(
                "SELECT COUNT(*) FROM failures WHERE source_id='mof'"
            ).fetchone()[0]
            == 1
        )
    finally:
        collector.close()


def test_frozen_public_profile_survives_registry_change_on_resume(tmp_path, source_world):
    registry, pages, calls, _clients = source_world
    pages[registry.sources["public_a"].entry] = (
        "<ul id='items'><li><a href='one.html'>通知一</a><time>2026-11-01</time></li>"
        "<li><a href='two.html'>通知二</a><time>2026-11-02</time></li></ul>"
    ).encode()
    pages["https://official.example.test/public_a/two.html"] = (
        "<h1>通知二</h1><main>第二条 虚构测试正文。</main>"
    ).encode()
    collector = collect(tmp_path, ["public_a"])
    try:
        first = collector.collect(request(["public_a"], max_documents=1))
        frozen = collector.repo.task(first.task_id)["request_json"]
        registry.sources["public_a"].entry = "https://official.example.test/replaced/"
        registry.sources["public_a"].profile["detail"]["body"] = "//aside"
        collector.resume(first.task_id)
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert collector.repo.task(first.task_id)["request_json"] == frozen
        assert not any("/replaced/" in url for url in calls)
    finally:
        collector.close()


def test_current_host_revocation_stops_frozen_resume_before_http(tmp_path, source_world):
    registry, _pages, calls, _clients = source_world
    collector = collect(tmp_path, ["public_a"])
    try:
        task = collector.repo.create_task(request(["public_a"]))
        registry.sources["public_a"].allowed_hosts = ["revoked.example.test"]
        result = collector.resume(task)
        assert calls == []
        assert "SOURCE_FAILURE" in result.data["stop_reasons"]
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    finally:
        collector.close()


@pytest.mark.parametrize("budget", [{"max_documents_per_batch": 1}, {"max_pages_per_batch": 1}])
def test_batch_budget_is_shared_across_sources(tmp_path, source_world, budget):
    _registry, _pages, calls, _clients = source_world
    collector = collect(tmp_path, ["public_a", "public_b"], **budget)
    try:
        result = collector.collect(
            request(["public_a", "public_b"], max_pages=10, max_documents=10)
        )
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert not any("/public_b/" in url for url in calls)
        assert "BUDGET_REACHED" in result.data["stop_reasons"]
    finally:
        collector.close()


def test_source_clients_share_host_spacing(tmp_path, source_world, monkeypatch):
    from ftr import network

    _registry, _pages, _calls, clients = source_world
    clock = {"now": 100.0}
    starts = []
    monkeypatch.setattr(network.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        BoundedClient, "_wait", lambda self, duration: clock.update(now=clock["now"] + duration)
    )
    original = BoundedClient._before

    def before(client, url):
        original(client, url)
        starts.append(clock["now"])

    monkeypatch.setattr(BoundedClient, "_before", before)
    settings = RuntimeSettings(
        data_dir=tmp_path, network={"proxy_mode": "direct", "interval_seconds": 2}
    )
    collector = Collector(tmp_path, settings)
    try:
        collector.collect(request(["public_a", "public_b"]))
        assert len(clients) == 2 and len(starts) == 4
        assert all(second - first >= 2 for first, second in pairwise(starts))
    finally:
        collector.close()


def test_multiple_source_preview_and_catalog_are_readonly(tmp_path, source_world):
    registry, _pages, calls, _clients = source_world
    with closing(Repository(tmp_path)):
        pass
    before = (tmp_path / "database.sqlite3").read_bytes()
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        catalog = client.get("/api/sources", params={"region": "乙省", "category": "tax"})
        assert [item["source_id"] for item in catalog.json()["items"]] == ["public_b"]
        response = client.post(
            "/api/sources/preview", json=request(["public_a", "public_b"]).model_dump(mode="json")
        )
        assert response.status_code == 200
        value = response.json()["request"]
        assert value["source_ids"] == ["public_a", "public_b"]
        assert set(value["source_snapshot"]) == {"public_a", "public_b"}
        assert (
            value["source_snapshot"]["public_a"]["config"]["entry"]
            == registry.sources["public_a"].entry
        )
        invalid = json.loads(json.dumps(value))
        invalid["source_ids"] = ["not_registered"]
        assert client.post("/api/sources/preview", json=invalid).status_code == 422
    assert (tmp_path / "database.sqlite3").read_bytes() == before
    assert calls == []


def test_out_of_range_detail_evidence_counts_toward_batch_bytes(tmp_path, source_world):
    _registry, pages, calls, _clients = source_world
    for url in list(pages):
        if url.endswith("one.html"):
            pages[url] = pages[url].replace(b"2026-11-01", b"2025-11-01")
    collector = collect(tmp_path, ["public_a", "public_b"])
    try:
        result = collector.collect(request(["public_a", "public_b"], date_basis="published_date"))
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        fetched_bytes = sum(len(pages[url]) for url in calls)
        accounted = collector.repo.db.execute(
            "SELECT SUM(bytes_count) FROM source_runs WHERE task_id=?", (result.task_id,)
        ).fetchone()[0]
        assert accounted >= fetched_bytes, "筛选排除前已保存的正文原件不能绕过整批字节预算"
    finally:
        collector.close()


def test_recovered_attachments_share_batch_byte_limit(tmp_path, source_world):
    _registry, pages, calls, _clients = source_world
    base = "https://official.example.test/public_a/"
    pages[base + "one.doc"] = b"a" * 300
    pages[base + "two.doc"] = b"b" * 300
    collector = collect(tmp_path, ["public_a"], max_bytes_per_batch=500)
    try:
        task = collector.repo.create_task(request(["public_a"]))
        evidence = collector.evidence.save(
            b"prior body", base + "one.html", base + "one.html", "text/html"
        )
        collector.repo.save_evidence(evidence)
        record = DocumentRecord(
            source_id="public_a",
            source_url=base + "one.html",
            canonical_url=base + "one.html",
            listing_title="虚构原文",
            title="虚构原文",
            body_text="先前已保存的测试正文",
            document_type=DocumentType.POLICY_FILE,
            evidence_id=evidence.evidence_id,
            listing_date=date(2026, 11, 1),
            listing_date_kind="published_date",
            attachments=[
                Attachment(url=base + "one.doc", label="附件一"),
                Attachment(url=base + "two.doc", label="附件二"),
            ],
        )
        collector.repo.add_document(task, record)
        collector.repo.save_page(task, "public_a", [], None)
        first = collector.resume(task)
        assert first.data["stop_reasons"] == ["BUDGET_REACHED"]
        assert first.data["pending_attachments"] == 1
        assert collector.repo.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
        assert collector.repo.source_run(task, "public_a")["bytes_count"] <= 500
        second = collector.resume(task)
        assert second.data["pending_attachments"] == 0
        assert collector.repo.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
        assert collector.repo.source_run(task, "public_a")["bytes_count"] == 600
        assert calls.count(base + "one.doc") == 1
        assert calls.count(base + "two.doc") == 2
    finally:
        collector.close()


def test_original_navigation_evidence_respects_batch_byte_limit(
    tmp_path, source_world, monkeypatch
):
    from ftr.adapters.public import PublicAdapter

    _registry, _pages, _calls, _clients = source_world
    original = PublicAdapter._read

    def captured_dom(adapter, url, **kwargs):
        raw, final, media = original(adapter, url, **kwargs)
        # Browser navigation HTML can be larger than its rendered DOM snapshot.
        adapter.last_original_responses = [(b"original navigation" * 100, final, "text/html", 200)]
        adapter.last_capture["capture_kind"] = "browser_dom"
        return raw, final, media

    monkeypatch.setattr(PublicAdapter, "_read", captured_dom)
    collector = collect(tmp_path, ["public_a"], max_bytes_per_batch=500)
    try:
        result = collector.collect(request(["public_a"]))
        assert result.data["stop_reasons"] == ["BUDGET_REACHED"]
        assert collector.repo.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
        assert collector.repo.source_run(result.task_id, "public_a")["next_page"] == 1
        saved_bytes = sum(
            path.stat().st_size for path in (tmp_path / "evidence").rglob("*") if path.is_file()
        )
        assert saved_bytes <= 500, "原始导航响应与DOM证据必须共用有界预算"
    finally:
        collector.close()


@pytest.mark.parametrize("limit", ["max_bytes_per_source", "max_bytes_per_batch"])
def test_scheduler_rotates_and_resumes_after_response_exceeds_remaining_bytes(
    tmp_path, source_world, monkeypatch, limit
):
    from ftr import management
    from ftr.scheduler import Scheduler, read_states

    _registry, _pages, calls, _clients = source_world
    settings = RuntimeSettings(
        data_dir=tmp_path,
        network={"proxy_mode": "direct", "interval_seconds": 0, "max_file_bytes": 1000},
        collection={limit: 200},
        scheduler={"enabled": True},
    )
    monkeypatch.setattr(
        management,
        "effective_plan",
        lambda *_: {
            "settings": settings.scheduler.model_dump(mode="json"),
            "scope": {"source_ids": ["public_a", "public_b"]},
        },
    )
    scheduler = Scheduler(settings)
    now = datetime(2026, 11, 6, 1, tzinfo=UTC)
    tasks = {}
    for source in ("public_a", "public_b"):
        assert scheduler.tick(now)["source_id"] == source
        with closing(Repository(tmp_path, readonly=True)) as repo:
            state = read_states(repo.db)[source]
            assert state["paused_reason"] is None
            assert state["retry_count"] == 0
            tasks[source] = state["active_task_id"]
            assert repo.source_run(tasks[source], source)["discovery_done"]
            assert len(repo.pending_refs(tasks[source], source)) == 1
            assert repo.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
    for source in ("public_a", "public_b"):
        assert scheduler.tick(now + timedelta(seconds=61))["source_id"] == source
        with closing(Repository(tmp_path, readonly=True)) as repo:
            state = read_states(repo.db)[source]
            assert state["paused_reason"] is None
            assert state["active_task_id"] is None
            assert state["last_task_id"] == tasks[source]
            assert not repo.pending_refs(tasks[source], source)
            assert repo.db.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0
    for source in ("public_a", "public_b"):
        assert calls.count(f"https://official.example.test/{source}/") == 1
        assert calls.count(f"https://official.example.test/{source}/one.html") == 2


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需 Chromium 请求中断验收")
def test_browser_shutdown_cancels_pending_response_without_waiting_for_origin():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from playwright.sync_api import Error, sync_playwright

    from ftr.runtime import drain_private_context

    received = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/slow":
                received.set()
                release.wait(8)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"<html>isolated pending-response test</html>")
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            page = context.new_page()

            def pending(route):
                try:
                    route.fulfill(response=route.fetch(timeout=10_000))
                except Error:
                    pass  # The owning context intentionally cancels this request.

            context.route("**/slow", pending)
            page.goto(f"http://127.0.0.1:{server.server_port}")
            page.evaluate("() => {void fetch('/slow').catch(() => {});}")
            page.wait_for_timeout(150)
            assert received.is_set()
            started = time.monotonic()
            drain_private_context(context)
            context.close()
            assert time.monotonic() - started < 3, "收尾不能等待尚未返回的官网响应"
            assert not release.is_set()
            browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("retain_original", [True, False])
def test_dom_requires_original_but_capture_notes_do_not_forbid_review(
    tmp_path, source_world, monkeypatch, retain_original
):
    from ftr.adapters.public import PublicAdapter
    from ftr.management import review_detail

    original = PublicAdapter._read

    def captured(adapter, url, **kwargs):
        raw, final, media = original(adapter, url, **kwargs)
        if kwargs.get("listing"):
            return raw, final, media
        adapter.last_original_responses = [(raw, final, media, 200)] if retain_original else []
        adapter.last_capture["capture_kind"] = "browser_dom"
        return (
            raw.replace(
                b"<html>",
                b'<html><head><meta name="ftr-capture-kind" content="browser_dom"></head>',
            ),
            final,
            "application/vnd.ftr.browser-dom+html; charset=utf-8",
        )

    monkeypatch.setattr(PublicAdapter, "_read", captured)
    collector = collect(tmp_path, ["public_a"])
    try:
        collector.collect(request(["public_a"]))
        row = collector.repo.db.execute("SELECT id FROM documents").fetchone()
        if retain_original:
            assert row
            record = collector.repo.get_document(row[0])
            assert record.collection_notes
            assert not record.limitations
            assert record.quality_state == "collected"
            assert review_detail(tmp_path, row[0])["can_pass"]
        else:
            assert row is None
            assert collector.repo.db.execute(
                "SELECT COUNT(*) FROM failures WHERE category='STRUCTURE_DRIFT'"
            ).fetchone()[0]
    finally:
        collector.close()


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需 Chromium 来源目录验收")
@pytest.mark.parametrize("width", [1440, 390])
def test_browser_source_catalog_filter_freeze_download_and_import(tmp_path, source_world, width):
    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    from ftr.config import WebSettings
    from ftr.management import effective_plan
    from ftr.web.management import ManagementSession

    registry, _pages, calls, _clients = source_world
    registry.sources["public_a"].name = "甲省税务政策（虚构回归）"
    registry.sources["public_b"].name = "乙省财政政策（虚构回归）"
    registry.sources["public_b"].categories = ["fiscal"]
    with closing(Repository(tmp_path)):
        pass
    before_bytes = (tmp_path / "database.sqlite3").read_bytes()
    before_plan = effective_plan(tmp_path)
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        port = allocation.getsockname()[1]
    assert port != 8765
    settings = RuntimeSettings(
        data_dir=tmp_path,
        web=WebSettings(port=port, poll_interval_ms=300),
        collection={
            "max_pages_per_batch": 77,
            "max_documents_per_batch": 31,
            "max_bytes_per_batch": 123456,
        },
        update_check={"enabled": False},
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
            access_log=False,
            log_level="warning",
            proxy_headers=False,
        )
    )
    web_thread = threading.Thread(target=server.run, daemon=True)
    web_thread.start()
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.03)
        else:
            pytest.fail("隔离来源目录测试服务未启动")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": width, "height": 1000}, accept_downloads=True
            )
            page = context.new_page()
            page_errors = []
            commands = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "request",
                lambda item: (
                    commands.append(item.url)
                    if item.method == "POST" and item.url.endswith("/api/manage/commands")
                    else None
                ),
            )
            page.route(
                "**/*",
                lambda route: (
                    route.continue_() if route.request.url.startswith(url + "/") else route.abort()
                ),
            )
            try:
                page.goto(url + "#manage-token=" + token)
                expect(page.locator("#management-mode")).to_contain_text("本机管理")
                expect(page.locator("#schedule-form")).to_contain_text("日常计划来源")
                page.get_by_role("button", name="查看采集来源").click()
                dialog = page.get_by_role("dialog", name="采集来源")
                expect(dialog).to_be_visible()
                # showModal precedes the asynchronous catalog reload; wait for content.
                expect(
                    dialog.get_by_role("checkbox", name="甲省税务政策（虚构回归）")
                ).to_be_visible()
                region = dialog.get_by_label("来源地区", exact=True)
                category = dialog.get_by_label("来源类别", exact=True)
                region.select_option(label="甲省")
                category.select_option(label="税收")
                expect(dialog.locator(".source-card")).to_have_count(1)
                dialog.get_by_role("button", name="关闭来源详情").click()
                with page.expect_response(lambda item: item.url.endswith("/api/sources")):
                    page.get_by_role("button", name="查看采集来源").click()
                expect(region).to_have_value("甲省")
                expect(category).to_have_value("tax")
                expect(dialog.locator(".source-card")).to_have_count(1)
                dialog.get_by_role("checkbox", name="甲省税务政策（虚构回归）").check()
                region.select_option(label="乙省")
                expect(dialog.locator(".source-card")).to_have_count(0)
                category.select_option(label="财政")
                expect(dialog.locator(".source-card")).to_have_count(1)
                dialog.get_by_role("checkbox", name="乙省财政政策（虚构回归）").check()
                region.select_option(label="全部地区")
                category.select_option(label="全部类别")
                expect(
                    dialog.get_by_role("checkbox", name="甲省税务政策（虚构回归）")
                ).to_be_checked()
                expect(
                    dialog.get_by_role("checkbox", name="乙省财政政策（虚构回归）")
                ).to_be_checked()
                expect(dialog.locator("#source-selection-count")).to_contain_text("已选 2 个")
                dialog.get_by_label("开始日期", exact=True).fill("2026-11-01")
                dialog.get_by_label("结束日期", exact=True).fill("2026-11-06")
                dialog.get_by_label("每来源列表页", exact=True).fill("4")
                dialog.get_by_label("每来源正文", exact=True).fill("7")
                with page.expect_response(
                    lambda item: (
                        item.url.endswith("/api/sources/preview") and item.request.method == "POST"
                    )
                ) as received:
                    dialog.get_by_role("button", name="预览冻结范围", exact=True).click()
                assert received.value.status == 200
                frozen = received.value.json()["request"]
                expect(dialog.locator("#source-scope-preview")).to_contain_text(
                    "77 页 / 31 份 / 123456 字节"
                )
                expect(dialog.locator("#source-scope-preview")).to_contain_text("4 页 / 7 份正文")
                with page.expect_download() as downloaded:
                    dialog.get_by_role("button", name="下载任务请求", exact=True).click()
                path = tmp_path / "downloaded-source-request.json"
                downloaded.value.save_as(path)
                value = json.loads(path.read_text())
                assert value == frozen
                assert set(value["source_ids"]) == {"public_a", "public_b"}
                assert value["date_from"] == "2026-11-01" and value["date_to"] == "2026-11-06"
                assert value["max_pages"] == 4 and value["max_documents"] == 7
                assert set(value["source_snapshot"]) == {"public_a", "public_b"}
                for source_id in value["source_ids"]:
                    assert (
                        value["source_snapshot"][source_id]["config"]["entry"]
                        == registry.sources[source_id].entry
                    )
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                assert dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
                page.screenshot(path=str(tmp_path / f"source-catalog-{width}.png"))
                dialog.get_by_label("每来源列表页", exact=True).fill("5")
                expect(
                    dialog.get_by_role("button", name="下载任务请求", exact=True)
                ).to_be_disabled()
                dialog.get_by_role("button", name="用于立即获取选源", exact=True).click()
                expect(dialog).not_to_be_visible()
                expect(page.locator("#scheduler-view")).to_be_visible()
                expect(page.locator('[name="now-source"]:checked')).to_have_count(2)
                assert set(
                    page.locator('[name="now-source"]:checked').evaluate_all(
                        "nodes=>nodes.map(node=>node.value)"
                    )
                ) == {"public_a", "public_b"}
                assert set(
                    page.locator('[name="plan-source"]:checked').evaluate_all(
                        "nodes=>nodes.map(node=>node.value)"
                    )
                ) == set(before_plan["scope"]["source_ids"])
                expect(page.locator("#run-now")).to_be_disabled()
                page.get_by_role("button", name="预览本轮窗口", exact=True).click()
                expect(page.locator("#run-now")).to_be_enabled()
                now_scope = json.loads(page.locator("#run-now").get_attribute("data-window"))
                assert set(now_scope["sources"]) == {"public_a", "public_b"}
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth + 1"
                )
                page.screenshot(path=str(tmp_path / f"source-import-{width}.png"))
                assert not page_errors
                assert not commands
            except Exception:
                page.screenshot(path=str(tmp_path / f"source-catalog-failure-{width}.png"))
                raise
            finally:
                context.close()
                browser.close()
    finally:
        server.should_exit = True
        web_thread.join(timeout=5)
        assert not web_thread.is_alive()
    after_plan = effective_plan(tmp_path)
    assert (after_plan["revision"], after_plan["scope"], after_plan["settings"]) == (
        before_plan["revision"],
        before_plan["scope"],
        before_plan["settings"],
    )
    assert (tmp_path / "database.sqlite3").read_bytes() == before_bytes
    assert calls == []
