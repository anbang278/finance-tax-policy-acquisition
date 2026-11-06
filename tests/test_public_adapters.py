"""Isolated parsing and browser-boundary tests; never contact official hosts."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin

import pytest
import yaml
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from pydantic import ValidationError

from ftr.adapters.public import DOM_MEDIA_TYPE, PublicAdapter, PublicProfile
from ftr.config import SourceConfig
from ftr.models import DiscoveredRef, DocumentType, digest
from ftr.network import AccessBlocked
from ftr.rules import StructureDrift

ENTRY = "https://official.example/policy/"
DETAIL = "https://official.example/policy/a.html"
PUBLIC_SOURCE_FIXTURES = Path(__file__).parent / "fixtures" / "public_sources"


def profile():
    return {
        "schema_version": "1",
        "listing": {
            "container": "//*[@id='list']",
            "items": "./li",
            "link": "./a",
            "date": "./span",
            "empty_selector": "//*[@id='empty']",
        },
        "pagination": {"mode": "none"},
        "detail": {"title": "//h1", "body": "//*[@id='body']"},
    }


def config(value=None, *, browser=False, **changes):
    return SourceConfig(
        name="公开机关政策栏目",
        entry=ENTRY,
        allowed_hosts=["official.example"],
        listing_date_kind="column_date",
        adapter="public_browser" if browser else "public_html",
        profile=profile() if value is None else value,
        publisher="登记机关不能冒充页面来源",
        region="登记地区不能冒充适用地区",
        categories=["税收"],
        registry_version="test-v1",
        **changes,
    )


def listing(href="a.html", title="关于2023年政策的通知", extra=""):
    return (
        f"<html><body><ul id='list'><li><a href='{href}'>{title}</a>"
        f"<span>2026-11-06</span></li></ul>{extra}</body></html>"
    ).encode()


class Client:
    proxy = None
    max_bytes = 50_000_000

    def __init__(self, pages):
        self.pages = pages
        self.calls = []
        self.before_calls = []
        self.before_request = self.before_calls.append
        self.checkpoint = None
        self.public_browser_service_workers_blocked = True

    def _before(self, url):
        if self.checkpoint:
            self.checkpoint()
        if self.before_request:
            self.before_request(url)

    def get(self, url):
        self._before(url)
        self.calls.append(url)
        value = self.pages[url]
        return value if isinstance(value, tuple) else (value, url, "text/html")


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    # The real client/check_url remains unchanged. Fixtures may use fake hosts.
    monkeypatch.setattr("ftr.adapters.public.check_url", lambda *_args: "official.example")


def ref(url=DETAIL):
    return DiscoveredRef(
        source_id="public_test",
        url=url,
        listing_title="栏目标题",
        listing_date=date(2026, 11, 6),
        listing_date_kind="column_date",
        discovered_from=ENTRY,
    )


def test_static_template_listing_preserves_date_semantics_and_capture():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_variable": "countPage",
    }
    raw = listing(extra="<script>var countPage = 2;</script>")
    last = listing("b.html", extra="<script>var countPage = 2;</script>")
    client = Client({ENTRY: raw, ENTRY + "index_1.html": last})
    adapter = PublicAdapter("public_test", config(p), client)
    refs, next_page, returned_raw, media = adapter.discover(1)
    assert refs[0].listing_date == date(2026, 11, 6)
    assert refs[0].listing_date_kind == "column_date"
    assert refs[0].url == DETAIL and next_page == 2
    assert returned_raw == raw and media == "text/html"
    assert adapter.last_response == (raw, ENTRY, "text/html", 1)
    assert adapter.last_capture["capture_kind"] == "http_response"
    assert adapter.discover(2)[1] is None
    assert client.before_calls == [ENTRY, ENTRY + "index_1.html"]


@pytest.mark.parametrize(
    "raw",
    [
        b"<div id='app'></div><script src='app.js'></script>",
        b"<ul id='list'></ul>",
        "<ul id='list'><span id='empty' style='display:none'>暂无数据</span></ul>".encode(),
        "<h1>您访问的页面不存在或已删除</h1>".encode(),
        "<ul id='list'></ul><p>请输入验证码</p>".encode(),
    ],
)
def test_shell_hidden_empty_error_and_verification_are_not_empty_results(raw):
    adapter = PublicAdapter("public_test", config(), Client({ENTRY: raw}))
    with pytest.raises(StructureDrift):
        adapter.discover(1)
    assert adapter.last_response[0] == raw


def test_explicit_empty_is_allowed_but_contradictory_rows_are_not():
    empty = "<ul id='list'></ul><p id='empty'>暂无数据</p>".encode()
    adapter = PublicAdapter("public_test", config(), Client({ENTRY: empty}))
    assert adapter.discover(1)[:2] == ([], None)
    bad = listing(extra="<p id='empty'>暂无数据</p>")
    with pytest.raises(StructureDrift, match="冲突"):
        PublicAdapter("public_test", config(), Client({ENTRY: bad})).discover(1)


@pytest.mark.parametrize(
    "bad",
    [
        "http://official.example:81/a",
        "https://official.example.evil/a",
        "https://evil.example/a",
        "https://user:secret@official.example/a",
        "https://official.example:444/a",
        "javascript:alert(1)",
    ],
)
def test_selected_listing_links_fail_closed_for_unapproved_host_protocol_or_credentials(bad):
    adapter = PublicAdapter("public_test", config(), Client({ENTRY: listing(bad)}))
    with pytest.raises((AccessBlocked, StructureDrift)):
        adapter.discover(1)


def test_redirect_destination_checked_even_for_a_faulty_client():
    client = Client({ENTRY: (listing(), "https://evil.example/", "text/html")})
    with pytest.raises(AccessBlocked):
        PublicAdapter("public_test", config(), client).discover(1)


def test_template_requires_observed_total_and_rejects_missing_or_inconsistent_value():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_variable": "pages",
    }
    with pytest.raises(StructureDrift, match="总页数"):
        PublicAdapter("public_test", config(p), Client({ENTRY: listing()})).discover(1)
    raw = listing(extra="<script>var pages = 0;</script>")
    with pytest.raises(StructureDrift, match="总页数"):
        PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)


def test_next_link_resume_uses_real_links_and_proves_terminal_page():
    p = profile()
    p["pagination"] = {
        "mode": "next_link",
        "next": "//a[@id='next']",
        "end_selector": "//span[@id='end']",
    }
    page_two = ENTRY + "actual.action?column=42&page=2"
    first = listing(extra=f"<a id='next' href='{page_two}'>下一页</a>")
    last = listing("b.html", extra="<span id='end'>末页</span>")
    client = Client({ENTRY: first, page_two: last})
    adapter = PublicAdapter("public_test", config(p), client)
    assert adapter.discover(2)[1] is None
    assert client.calls == [ENTRY, page_two]
    assert adapter.discover(1)[1] == 2  # A replay is not a pagination loop.
    with pytest.raises(StructureDrift, match="末页"):
        PublicAdapter("public_test", config(p), Client({ENTRY: listing()})).discover(1)


def test_no_date_fallback_from_title_path_footer_or_listing():
    raw = "<h1>2023年第7号</h1><div id='body'>正文。2024年1月1日起执行。</div>".encode()
    adapter = PublicAdapter("public_test", config(), Client({DETAIL: raw}))
    captured, final, _ = adapter.fetch_detail(DETAIL)
    record = adapter.parse_detail(ref(), captured, digest(raw))
    assert final == DETAIL
    assert record.issued_date is None and record.published_date is None
    assert record.source_publisher == "" and record.issuing_authorities == []
    assert record.applicable_regions == [] and record.content_stage == "unknown"
    assert (
        record.categories == ["税收"]
        and record.field_evidence["categories"] == "source_registry:test-v1"
    )


def test_explicit_metadata_stage_table_and_all_attachment_forms():
    p = profile()
    p["detail"].update(
        {
            "published_date": "//meta[@name='PubDate']/@content",
            "issued_date": "//*[@id='issued']",
            "document_number": "//*[@id='number']",
            "source_publisher": "//*[@id='source']",
            "issuing_authorities": "//*[@id='issuers']",
            "applicable_regions": "//*[@id='region']",
            "attachments": ["//*[@id='downloads']//a[@href]"],
            "primary_attachments": ["//a[@id='main']"],
            "related_links": ["//a[@id='related']"],
        }
    )
    raw = (
        "<html><head><meta name='PubDate' content='2026-11-06'></head><body>"
        "<h1>关于印发管理办法的通知</h1><p id='issued'>成文日期：2026-10-28</p>"
        "<p id='number'>文号：财资环〔2026〕129号</p><p id='source'>来源：财政部网站</p>"
        "<p id='issuers'>发文机关：财政部、应急管理部</p><p id='region'>适用地区：全国</p>"
        "<div id='body'><p>现予印发。</p><table><tr><th>项目</th><th>金额</th></tr>"
        "<tr><td rowspan='2'>甲</td><td>100</td></tr><tr><td>200</td></tr></table></div>"
        "<div id='downloads'><a id='main' href='main.pdf'>文件正文</a>"
        "<a href='form.doc'>说明</a><a href='form.wps'>说明</a><a href='form.xls'>表格</a>"
        "<a href='files.zip'>附件包</a><a href='/law/download?LawID=1843&amp;type=pdf'>图片版</a>"
        "<a href='/endpoint?id=7' download='guide.docx'>原件</a>"
        "<a href='https://cdn.example/files.pdf'>待授权附件</a></div>"
        "<a id='related' href='/explanation.html'>关联解读</a></body></html>"
    ).encode()
    adapter = PublicAdapter("public_test", config(p), Client({DETAIL: raw}))
    adapter.fetch_detail(DETAIL)
    record = adapter.parse_detail(ref(), raw, digest(raw))
    assert record.published_date == date(2026, 11, 6) and record.issued_date == date(2026, 10, 28)
    assert record.document_number == "财资环〔2026〕129号"
    assert record.source_publisher == "财政部网站"
    assert record.issuing_authorities == ["财政部", "应急管理部"]
    assert record.applicable_regions == ["全国"] and record.content_stage == "formal"
    assert '<td rowspan="2">甲</td>' in record.body_text and "<th>金额</th>" in record.body_text
    assert len(record.attachments) == 8
    assert record.attachments[0].content_role == "primary"
    assert all(a.extraction_state == "pending" for a in record.attachments)
    assert record.attachments[-1].download_state == "blocked"
    assert record.related_urls == ["https://official.example/explanation.html"]
    assert record.field_evidence["source_publisher"] == digest(raw)


def test_two_dates_in_one_metadata_field_are_unknown_and_drafts_are_not_formal():
    p = profile()
    p["detail"]["issued_date"] = "//*[@id='dates']"
    raw = (
        "<h1>管理办法（征求意见稿）</h1><p id='dates'>2026-01-01；2026-11-01</p>"
        "<div id='body'>现予公布征求意见。</div>"
    ).encode()
    record = PublicAdapter("public_test", config(p), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert record.issued_date is None and record.content_stage == "draft"


def test_qa_has_distinct_document_type_and_missing_body_does_not_use_whole_page():
    raw = "<h1>税费咨询答复</h1><div id='body'>问：如何办理？答：核对条件。</div>".encode()
    adapter = PublicAdapter("public_test", config(channel="qa"), Client({}))
    assert adapter.parse_detail(ref(), raw, digest(raw)).document_type == DocumentType.OFFICIAL_QA
    with pytest.raises(StructureDrift):
        adapter.parse_detail(ref(), b"<h1>Header only</h1><p>Footer</p>", "evidence")


@pytest.mark.parametrize(
    "change",
    [
        {"arbitrary_python": "eval()"},
        {"listing": {"container": "//*[system('touch x')]", "items": "./li"}},
        {"listing": {"container": "//ul", "items": "//a"}},
        {
            "pagination": {
                "mode": "template",
                "template": "../{index}.html",
                "total_pages_variable": "pages",
            }
        },
        {"pagination": {"mode": "template", "template": "index_{index}.html"}},
        {"pagination": {"mode": "browser_next", "next": "//button"}},
        {"detail": {"title": "//h1", "body": "//div", "attachments": "//a"}},
    ],
)
def test_profile_rejects_executable_unscoped_or_ambiguous_rules(change):
    p = profile() | change
    with pytest.raises(ValidationError):
        PublicProfile.model_validate(p)


def test_empty_profile_is_structure_drift_before_any_request():
    client = Client({})
    with pytest.raises(StructureDrift, match="profile"):
        PublicAdapter("public_test", config({}), client)
    assert not client.calls


def test_failed_next_read_never_reuses_previous_capture_as_new_evidence():
    client = Client({ENTRY: listing()})
    adapter = PublicAdapter("public_test", config(), client)
    adapter.discover(1)
    assert adapter.last_response is not None and adapter.last_capture["capture_complete"]
    with pytest.raises(KeyError):
        adapter.fetch_detail(DETAIL)
    assert adapter.last_response is None and adapter.last_original_responses == []
    assert adapter.last_capture == {
        "capture_kind": "http_attempt",
        "requested_url": DETAIL,
        "capture_complete": False,
    }


class Response:
    def __init__(self, url, body, *, status=200, media="text/html", resource_type="document"):
        self.url, self.raw, self.status = url, body, status
        self.headers = {"content-type": media}
        self.resource_type = resource_type

    def body(self):
        return self.raw


class Route:
    def __init__(self, response, *, navigation=True, resource_type="document"):
        self.response, self.aborted, self.fulfilled = response, False, False
        self.request = SimpleNamespace(
            url=response.url,
            is_navigation_request=lambda: navigation,
            resource_type=resource_type,
        )

    def fetch(self, **options):
        assert options["max_redirects"] == 0 and options["max_retries"] == 0
        return self.response

    def fulfill(self, **_):
        self.fulfilled = True

    def abort(self, *_):
        self.aborted = True


class Context:
    def __init__(self):
        self.handler = None
        self.websocket_handler = None

    def route(self, _pattern, handler):
        self.handler = handler

    def unroute(self, _pattern, handler):
        assert self.handler == handler
        self.handler = None

    def route_web_socket(self, _pattern, handler):
        self.websocket_handler = handler


class Page:
    def __init__(self, dom=None, responses=None, frame=None):
        self.context = Context()
        self.dom = dom or listing().decode()
        self.responses = responses or [Response(ENTRY, b"<html><div id='app'></div></html>")]
        self.frame = frame
        self.url = "about:blank"
        self.ready_args = []
        self.routes = []

    def goto(self, url, **_):
        self.url = url
        for response in self.responses:
            r = Route(
                response,
                navigation=response.resource_type == "document",
                resource_type=response.resource_type,
            )
            self.routes.append(r)
            self.context.handler(r)
            if r.aborted and response.resource_type == "document":
                raise PlaywrightError("Fixture navigation aborted")
        return self.responses[0]

    def wait_for_function(self, _fn, **options):
        self.ready_args.append(options["arg"])

    def content(self):
        return self.dom

    def locator(self, _selector):
        return SimpleNamespace(
            wait_for=lambda **_: None,
            count=lambda: 1,
            element_handle=lambda: SimpleNamespace(content_frame=lambda: self.frame),
        )


def test_websocket_sink_never_calls_synchronous_or_upstream_api():
    class SocketRoute:
        url = "wss://untrusted.example/socket?credential=not-recorded"

        def __getattr__(self, name):
            pytest.fail(f"WebSocket callback must not access an operation: {name}")

    page = Page()
    client = Client({})
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    adapter._install_guard()
    page.context.websocket_handler(SocketRoute())
    assert adapter.blocked_resources == [
        {
            "url": "wss://untrusted.example/socket",
            "resource_type": "websocket",
            "reason": "WEBSOCKET_UPSTREAM_NOT_CONNECTED",
        }
    ]
    assert adapter._browser_error is None and client.before_calls == []


@pytest.mark.skipif(
    os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需隔离Chromium WebSocket零上游回归"
)
def test_real_browser_websocket_sink_zero_upstream_and_bounded_close():
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent('''
        import json, socket, threading, time
        from types import SimpleNamespace
        from playwright.sync_api import sync_playwright
        from ftr.config import SourceConfig
        from ftr.adapters.public import PublicAdapter
        server=socket.socket()
        server.bind(('127.0.0.1',0));server.listen(4);server.settimeout(0.05)
        port=server.getsockname()[1];connections=[];stop=threading.Event()
        def watch():
            while not stop.is_set():
                try:
                    conn,peer=server.accept();connections.append(peer);conn.close()
                except socket.timeout: pass
                except OSError: return
        thread=threading.Thread(target=watch,daemon=True);thread.start()
        source=SourceConfig(name='isolated websocket regression',entry='https://official.example/',allowed_hosts=['official.example'],listing_date_kind='unknown',adapter='public_browser',profile={
            'listing':{'container':'//ul','items':'./li'},'pagination':{'mode':'none'},'detail':{'title':'//h1','body':'//main'}})
        closed_seconds=None;audits=[]
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                context=browser.new_context(service_workers='block');page=context.new_page()
                adapter=PublicAdapter('public_test',source,SimpleNamespace(public_browser_service_workers_blocked=True),page)
                adapter._install_guard()
                # Navigate to an intercepted same-origin fixture so Chromium's
                # local-network policy and router injection are exercised.
                fixture=f'http://127.0.0.1:{port}/fixture'
                page.route(fixture,lambda route:route.fulfill(status=200,content_type='text/html',body='<main><h1>isolated page</h1></main>'))
                page.goto(fixture)
                page.unroute(fixture)
                page.evaluate("""url=>{
                    window.opened=0;window.sockets=[];
                    for (const suffix of ['/close-in-page?token=redacted','/close-in-context']) {
                        const ws=new WebSocket(url+suffix);
                        ws.onopen=()=>{window.opened++;ws.send('must stay in local sink');};
                        window.sockets.push(ws);
                    }
                }""",f'ws://127.0.0.1:{port}')
                page.wait_for_function('()=>window.opened===2',timeout=2000)
                assert page.evaluate('()=>6*7')==42
                page.evaluate('()=>window.sockets[0].close()')
                page.wait_for_function('()=>window.sockets[0].readyState===WebSocket.CLOSED',timeout=2000)
                assert len(adapter.blocked_resources)==2
                audits=list(adapter.blocked_resources)
                assert all(x['reason']=='WEBSOCKET_UPSTREAM_NOT_CONNECTED' for x in audits)
                assert 'token' not in str(audits)
                started=time.monotonic()
                page.close(run_before_unload=False)
                context.unroute_all(behavior='ignoreErrors');context.close();browser.close()
                closed_seconds=time.monotonic()-started
        finally:
            stop.set();server.close();thread.join(1)
        assert not connections, f'unexpected upstream TCP/WebSocket handshake: {connections}'
        assert closed_seconds<5
        print(json.dumps({'upstream_connections':len(connections),'blocked_sockets':len(audits),'close_seconds':closed_seconds}))
    ''')
    # A regressed synchronous callback cannot hang the whole test process.
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=15, check=False
    )
    assert result.returncode == 0, result.stderr
    metrics = json.loads(result.stdout.strip())
    assert metrics["upstream_connections"] == 0 and metrics["blocked_sockets"] == 2
    assert metrics["close_seconds"] < 5


def test_browser_snapshot_is_not_original_and_each_request_is_bounded():
    page = Page()
    client = Client({})
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    refs, _, raw, media = adapter.discover(1)
    assert refs[0].url == DETAIL and media == DOM_MEDIA_TYPE
    assert b"ftr-capture-kind" in raw and b"browser_dom" in raw
    assert raw != adapter.last_original_responses[0][0]
    assert adapter.last_original_responses == [(page.responses[0].raw, ENTRY, "text/html", 200)]
    assert adapter.last_capture["capture_kind"] == "browser_dom"
    assert client.before_calls == [ENTRY] and page.routes[0].fulfilled
    assert page.ready_args[0]["ready"] == "//*[@id='list']/li"
    adapter.close()
    assert page.context.handler is None


@pytest.mark.parametrize("kind", ["listing", "empty", "detail"])
def test_readiness_credits_only_measured_guard_wait_without_renavigating(monkeypatch, kind):
    clock = [0.0]
    monkeypatch.setattr("ftr.adapters.public.time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = Client({})

    def before(url):
        client.before_calls.append(url)
        clock[0] += 6.0

    client.before_request = before

    class ThrottledPage(Page):
        gotos = 0

        def goto(self, *args, **options):
            self.gotos += 1
            return super().goto(*args, **options)

        def wait_for_function(self, fn, **options):
            self.ready_args.append(options["arg"])
            if len(self.ready_args) == 1:
                # A required subresource obeys a slow host-throttle callback
                # while the first Playwright predicate timeout is running.
                self.context.handler(
                    Route(
                        Response(ENTRY + "needed.js", b"/*source script*/"),
                        navigation=False,
                        resource_type="script",
                    )
                )
                clock[0] += 1
                raise PlaywrightTimeout("fixture wall timeout while host throttle blocks")
            assert options["timeout"] == pytest.approx(3000)
            clock[0] += 1

    dom = listing().decode()
    if kind == "empty":
        dom = '<ul id="list"></ul><p id="empty">暂无数据</p>'
    if kind == "detail":
        dom = '<h1>Title</h1><div id="body">Full body.</div>'
    page = ThrottledPage(dom=dom)
    adapter = PublicAdapter("public_test", config(browser=True), client, page, timeout_seconds=4)
    if kind == "detail":
        raw, _, _ = adapter.fetch_detail(DETAIL)
        assert adapter.parse_detail(ref(), raw, digest(raw)).body_text == "Full body."
        assert page.ready_args[0]["empty"] is None and page.ready_args[0]["container"] is None
    else:
        refs, _, _, _ = adapter.discover(1)
        assert bool(refs) == (kind == "listing")
        assert page.ready_args[0]["empty"] == "//*[@id='empty']"
    assert page.gotos == 1 and len(page.routes) == 1
    assert len(page.ready_args) == 2 and page.ready_args[0] == page.ready_args[1]
    assert len(client.before_calls) == 2
    timing = adapter.last_capture["readiness_wait"]
    assert timing == {
        "wall_seconds": 8.0,
        "guard_wait_seconds": 6.0,
        "active_seconds": 2.0,
        "active_timeout_seconds": 4.0,
        "predicate_wait_attempts": 2,
    }


def test_readiness_never_renews_an_exhausted_active_budget(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("ftr.adapters.public.time", SimpleNamespace(monotonic=lambda: clock[0]))

    class SlowDOM(Page):
        def wait_for_function(self, _fn, **options):
            self.ready_args.append(options["arg"])
            clock[0] += 4.1
            raise PlaywrightTimeout("real DOM active wait exhausted")

    page = SlowDOM()
    adapter = PublicAdapter(
        "public_test", config(browser=True), Client({}), page, timeout_seconds=4
    )
    with pytest.raises(StructureDrift):
        adapter.discover(1)
    assert len(page.ready_args) == 1 and len(page.routes) == 1
    assert adapter.last_capture["readiness_wait"]["predicate_wait_attempts"] == 1


def test_readiness_retry_preserves_owner_global_deadline(monkeypatch):
    clock = [0.0]
    client = Client({})
    monkeypatch.setattr("ftr.adapters.public.time", SimpleNamespace(monotonic=lambda: clock[0]))
    client.before_request = lambda _: clock.__setitem__(0, clock[0] + 3)

    def checkpoint():
        if clock[0] >= 7:
            raise RuntimeError("owner global deadline")

    client.checkpoint = checkpoint

    class OwnerDeadlinePage(Page):
        def wait_for_function(self, _fn, **options):
            self.ready_args.append(options["arg"])
            self.context.handler(
                Route(
                    Response(ENTRY + "needed.js", b"/*script*/"),
                    navigation=False,
                    resource_type="script",
                )
            )
            clock[0] += 1.1
            raise PlaywrightTimeout("wall timeout including throttle")

    page = OwnerDeadlinePage()
    adapter = PublicAdapter("public_test", config(browser=True), client, page, timeout_seconds=4)
    with pytest.raises(RuntimeError, match="owner global deadline"):
        adapter.discover(1)
    assert len(page.ready_args) == 1 and len(page.routes) == 1


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需真实离线Chromium就绪回归")
def test_real_browser_readiness_accepts_absent_previous_but_rejects_stale_list():
    from lxml import html
    from playwright.sync_api import sync_playwright

    from ftr.adapters.public import _READY

    fixture_base = PUBLIC_SOURCE_FIXTURES
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        attempted_requests = []

        def block(route):
            attempted_requests.append(route.request.url)
            route.abort()

        context.route("**/*", block)
        page = context.new_page()
        try:
            for name, path in [
                ("yunnan-policy", '//*[@id="zoom"]/p[1]'),
                ("tianjin-normative", '//*[@id="conntentNR"]/p[1]'),
            ]:
                tree = html.fromstring(
                    (fixture_base / f"regression-readiness-{name}-detail.html").read_bytes()
                )
                # Exercise the observed layout with independently authored, offline DOM.
                for node in tree.xpath("//script | //link | //iframe | //img"):
                    node.drop_tree()
                page.set_content(html.tostring(tree, encoding="unicode"))
                args = {
                    "ready": path,
                    "empty": None,
                    "container": None,
                    "empty_text": [],
                    "previous": None,
                }
                assert page.evaluate(_READY, args) is True
                page.wait_for_function(_READY, arg=args, timeout=1000)
            page.set_content('<ul id="list"><li>First</li></ul><p id="empty">暂无数据</p>')
            previous = page.locator("#list").inner_html()
            args = {
                "ready": '//*[@id="list"]/li',
                "empty": '//*[@id="empty"]',
                "container": '//*[@id="list"]',
                "empty_text": ["暂无数据"],
                "previous": previous,
            }
            with pytest.raises(PlaywrightTimeout):
                page.wait_for_function(_READY, arg=args, timeout=120)
            page.locator("#list").evaluate("n=>n.innerHTML='<li>Second</li>'")
            page.wait_for_function(_READY, arg=args, timeout=1000)
            page.set_content('<ul id="list"></ul><p id="empty">暂无数据</p>')
            args["previous"] = None
            page.wait_for_function(_READY, arg=args, timeout=1000)
            # A detail can never use a listing's explicit-empty marker as body.
            detail = {
                "ready": '//*[@id="body"]',
                "empty": None,
                "container": None,
                "empty_text": [],
                "previous": None,
            }
            with pytest.raises(PlaywrightTimeout):
                page.wait_for_function(_READY, arg=detail, timeout=120)
        finally:
            page.close()
            context.close()
            browser.close()


def test_browser_requires_runtime_service_worker_attestation():
    client = Client({})
    client.public_browser_service_workers_blocked = False
    with pytest.raises(AccessBlocked, match="service_workers"):
        PublicAdapter("public_test", config(browser=True), client, Page()).discover(1)
    assert not client.before_calls


@pytest.mark.parametrize(
    "status,error", [(302, AccessBlocked), (403, AccessBlocked), (404, StructureDrift)]
)
def test_browser_status_and_redirect_errors_keep_original_response(status, error):
    page = Page(responses=[Response(ENTRY, b"Error or redirect body", status=status)])
    adapter = PublicAdapter("public_test", config(browser=True), Client({}), page)
    with pytest.raises(error):
        adapter.discover(1)
    assert page.routes[0].aborted
    assert adapter.last_original_responses[0][3] == status


def test_browser_subrequest_cannot_escape_host_or_bypass_budget():
    page = Page(
        responses=[Response(ENTRY, b"<html></html>"), Response("https://evil.example/a", b"bad")]
    )
    client = Client({})
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    with pytest.raises(AccessBlocked):
        adapter.discover(1)
    assert client.before_calls == [ENTRY]
    assert page.routes[1].aborted

    client = Client({})
    client.before_request = lambda _: (_ for _ in ()).throw(RuntimeError("budget reached"))
    with pytest.raises(RuntimeError, match="budget reached"):
        PublicAdapter("public_test", config(browser=True), client, Page()).discover(1)


def test_browser_only_follows_registered_iframe_and_uses_its_link_base():
    p = profile()
    p["browser"] = {"iframe": "//iframe[@id='policy']"}
    frame_url = "https://official.example/inner/list.html"
    frame = Page(dom=listing("document.html").decode())
    frame.url = frame_url
    page = Page(
        frame=frame,
        responses=[
            Response(ENTRY, b"<iframe></iframe>"),
            Response(frame_url, b"<div>original frame</div>"),
        ],
    )
    adapter = PublicAdapter("public_test", config(p, browser=True), Client({}), page)
    refs, _, _, _ = adapter.discover(1)
    assert refs[0].url == "https://official.example/inner/document.html"
    assert len(adapter.last_original_responses) == 2
    assert adapter.last_capture["document_url"] == frame_url
    # A listing iframe is deliberately not inherited by detail navigation.
    page.dom = "<h1>详情</h1><div id='body'>原文。</div>"
    page.responses = [Response(DETAIL, b"<h1>Original detail</h1>")]
    raw, final, media = adapter.fetch_detail(DETAIL)
    assert final == DETAIL and media == DOM_MEDIA_TYPE
    record = adapter.parse_detail(ref(), raw, digest(raw))
    assert record.body_text == "原文。"
    assert any("DOM快照" in x for x in record.collection_notes)
    assert not record.limitations


@pytest.mark.parametrize("hard_gap", [False, True])
def test_browser_collection_notes_do_not_block_review_but_real_gaps_do(hard_gap):
    from ftr.management import _review_item

    control = "<button>附件下载</button>" if hard_gap else ""
    page = Page(
        dom=f'<h1>Title</h1><div id="body">Complete visible body.{control}</div>',
        responses=[
            Response(DETAIL, b"<h1>Original</h1>"),
            Response(DETAIL + "/decor.png", b"", resource_type="image"),
        ],
    )
    adapter = PublicAdapter("public_test", config(browser=True), Client({}), page)
    raw, _, _ = adapter.fetch_detail(DETAIL)
    record = adapter.parse_detail(ref(), raw, digest(raw))
    assert adapter.last_original_responses
    assert len(record.collection_notes) == 2
    assert bool(record.limitations) == hard_gap

    class ReadOnlyReviewStub:
        def execute(self, query, _parameters=()):
            value = ("record_test",) if query.startswith("SELECT id FROM documents") else None
            return SimpleNamespace(fetchone=lambda: value)

    row = {
        "id": "record_test",
        "record_json": record.model_dump_json(),
        "source_id": record.source_id,
        "canonical_url": record.canonical_url,
        "task_id": "task_test",
    }
    review = _review_item(ReadOnlyReviewStub(), row)
    assert review["category"] == ("limited" if hard_gap else "pending")
    assert review["can_pass"] is (not hard_gap)


def test_browser_xhr_originals_and_capture_byte_limit():
    page = Page()
    client = Client({})
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    adapter._install_guard()
    response = Response(ENTRY + "rows", b'{"data":[1]}', media="application/json")
    route = Route(response, navigation=False, resource_type="xhr")
    adapter._route(route)
    assert route.fulfilled and adapter._document_responses[0][2] == "application/json"
    client.max_bytes = 1
    second = Route(response, navigation=False, resource_type="fetch")
    adapter._route(second)
    assert second.aborted and isinstance(adapter._browser_error, AccessBlocked)


@pytest.mark.parametrize("resource_type", ["document", "xhr", "script"])
def test_browser_original_response_budget_propagates_without_access_failure(resource_type):
    from ftr.runtime import BudgetReached, Collector

    client = Client({})
    client.max_bytes = 1000
    owner = SimpleNamespace(
        _checkpoint=lambda *_: None,
        _remaining_bytes=lambda *_: 100,
        settings=SimpleNamespace(network=SimpleNamespace(max_file_bytes=1000)),
        detail_limit=None,
    )
    client.before_request = lambda url: Collector._network_request(
        owner, "task", "public_test", 0, url, client
    )
    page = Page(responses=[Response(ENTRY, b"x" * 150, resource_type=resource_type)])
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    with pytest.raises(BudgetReached):
        adapter.discover(1)
    assert page.routes[0].aborted
    assert not page.routes[0].fulfilled


def test_browser_combined_original_response_budget_remains_resumable():
    from ftr.runtime import BudgetReached, Collector

    client = Client({})
    owner = SimpleNamespace(
        _checkpoint=lambda *_: None,
        _remaining_bytes=lambda *_: 100,
        settings=SimpleNamespace(network=SimpleNamespace(max_file_bytes=1000)),
        detail_limit=None,
    )
    client.before_request = lambda url: Collector._network_request(
        owner, "task", "public_test", 0, url, client
    )
    page = Page(
        responses=[
            Response(ENTRY, b"x" * 60),
            Response(ENTRY + "data", b"y" * 60, resource_type="xhr"),
        ]
    )
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    with pytest.raises(BudgetReached):
        adapter.discover(1)
    assert page.routes[0].fulfilled and page.routes[1].aborted
    assert not page.routes[1].fulfilled


def test_trs_function_total_is_only_the_registered_numeric_argument():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "policy_{index}.shtml",
        "total_pages_function": "createPageHTML",
        "total_pages_argument": 1,
    }
    first = listing(extra="<script>createPageHTML('page_div',2,0,'policy','shtml',32);</script>")
    last = listing(extra="<script>createPageHTML('page_div',2,1,'policy','shtml',32);</script>")
    client = Client({ENTRY: first, ENTRY + "policy_1.shtml": last})
    adapter = PublicAdapter("public_test", config(p), client)
    assert adapter.discover(1)[1] == 2
    assert adapter.discover(2)[1] is None


@pytest.mark.parametrize(
    "script",
    [
        "createPageHTML('page_div','2',0,'policy','shtml',32);",
        "createPageHTML('page_div',getTotal(),0,'policy','shtml',32);",
        "createPageHTML('page_div',totalPages,0,'policy','shtml',32);",
        "createPageHTML('page_div');",
        "createPageHTML('page_div',2);createPageHTML('page_div',3);",
    ],
)
def test_trs_function_does_not_execute_or_guess_nonliteral_page_count(script):
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "policy_{index}.shtml",
        "total_pages_function": "createPageHTML",
        "total_pages_argument": 1,
    }
    with pytest.raises(StructureDrift):
        PublicAdapter(
            "public_test", config(p), Client({ENTRY: listing(extra=f"<script>{script}</script>")})
        ).discover(1)


def test_conflicting_total_evidence_is_drift():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_variable": "pages",
        "total_pages_function": "createPageHTML",
        "total_pages_argument": 1,
    }
    raw = listing(extra="<script>var pages=3;createPageHTML('page_div',2);</script>")
    with pytest.raises(StructureDrift, match="不一致"):
        PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)


def test_browser_next_clicks_registered_visible_control_and_keeps_xhr_original():
    p = profile()
    p["pagination"] = {
        "mode": "browser_next",
        "next": "//button[@id='next']",
        "end_selector": "//span[@id='end']",
    }

    class PagingPage(Page):
        clicks = 0

        def locator(self, selector):
            if selector == "xpath=//*[@id='list']":
                return SimpleNamespace(count=lambda: 1, inner_html=lambda **_: self.dom)
            assert selector == "xpath=//button[@id='next']"
            return SimpleNamespace(count=lambda: 1, is_enabled=lambda: True, click=self.turn)

        def turn(self, **_):
            self.clicks += 1
            response = Response(
                ENTRY + "visible-list-result", b'{"page":2}', media="application/json"
            )
            self.context.handler(Route(response, navigation=False, resource_type="xhr"))
            self.dom = listing("b.html", extra="<span id='end'>末页</span>").decode()

    page = PagingPage(dom=listing(extra="<button id='next'>下一页</button>").decode())
    client = Client({})
    adapter = PublicAdapter("public_test", config(p, browser=True), client, page)
    assert adapter.discover(1)[1] == 2
    refs, next_page, _, media = adapter.discover(2)
    assert next_page is None and refs[0].url.endswith("b.html") and media == DOM_MEDIA_TYPE
    assert page.clicks == 1
    assert page.ready_args[1]["previous"] and "a.html" in page.ready_args[1]["previous"]
    assert adapter.last_original_responses == [
        (b'{"page":2}', ENTRY + "visible-list-result", "application/json", 200)
    ]
    assert client.before_calls == [ENTRY, ENTRY + "visible-list-result"]


def test_shanghai_listing_structure_with_comments_and_empty_attributes():
    fixture = PUBLIC_SOURCE_FIXTURES / "regression-shanghai-policy-listing.html"
    raw = fixture.read_bytes()
    p = profile()
    p["listing"] = {
        "container": "//ul[@class='infolist']",
        "items": "./li",
        "link": "./a",
        "title": "./a/@title",
        "date": "./span",
    }
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_variable": "pagecount",
    }
    entry = "https://shanghai.chinatax.gov.cn/zcfw/zxwj/"
    source = SourceConfig(
        name="上海政策文件",
        entry=entry,
        allowed_hosts=["shanghai.chinatax.gov.cn"],
        listing_date_kind="column_date",
        profile=p,
    )
    refs, next_page, _, _ = PublicAdapter("public_test", source, Client({entry: raw})).discover(1)
    assert refs and next_page == 2
    assert all(r.url.startswith("https://shanghai.chinatax.gov.cn/") for r in refs)


def test_comment_tails_and_empty_style_do_not_lose_visible_body_text():
    raw = "<h1>标题</h1><div id='body' style>甲<!--not-visible-->乙<span hidden>隐藏</span>丙</div>".encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert record.body_text == "甲乙丙"


def test_trs_total_variable_without_semicolon_is_observed_as_integer():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.htm",
        "total_pages_variable": "countPage",
    }
    raw = listing(extra="<script>var countPage = 11//共多少页\n</script>")
    assert PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)[1] == 2


def test_observed_http_links_only_upgrade_exact_allowed_host_without_http_request():
    raw = listing("http://official.example:80/policy/a.html")
    client = Client({ENTRY: raw, DETAIL: "<h1>标题</h1><div id='body'>正文。</div>".encode()})
    adapter = PublicAdapter("public_test", config(), client)
    refs, _, _, _ = adapter.discover(1)
    assert refs[0].url == DETAIL
    adapter.fetch_detail(refs[0].url)
    assert client.calls == [ENTRY, DETAIL]
    assert all(url.startswith("https://") for url in client.calls)
    bad_entry = config().model_copy(update={"entry": "http://official.example/policy/"})
    client = Client({})
    with pytest.raises(AccessBlocked):
        PublicAdapter("public_test", bad_entry, client).discover(1)
    assert client.calls == []
    for bad in (
        "http://evil.example/a",
        "http://official.example.evil/a",
        "http://u@official.example/a",
    ):
        with pytest.raises(AccessBlocked):
            PublicAdapter("public_test", config(), Client({ENTRY: listing(bad)})).discover(1)


def test_observed_attachment_http_upgrade_is_not_a_host_expansion():
    raw = b"<h1>Title</h1><div id='body'>Body<a href='http://official.example:80/a.doc'>Word</a><a href='http://evil.example/a.pdf'>PDF</a></div>"
    adapter = PublicAdapter("public_test", config(), Client({}))
    record = adapter.parse_detail(ref(), raw, digest(raw))
    assert [a.url for a in record.attachments] == ["https://official.example/a.doc"]
    assert any("未允许协议" in value for value in record.limitations)


def test_noncontent_resource_failures_are_blocked_and_audited_without_host_expansion():
    page = Page(
        responses=[
            Response(ENTRY, b"<html></html>"),
            Response(
                "https://analytics.example/a.js?token=not-logged", b"bad", resource_type="script"
            ),
            Response(ENTRY + "missing.png", b"404", status=404, resource_type="image"),
        ]
    )
    client = Client({})
    adapter = PublicAdapter("public_test", config(browser=True), client, page)
    assert adapter.discover(1)[0]
    assert client.before_calls == [ENTRY]
    assert page.routes[1].aborted and page.routes[2].aborted
    blocked = adapter.last_capture["blocked_resources"]
    assert len(blocked) == 2 and "token" not in str(blocked)
    assert [entry[1] for entry in adapter.last_original_responses] == [ENTRY]


@pytest.mark.parametrize("resource_type", ["document", "xhr", "fetch"])
def test_content_data_offhost_remains_fatal(resource_type):
    page = Page(
        responses=[
            Response(ENTRY, b"OK"),
            Response("https://evil.example/data", b"bad", resource_type=resource_type),
        ]
    )
    with pytest.raises(AccessBlocked):
        PublicAdapter("public_test", config(browser=True), Client({}), page).discover(1)


def test_noncontent_resource_budget_failure_is_fatal():
    client = Client({})

    def budget(url):
        if url != ENTRY:
            raise RuntimeError("budget reached")

    client.before_request = budget
    page = Page(
        responses=[
            Response(ENTRY, b"OK"),
            Response(ENTRY + "needed.css", b"css", resource_type="stylesheet"),
        ]
    )
    with pytest.raises(RuntimeError, match="budget reached"):
        PublicAdapter("public_test", config(browser=True), client, page).discover(1)


def test_child_existence_rule_skips_observed_separator_rows():
    p = profile()
    p["listing"]["items"] = "./li[a]"
    raw = listing().replace(b"</ul>", b"<li class='line1'></li><li class='ztitle'>Header</li></ul>")
    assert len(PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)[0]) == 1


def test_bounded_child_attribute_existence_keeps_table_row_date():
    p = profile()
    p["listing"] = {
        "container": "//tbody[@id='list']",
        "items": './tr[td[@class="date"]]',
        "link": "./td/a",
        "date": './td[@class="date"]',
    }
    raw = b'<table><tbody id="list"><tr><td><a href="a.html">Title</a></td><td class="date">2026-11-06</td></tr><tr><td>separator</td></tr></tbody></table>'
    refs, _, _, _ = PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)
    assert len(refs) == 1 and refs[0].listing_date == date(2026, 11, 6)


def test_browser_initial_click_selects_observed_column_before_parsing():
    p = profile()
    p["browser"] = {
        "initial_click": "//a[@id='orders']",
        "wait_for": "//*[@id='list']/li/a[contains(@href,'/orders/')]",
    }

    class FilterPage(Page):
        clicks = 0

        def locator(self, selector):
            assert selector == "xpath=//a[@id='orders']"
            return SimpleNamespace(
                wait_for=lambda **_: None,
                count=lambda: 1,
                is_enabled=lambda: True,
                click=self.select,
            )

        def select(self, **_):
            self.clicks += 1
            self.context.handler(
                Route(
                    Response(ENTRY + "visible-filter", b'{"column":"orders"}'),
                    navigation=False,
                    resource_type="xhr",
                )
            )
            self.dom = listing("/orders/a.html").decode()

    page = FilterPage()
    adapter = PublicAdapter("public_test", config(p, browser=True), Client({}), page)
    assert adapter.discover(1)[0][0].url == "https://official.example/orders/a.html"
    assert page.clicks == 1 and len(adapter.last_original_responses) == 2


def test_exact_visible_text_predicate_selects_paging_control_without_functions():
    p = profile()
    p["pagination"] = {
        "mode": "next_link",
        "next": '//div[@id="pager"]/a[.="下一页"]',
        "end_selector": '//div[@id="pager"]/span[.="下一页"][@class="disab"]',
    }
    raw = listing(
        extra='<div id="pager"><a href="index_1.html">下一页</a><a href="last.html">尾页</a></div>'
    )
    assert PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)[1] == 2
    p["pagination"]["next"] = '//a[normalize-space(.)="下一页"]'
    with pytest.raises(ValidationError):
        PublicProfile.model_validate(p)


@pytest.mark.parametrize("channel,total", [("policy", 10), ("interpretation", 48), ("qa", 30)])
def test_guangdong_article_pages_structure_and_terminal_ui(channel, total):
    base = PUBLIC_SOURCE_FIXTURES
    p = profile()
    p["listing"] = {
        "container": '//*[@id="ajaxlist"]',
        "items": './ul/li[@class="itm"]',
        "link": "./h4/a",
        "date": "./h4/span",
    }
    p["pagination"] = {
        "mode": "browser_next",
        "next": '//*[@id="ajaxpage"]/li[@class="next"]/a',
        "end_selector": '//*[@id="ajaxpage"]/li[@class="current"]/span',
        "total_pages_selector": '//*[@id="ajaxpage"]/li[@class="pagination_total"]/span',
        "total_pages_format": "article_pages",
    }
    source = config(p, browser=True).model_copy(
        update={
            "entry": "https://guangdong.chinatax.gov.cn/gdsw/",
            "allowed_hosts": ["guangdong.chinatax.gov.cn"],
        }
    )
    adapter = PublicAdapter("public_test", source, Client({}))
    first = (base / f"regression-guangdong-{channel}-synchronized-first.html").read_bytes()
    last = (base / f"regression-guangdong-{channel}-synchronized-last.html").read_bytes()
    refs, nxt = adapter._listing(first, source.entry, "text/html", 1)
    endrefs, end = adapter._listing(last, source.entry, "text/html", total)
    assert refs and nxt == 2 and endrefs and end is None
    assert [r.url for r in refs] != [r.url for r in endrefs]
    # A current-page marker alone cannot terminate a middle page.
    with pytest.raises(StructureDrift, match="末页"):
        adapter._listing(last, source.entry, "text/html", total - 1)


@pytest.mark.parametrize(
    "text",
    ["共150篇文章/共10页 trailing", "150/10", "共150篇文章/共0页", "共150篇文章/共10页;evil()"],
)
def test_article_pages_rejects_unobserved_or_executable_format(text):
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_selector": '//*[@id="pages"]',
        "total_pages_format": "article_pages",
    }
    with pytest.raises(StructureDrift, match="总页数"):
        PublicAdapter(
            "public_test",
            config(p),
            Client({ENTRY: listing(extra=f'<span id="pages">{text}</span>')}),
        ).discover(1)


@pytest.mark.parametrize(
    "control",
    [
        '<button onclick="download()">附件下载</button>',
        '<a href="javascript:download()">下载附件</a>',
    ],
)
def test_unresolved_attachment_controls_are_explicit_gaps(control):
    raw = f'<h1>Title</h1><div id="body">Body{control}</div>'.encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert record.attachments == [] and any("下载控件" in x for x in record.limitations)


def test_ordinary_attachment_mentions_do_not_claim_missing_files():
    raw = '<h1>Title</h1><div id="body">企业应按规定留存附件和其他业务资料。<input type="hidden" value="附件"></div>'.encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert not record.limitations


def test_explicit_attachment_declaration_without_file_or_inline_content_is_gap():
    raw = '<h1>Title</h1><div id="body"><p>通知正文。</p><p>附件：资金分配表</p></div>'.encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert any("明确列示附件" in x for x in record.limitations)


def test_inline_attachment_table_is_preserved_and_evidenced_without_false_gap():
    raw = '<h1>Title</h1><div id="body"><p>附件：分配表</p><table><tr><td>地区</td><td>金额</td></tr><tr><td>甲</td><td>100</td></tr></table></div>'.encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert "<table>" in record.body_text and not record.limitations
    assert record.field_evidence["inline_attachment_content"] == digest(raw)


def test_file_named_visible_label_discovers_extensionless_original():
    raw = '<h1>Title</h1><div id="body">正文<a href="/files?id=123">附件：清单.docx</a></div>'.encode()
    record = PublicAdapter("public_test", config(), Client({})).parse_detail(
        ref(), raw, digest(raw)
    )
    assert record.attachments[0].url == "https://official.example/files?id=123"
    assert not record.limitations


def test_image_only_detail_reports_content_gap_instead_of_complete_text():
    raw = b'<h1>Title</h1><div id="body"><img src="answer.png"></div>'
    with pytest.raises(StructureDrift, match="仅呈现图片"):
        PublicAdapter("public_test", config(), Client({})).parse_detail(ref(), raw, digest(raw))


def test_visible_current_total_requires_matching_request_page():
    p = profile()
    p["pagination"] = {
        "mode": "template",
        "template": "index_{index}.html",
        "total_pages_selector": "//*[@id='pages']",
        "total_pages_format": "current_total",
    }
    raw = listing(extra="<span id='pages'>1/109</span>")
    assert PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)[1] == 2
    raw = listing(extra="<span id='pages'>2/109</span>")
    with pytest.raises(StructureDrift, match="当前/总页数"):
        PublicAdapter("public_test", config(p), Client({ENTRY: raw})).discover(1)


def test_observed_query_pagination_replaces_only_existing_public_key():
    p = profile()
    p["pagination"] = {
        "mode": "query",
        "query_parameter": "PageIndex",
        "total_pages_selector": "//*[@id='pages']/@value",
    }
    entry = ENTRY + "?q=a%20b&PageIndex=1"
    source = config(p).model_copy(update={"entry": entry})
    second = ENTRY + "?q=a%20b&PageIndex=2"
    raw = listing(extra="<input id='pages' value='2'>")
    adapter = PublicAdapter("public_test", source, Client({entry: raw, second: raw}))
    assert adapter.discover(1)[1] == 2
    assert adapter.discover(2)[1] is None
    assert adapter.client.calls == [entry, second]
    for query in ("?q=a", "?PageIndex=1&PageIndex=1"):
        adapter = PublicAdapter(
            "public_test", config(p).model_copy(update={"entry": ENTRY + query}), Client({})
        )
        with pytest.raises(StructureDrift, match="唯一"):
            adapter.discover(2)
        assert adapter.client.calls == []


@pytest.mark.parametrize(
    "sid",
    [
        "mof_tfs_rules",
        "mof_asset_policy",
        "mof_environment_policy",
        "mof_accounting_policy",
        "csrc_interpretation",
        "moj_administrative_regulations",
        "mof_gks_rules",
        "mof_yss_policy",
        "mof_jrs_notices",
        "mof_jrs_interpretation",
        "mof_zhs_policy",
        "mof_jjs_policy",
        "mof_sbs_policy",
        "mof_nys_rules",
        "mof_tfs_interpretation",
    ],
)
def test_registered_central_profiles_parse_synthetic_structural_samples(sid):
    root = Path(__file__).resolve().parents[1]
    source = yaml.safe_load((root / "config/sources.yaml").read_text())["sources"][sid]
    manifest = json.loads((PUBLIC_SOURCE_FIXTURES / "manifest.json").read_text())
    assert manifest["kind"] == "synthetic_structure"
    sample = manifest["cases"][sid]
    listing_raw = (PUBLIC_SOURCE_FIXTURES / sample["listing"]).read_bytes()
    detail_raw = (PUBLIC_SOURCE_FIXTURES / sample["detail"]).read_bytes()
    assert digest(listing_raw) == sample["listing_sha256"]
    assert digest(detail_raw) == sample["detail_sha256"]
    list_url = source["entry"]
    detail_url = urljoin(list_url, sample["detail_href"])
    adapter = PublicAdapter(sid, SourceConfig.model_validate(source), Client({}))
    refs, next_page = adapter._listing(listing_raw, list_url, "text/html", 1)
    discovered = next(r for r in refs if r.url == detail_url)
    record = adapter.parse_detail(discovered, detail_raw, digest(detail_raw))
    assert refs and next_page == 2 and record.title and record.body_text
    assert record.title == sample["expected_title"]
    assert sample["expected_body_fragment"] in record.body_text
    assert discovered.listing_date == date.fromisoformat(sample["expected_listing_date"])
    published = sample["expected_published_date"]
    assert record.published_date == (date.fromisoformat(published) if published else None)
    assert len(record.attachments) == sample["expected_attachments"]
    # A column's explicit publication label never becomes its document issue date.
    assert record.issued_date is None
    if sid == "mof_jrs_notices":
        assert not record.attachments and not record.limitations
        assert record.field_evidence["inline_attachment_content"] == digest(detail_raw)
        assert "<table>" in record.body_text
        assert 'colspan="2"' in record.body_text and 'rowspan="2"' in record.body_text
    if sid == "moj_administrative_regulations":
        assert all(attachment.content_role == "primary" for attachment in record.attachments)
