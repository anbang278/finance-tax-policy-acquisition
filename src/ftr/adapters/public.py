"""Declarative adapters for individually registered, public official columns.

This module neither grants hosts trust nor downloads attachments. HTTP uses the
supplied bounded client. Browser contexts must be private to this source and
created with service_workers='block'; the runtime attests this on the client.
"""

from __future__ import annotations

import copy
import html as html_escape
import re
import time
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from lxml import etree, html
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ftr.adapters.common import DATE_PATTERN, classify, parse_date, text_of
from ftr.config import SourceConfig
from ftr.content_layout import body_text_of
from ftr.models import Attachment, DiscoveredRef, DocumentRecord, DocumentType, digest
from ftr.network import AccessBlocked, TransientFailure, check_byte_budget, check_url
from ftr.rules import StructureDrift

DOM_MEDIA_TYPE = "application/vnd.ftr.browser-dom+html; charset=utf-8"
MAX_HTML_BYTES = 2_000_000
MAX_NODES = 20_000
MAX_MATCHES = 2_000
MAX_PAGE = 10_000
_FILES = re.compile(
    r"\.(?:pdf|docx?|wps|wpt|xlsx?|xlsm|et|ett|csv|zip|rar|7z|ofd|rtf|pptx?|odt)$", re.IGNORECASE
)
_ERRORS = (
    "您访问的页面不存在",
    "页面不存在或已删除",
    "访问过于频繁",
    "访问被拒绝",
    "请输入验证码",
    "请完成安全验证",
    "系统维护中",
    "系统升级维护",
    "access denied",
    "request blocked",
    "service unavailable",
)


class _BrowserLimitExceeded(AccessBlocked):
    """A byte/capture limit is fatal, including for a non-content resource."""


def _selector(value: str | None) -> str | None:
    if value is None:
        return value
    if len(value) > 500:
        raise ValueError("选择器过长")
    # XPath is data, not an extension/eval language. No unions, axes, parent
    # traversal, variables, arbitrary functions or externally supplied JS.
    atom = r"(?:[A-Za-z][A-Za-z0-9_-]*|\*)"
    attr = r"[A-Za-z_][A-Za-z0-9_:-]*"
    quoted = r"(?:'[^'\[\]<>]{1,100}'|\"[^\"\[\]<>]{1,100}\")"
    child = rf"[A-Za-z][A-Za-z0-9_-]*(?:\[@{attr}={quoted}\])?"
    pred = rf"\[(?:[1-9][0-9]{{0,3}}|{child}|\.={quoted}|@{attr}(?:={quoted})?|contains\(@{attr},{quoted}\))\]"
    step = rf"{atom}(?:{pred}){{0,3}}"
    grammar = rf"(?:\.?//|\./){step}(?:/{{1,2}}{step})*(?:/@{attr})?"
    if value != "." and not re.fullmatch(grammar, value):
        raise ValueError("选择器必须使用受限 XPath 路径、属性及 contains 谓词")
    etree.XPath(value)
    return value


class _ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublicListing(_ProfileModel):
    container: str
    items: str
    link: str = ".//a[@href]"
    title: str | None = None
    date: str | None = None
    date_attribute: str | None = None
    empty_selector: str | None = None
    empty_text: list[str] = Field(
        default_factory=lambda: ["暂无数据", "暂无记录", "没有找到相关结果"]
    )

    _paths = field_validator("container", "items", "link", "title", "date", "empty_selector")(
        _selector
    )

    @field_validator("items", "link", "title", "date")
    @classmethod
    def relative(cls, value):
        if value is not None and value != "." and not value.startswith("./"):
            raise ValueError("条目、链接、标题及日期须相对于登记容器定位")
        return value

    @field_validator("date_attribute")
    @classmethod
    def attribute(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_:-]{0,63}", value):
            raise ValueError("非法日期属性")
        return value

    @field_validator("empty_text")
    @classmethod
    def empty_labels(cls, values):
        if len(values) > 10 or any(not 2 <= len(x) <= 100 for x in values):
            raise ValueError("空列表提示必须是有限、明确的短语")
        return values


class PublicPagination(_ProfileModel):
    mode: Literal["none", "next_link", "template", "query", "browser_next"]
    next: str | None = None
    template: str | None = None
    query_parameter: str | None = None
    total_pages_variable: str | None = None
    total_pages_function: str | None = None
    total_pages_argument: int | None = Field(default=None, ge=0, le=9)
    total_pages_selector: str | None = None
    total_pages_format: Literal["integer", "current_total", "article_pages"] = "integer"
    end_selector: str | None = None

    _paths = field_validator("next", "total_pages_selector", "end_selector")(_selector)

    @field_validator("template")
    @classmethod
    def page_template(cls, value):
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{0,40}\{(?:index|page)\}[A-Za-z0-9_.-]{0,25}", value
        ):
            raise ValueError("分页模板只允许文件名和单个 {index} 或 {page}")
        return value

    @field_validator("total_pages_variable", "total_pages_function", "query_parameter")
    @classmethod
    def total_name(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
            raise ValueError("非法总页数字段")
        return value

    @model_validator(mode="after")
    def pagination_evidence(self):
        if self.total_pages_format != "integer" and not self.total_pages_selector:
            raise ValueError("复合总页数格式须有明确页码节点")
        if (self.total_pages_function is None) != (self.total_pages_argument is None):
            raise ValueError("总页数函数名与零起始参数序号须同时提供")
        if self.mode in ("template", "query") and not (
            (self.template if self.mode == "template" else self.query_parameter)
            and (
                self.total_pages_variable or self.total_pages_selector or self.total_pages_function
            )
        ):
            raise ValueError("模板/公开参数分页必须有定位值及明确总页数依据")
        if self.mode in ("next_link", "browser_next") and not self.next:
            raise ValueError("下一页模式必须有可观察的下一页选择器")
        if self.mode == "browser_next" and not self.end_selector:
            raise ValueError("浏览器点击分页必须有明确末页标志")
        return self


class PublicDetail(_ProfileModel):
    title: str
    body: str
    published_date: str | None = None
    issued_date: str | None = None
    document_number: str | None = None
    source_publisher: str | None = None
    issuing_authorities: str | None = None
    applicable_regions: str | None = None
    stage: str | None = None
    attachments: list[str] = Field(default_factory=list, max_length=12)
    primary_attachments: list[str] = Field(default_factory=list, max_length=8)
    related_links: list[str] = Field(default_factory=list, max_length=8)

    _paths = field_validator(
        "title",
        "body",
        "published_date",
        "issued_date",
        "document_number",
        "source_publisher",
        "issuing_authorities",
        "applicable_regions",
        "stage",
    )(_selector)

    @field_validator("attachments", "primary_attachments", "related_links")
    @classmethod
    def selectors(cls, values):
        return [_selector(value) for value in values]


class PublicBrowser(_ProfileModel):
    wait_for: str | None = None
    iframe: str | None = None
    initial_click: str | None = None
    detail_wait_for: str | None = None
    detail_iframe: str | None = None

    _paths = field_validator(
        "wait_for", "iframe", "initial_click", "detail_wait_for", "detail_iframe"
    )(_selector)


class PublicProfile(_ProfileModel):
    schema_version: Literal["1"] = "1"
    listing: PublicListing
    pagination: PublicPagination
    detail: PublicDetail
    browser: PublicBrowser = Field(default_factory=PublicBrowser)


def _visible(node) -> bool:
    if not isinstance(node, etree._Element):
        return True
    if not isinstance(node.tag, str):
        return False
    for item in (node, *node.iterancestors()):
        if not isinstance(item.tag, str):
            continue
        style = re.sub(r"\s+", "", (item.get("style") or "").lower())
        if "hidden" in item.attrib or "display:none" in style or "visibility:hidden" in style:
            return False
    return True


def _nodes(tree, path: str | None):
    if path is None:
        return []
    if not isinstance(tree, etree._Element) or not isinstance(tree.tag, str):
        raise StructureDrift("选择器上下文不是HTML元素")
    result = tree.xpath(path)
    if not isinstance(result, list):
        raise StructureDrift("选择器没有返回节点列表")
    if len(result) > MAX_MATCHES:
        raise StructureDrift("匹配节点超过上限")
    return [node for node in result if _visible(node)]


def _value(node) -> str:
    return text_of(node) if isinstance(node, etree._Element) else str(node).strip()


def _field(tree, path: str | None) -> str:
    return "\n".join(dict.fromkeys(_value(node) for node in _nodes(tree, path) if _value(node)))


def _visible_text(node) -> str:
    values = []
    for value in node.xpath(
        ".//text()[not(ancestor::script or ancestor::style or ancestor::noscript)]"
    ):
        parent = value.getparent()
        if value.is_tail and parent is not None and not isinstance(parent.tag, str):
            parent = parent.getparent()
        if _visible(parent) and str(value).strip():
            values.append(str(value).strip())
    return " ".join(values)


def _tree(raw: bytes, media_type=""):
    if not raw or len(raw) > MAX_HTML_BYTES:
        raise StructureDrift("HTML为空或超过2MB解析上限")
    match = re.search(r"charset\s*=\s*['\"]?([a-z0-9-]+)", media_type, re.IGNORECASE)
    encoding = match.group(1) if match else "utf-8"
    if match is None:
        byte_match = re.search(rb"charset\s*=\s*['\"]?([a-zA-Z0-9-]+)", raw[:4096], re.IGNORECASE)
        if byte_match is not None:
            encoding = byte_match.group(1).decode("ascii")
    if encoding.lower() not in ("utf-8", "utf8", "gb2312", "gbk", "gb18030", "big5"):
        raise StructureDrift("未支持的HTML字符编码")
    try:
        tree = html.fromstring(raw, parser=html.HTMLParser(encoding=encoding, no_network=True))
    except (etree.ParserError, ValueError) as exc:
        raise StructureDrift("HTML结构无法解析") from exc
    if sum(1 for _ in tree.iter()) > MAX_NODES:
        raise StructureDrift("HTML节点超过上限")
    visible = _visible_text(tree).lower()
    if len(visible) < 3000 and any(marker in visible for marker in _ERRORS):
        raise StructureDrift("页面为异常、软404或访问验证提示，不能记为空列表")
    return tree


def _one_date(value: str | None):
    matches = {match.group(0) for match in DATE_PATTERN.finditer(value or "")}
    dates = {parse_date(match) for match in matches}
    dates.discard(None)
    return dates.pop() if len(dates) == 1 else None


def _label_value(value: str) -> str:
    return re.sub(r"^(?:来源|信息来源|发布机构|发文机关|适用地区|文号)[：:]\s*", "", value).strip()


def _attachment_label(value: str) -> bool:
    return bool(
        re.search(
            r"^(?:附件|附表|附录)(?:[0-9一二三四五六七八九十]*)?(?:[：:\s、.]|下载|$)|^(?:下载附件|附件下载|下载\s*(?:PDF|Word|WPS|Excel)|(?:PDF|Word|WPS|Excel)\s*下载)",
            value,
            re.IGNORECASE,
        )
        or _FILES.search(value)
    )


def _table_html(table) -> str:
    allowed = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col"}

    def render(node):
        if not isinstance(node.tag, str) or node.tag.lower() not in allowed:
            return html_escape.escape(text_of(node))
        tag = node.tag.lower()
        attrs = "".join(
            f' {key}="{node.get(key)}"'
            for key in ("rowspan", "colspan", "span")
            if re.fullmatch(r"[1-9][0-9]{0,3}", node.get(key) or "")
        )
        content = html_escape.escape(node.text or "")
        for child in node:
            content += render(child) + html_escape.escape(child.tail or "")
        return f"<{tag}{attrs}>{content}</{tag}>"

    return render(table)


def _body(node) -> str:
    # Keep rows/cells and merged-cell semantics rather than flattening a table
    # into an ambiguous paragraph. Original HTML remains in the raw evidence.
    clone = copy.deepcopy(node)
    for hidden in list(clone.iterdescendants()):
        if hidden.getparent() is not None and not _visible(hidden):
            parent, previous = hidden.getparent(), hidden.getprevious()
            if hidden.tail:
                if previous is None:
                    parent.text = (parent.text or "") + hidden.tail
                else:
                    previous.tail = (previous.tail or "") + hidden.tail
            parent.remove(hidden)
    tables = [clone] if clone.tag == "table" else clone.xpath(".//table[not(ancestor::table)]")
    if clone.tag == "table":
        return _table_html(clone)
    for table in tables:
        replacement = etree.Element("pre")
        replacement.text = _table_html(table)
        replacement.tail = table.tail
        table.getparent().replace(table, replacement)
    return body_text_of(clone)


# Fixed, read-only readiness polling; profile values are passed as XPath data.
_READY = """({ready, empty, container, empty_text, previous}) => {
 const visible = (xp) => {
   if (!xp) return false;
   const n = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
   if (!n || n.nodeType !== 1) return false;
   const s = getComputedStyle(n);
   return s.display !== 'none' && s.visibility !== 'hidden' && n.getClientRects().length > 0;
 };
 const list = container && document.evaluate(container, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
 // Playwright polling can represent an absent Python None as undefined.
 // Only an actual prior-list string enables the pagination-change guard.
 if (typeof previous === 'string' && (!list || list.innerHTML === previous)) return false;
 if (visible(ready) || visible(empty)) return true;
 return list && visible(container) && empty_text.some(t => list.textContent.includes(t));
}"""


class PublicAdapter:
    def __init__(self, source_id: str, config: SourceConfig, client, page=None, timeout_seconds=30):
        self.source_id, self.config, self.client = source_id, config, client
        self.page: Any = page
        try:
            self.profile = PublicProfile.model_validate(config.profile)
        except ValidationError as exc:
            raise StructureDrift("来源profile未完整登记或含不允许的规则，禁止全页兜底") from exc
        self.browser = config.adapter == "public_browser"
        if self.profile.pagination.mode == "browser_next" and not self.browser:
            raise ValueError("browser_next仅允许public_browser")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("浏览器超时须在0至300秒之间")
        self.timeout_ms = timeout_seconds * 1000
        self.last_request_url = config.entry
        self.last_response: tuple[bytes, str, str, int] | None = None
        self.last_original_responses: list[tuple[bytes, str, str, int]] = []
        self.last_capture: dict = {}
        self._page_urls = {1: config.entry}
        self._final_urls: dict[str, str] = {}
        self._media_types: dict[str, str] = {}
        self._browser_page_number = 0
        self._browser_error: Exception | None = None
        self._guard_installed = False
        self._frame: Any = None
        self._document_responses: list[tuple[bytes, str, str, int]] = []
        self._capture_bytes = 0
        self._browser_guard_wait_seconds = 0.0
        self._readiness_timing: dict = {}
        self.blocked_resources: list[dict[str, str]] = []

    def _url(self, url: str) -> str:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or p.hostname not in self.config.allowed_hosts
            or p.username is not None
            or p.password is not None
            or p.port not in (None, 443)
        ):
            raise AccessBlocked("公开来源链接的协议、精确主机或端口未获许可")
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, p.fragment))

    def _document_url(self, url: str) -> str:
        """Normalize observed links only; configured entries are never upgraded."""
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise AccessBlocked("公开文档链接端口非法") from exc
        if (
            parsed.scheme == "http"
            and parsed.hostname in self.config.allowed_hosts
            and parsed.username is None
            and parsed.password is None
            and port in (None, 80)
        ):
            assert parsed.hostname is not None
            url = urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, parsed.fragment))
        return self._url(url)

    def _network_url(self, url):
        self._url(url)
        check_url(url, self.config.allowed_hosts, getattr(self.client, "proxy", None))

    def _before_browser(self, url):
        started = time.monotonic()
        try:
            if hasattr(self.client, "_before"):
                self.client._before(url)
            else:
                checkpoint = getattr(self.client, "checkpoint", None)
                before = getattr(self.client, "before_request", None)
                if checkpoint is not None:
                    checkpoint()
                if before is not None:
                    before(url)
        finally:
            self._browser_guard_wait_seconds += time.monotonic() - started

    def _wait_ready(self, args):
        """Keep the DOM's active wait budget separate from mandatory host throttles.

        Sync route callbacks consume Playwright's wall-clock timeout while obeying
        the owner's _before hook. Retry only the DOM predicate with that measured
        time returned; never navigate, re-fetch or reset task/request/byte budgets.
        """
        started = time.monotonic()
        guard_started = self._browser_guard_wait_seconds
        attempts = 0
        try:
            while True:
                if self._browser_error is not None:
                    raise self._browser_error
                checkpoint = getattr(self.client, "checkpoint", None)
                if checkpoint is not None:
                    checkpoint()
                guard_wait = self._browser_guard_wait_seconds - guard_started
                active_elapsed = max(0.0, time.monotonic() - started - guard_wait)
                remaining_ms = self.timeout_ms - active_elapsed * 1000
                if remaining_ms <= 0:
                    raise PlaywrightTimeout("DOM主动等待预算耗尽，未将限流时间作为内容就绪时间")
                attempts += 1
                try:
                    self._frame.wait_for_function(_READY, arg=args, timeout=remaining_ms)
                    return
                except PlaywrightTimeout:
                    # Recompute after callbacks; a retry spends only the active
                    # budget left, not another fixed timeout.
                    guard_wait = self._browser_guard_wait_seconds - guard_started
                    if time.monotonic() - started - guard_wait >= self.timeout_ms / 1000:
                        raise
        finally:
            elapsed = time.monotonic() - started
            guard_wait = self._browser_guard_wait_seconds - guard_started
            self._readiness_timing = {
                "wall_seconds": round(elapsed, 6),
                "guard_wait_seconds": round(guard_wait, 6),
                "active_seconds": round(max(0.0, elapsed - guard_wait), 6),
                "active_timeout_seconds": self.timeout_ms / 1000,
                "predicate_wait_attempts": attempts,
            }

    def _install_guard(self):
        if self._guard_installed:
            return
        if (
            self.page is None
            or getattr(self.client, "public_browser_service_workers_blocked", False) is not True
        ):
            raise AccessBlocked("public_browser需要独占page及service_workers=block的受信运行时标记")
        self.page.context.route("**/*", self._route)
        # Routed WebSockets stay local unless connect_to_server() is called.
        if not hasattr(self.page.context, "route_web_socket"):
            self.page.context.unroute("**/*", self._route)
            raise AccessBlocked("浏览器缺少WebSocket阻断能力")
        self.page.context.route_web_socket("**/*", self._block_websocket)
        self._guard_installed = True

    def _block_websocket(self, ws):
        # WebSocketRoute handlers run in the event-loop callback. Calling the
        # synchronous close() here can re-enter _sync and deadlock. Its default
        # unconnected local sink neither handshakes upstream nor forwards data.
        # url is a cached property, not a synchronous Playwright operation.
        self._record_blocked_resource(
            ws, "WEBSOCKET_UPSTREAM_NOT_CONNECTED", resource_type="websocket"
        )

    def _record_blocked_resource(self, request, reason, *, resource_type=None):
        parsed = urlsplit(request.url)
        self.blocked_resources.append(
            {
                "url": urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", "")),
                "resource_type": resource_type or getattr(request, "resource_type", "unknown"),
                "reason": reason,
            }
        )

    @staticmethod
    def _abort_route(route):
        # Navigation timeout/context shutdown can finish a pending route first.
        # Never issue a fallback request or continue an untrusted route.
        try:
            route.abort("blockedbyclient")
        except PlaywrightError:
            pass

    def _route(self, route):
        request = route.request
        content_request = request.is_navigation_request() or getattr(
            request, "resource_type", ""
        ) in ("document", "xhr", "fetch")
        phase, status = "trust", None
        try:
            self._network_url(request.url)
            if not content_request and getattr(request, "resource_type", "") in (
                "image",
                "font",
                "media",
            ):
                self._record_blocked_resource(request, "NON_CONTENT_RESOURCE_POLICY")
                self._abort_route(route)
                return
            phase = "budget"
            self._before_browser(request.url)
            phase = "response"
            # route.continue_ can follow an unguarded redirect (Playwright's
            # documented behavior). Never follow redirects inside interception.
            response = route.fetch(max_redirects=0, max_retries=0, timeout=self.timeout_ms)
            status = response.status
            raw = response.body()
            if len(raw) > getattr(self.client, "max_bytes", 50_000_000):
                raise _BrowserLimitExceeded("浏览器响应超过单文件上限")
            media = response.headers.get("content-type") or ""
            if request.is_navigation_request() or getattr(request, "resource_type", "") in (
                "xhr",
                "fetch",
            ):
                self._capture_bytes += len(raw)
                if self._capture_bytes > getattr(self.client, "max_bytes", 50_000_000):
                    raise _BrowserLimitExceeded("浏览器文档及数据原响应合计超过捕获上限")
                self._document_responses.append((raw, response.url, media, status))
                self.last_response = (raw, response.url, media, 0)
                if len(self._document_responses) > 64:
                    raise _BrowserLimitExceeded("浏览器文档响应超过有界捕获数量")
            if 300 <= status < 400:
                raise AccessBlocked("浏览器HTTP重定向停止，须核验并登记真实最终入口")
            if status in (401, 403, 429):
                raise AccessBlocked(f"浏览器来源限制访问，HTTP {status}")
            if status >= 500:
                raise TransientFailure(f"浏览器来源响应异常，HTTP {status}")
            if status >= 400:
                raise StructureDrift(f"浏览器来源响应异常，HTTP {status}")
            check_byte_budget(
                max(len(raw), self._capture_bytes), getattr(self.client, "remaining_bytes", None)
            )
            route.fulfill(response=response)
        except Exception as exc:  # noqa: BLE001 - route callbacks must abort and rethrow owner budgets.
            if (
                not content_request
                and phase != "budget"
                and isinstance(
                    exc, (AccessBlocked, TransientFailure, StructureDrift, PlaywrightError)
                )
                and not isinstance(exc, _BrowserLimitExceeded)
            ):
                self._record_blocked_resource(
                    request,
                    f"HTTP_{status}"
                    if status is not None and status >= 300
                    else type(exc).__name__,
                )
            elif self._browser_error is None:
                self._browser_error = exc
            self._abort_route(route)

    def close(self):
        """Detach this source's HTTP route; runtime closes its private context."""
        if self._guard_installed:
            self.page.context.unroute("**/*", self._route)
            self._guard_installed = False

    def _frame_for(self, *, listing):
        frame = self.page
        iframe = self.profile.browser.iframe if listing else self.profile.browser.detail_iframe
        if iframe:
            locator = self.page.locator("xpath=" + iframe)
            locator.wait_for(state="visible", timeout=self.timeout_ms)
            if locator.count() != 1:
                raise StructureDrift("已登记正文iframe不是唯一可见节点")
            handle = locator.element_handle()
            frame = handle.content_frame() if handle else None
            if frame is None:
                raise StructureDrift("已登记iframe未加载")
        self._network_url(frame.url)
        return frame

    def _browser_read(self, url, *, listing: bool, click=False):
        self.blocked_resources = []
        self.last_capture = {
            "capture_kind": "browser_attempt",
            "requested_url": url,
            "capture_complete": False,
            "blocked_resources": self.blocked_resources,
        }
        self._install_guard()
        self._network_url(url)
        if self._browser_error is not None:
            raise self._browser_error
        self._document_responses = []
        self._capture_bytes = 0
        self._browser_guard_wait_seconds = 0.0
        self._readiness_timing = {}
        try:
            response = None
            previous = None
            if click:
                assert self._frame is not None and self.profile.pagination.next is not None
                old_list = self._frame.locator("xpath=" + self.profile.listing.container)
                if old_list.count() != 1:
                    raise StructureDrift("翻页前列表容器不可唯一定位")
                previous = old_list.inner_html(timeout=self.timeout_ms)
                if len(previous.encode("utf-8")) > MAX_HTML_BYTES:
                    raise AccessBlocked("翻页前列表快照超过上限")
                target = self._frame.locator("xpath=" + self.profile.pagination.next)
                if target.count() != 1 or not target.is_enabled():
                    raise StructureDrift("下一页控件不可唯一操作")
                target.click(timeout=self.timeout_ms)
            else:
                response = self.page.goto(url, wait_until="commit", timeout=self.timeout_ms)
                if response is None:
                    raise StructureDrift("浏览器导航未产生可存证响应")
            if self._browser_error is not None:
                raise self._browser_error
            self._frame = self._frame_for(listing=listing)
            if listing and not click and self.profile.browser.initial_click:
                target = self._frame.locator("xpath=" + self.profile.browser.initial_click)
                target.wait_for(state="visible", timeout=self.timeout_ms)
                if target.count() != 1 or not target.is_enabled():
                    raise StructureDrift("已登记栏目筛选控件不可唯一操作")
                target.click(timeout=self.timeout_ms)
            listing_path = self.profile.listing.container
            items_path = self.profile.listing.items
            if items_path != ".":
                listing_path += items_path[1:]
            ready = (
                (self.profile.browser.wait_for or listing_path)
                if listing
                else (self.profile.browser.detail_wait_for or self.profile.detail.body)
            )
            self._wait_ready(
                {
                    "ready": ready,
                    "empty": self.profile.listing.empty_selector if listing else None,
                    "container": self.profile.listing.container if listing else None,
                    "empty_text": self.profile.listing.empty_text if listing else [],
                    "previous": previous,
                }
            )
            if self._browser_error is not None:
                raise self._browser_error
            document_url = self._frame.url
            self._network_url(document_url)
            snapshot = self._frame.content().encode("utf-8")
            tree = _tree(snapshot)
            head = tree.find("head")
            if head is None:
                head = etree.Element("head")
                tree.insert(0, head)
            marker = etree.SubElement(head, "meta", name="ftr-capture-kind", content="browser_dom")
            marker.set("data-requested-url", url)
            bases = tree.xpath("//base")
            for base in bases:
                base.getparent().remove(base)
            etree.SubElement(head, "base", href=document_url)
            raw = etree.tostring(tree, encoding="utf-8", method="html")
            self.last_capture = {
                "capture_kind": "browser_dom",
                "requested_url": url,
                "navigation_url": self.page.url,
                "document_url": document_url,
                "media_type": DOM_MEDIA_TYPE,
                "original_response_count": len(self.last_original_responses)
                + len(self._document_responses),
                "blocked_resources": self.blocked_resources,
                "capture_complete": True,
                "readiness_wait": self._readiness_timing,
            }
            return raw, document_url, DOM_MEDIA_TYPE
        except (PlaywrightTimeout, PlaywrightError) as exc:
            if self._browser_error is not None:
                raise self._browser_error from exc
            raise StructureDrift("浏览器导航或可见内容加载未完成，不能当作空列表") from exc
        finally:
            self.last_original_responses.extend(self._document_responses)
            if self._readiness_timing:
                self.last_capture["readiness_wait"] = self._readiness_timing

    def _read(self, url, *, listing, click=False):
        self.last_request_url = url
        self.last_response = None
        if self.browser:
            return self._browser_read(url, listing=listing, click=click)
        self.last_capture = {
            "capture_kind": "http_attempt",
            "requested_url": url,
            "capture_complete": False,
        }
        self._network_url(url)
        raw, final, media = self.client.get(url)
        self._network_url(final)
        self.last_original_responses.append((raw, final, media, 200))
        self.last_capture = {
            "capture_kind": "http_response",
            "requested_url": url,
            "navigation_url": final,
            "document_url": final,
            "media_type": media,
            "original_response_count": len(self.last_original_responses),
            "capture_complete": True,
        }
        return raw, final, media

    def _base(self, tree, url):
        bases = _nodes(tree, "//base[@href]")
        return self._document_url(urljoin(url, bases[0].get("href") or "")) if bases else url

    def _total_pages(self, tree, raw, page_number):
        pagination = self.profile.pagination
        values = []
        if pagination.total_pages_selector:
            value = _field(tree, pagination.total_pages_selector)
            if pagination.total_pages_format == "current_total":
                pair = re.fullmatch(r"([1-9][0-9]{0,4})\s*/\s*([1-9][0-9]{0,4})", value)
                if pair is None or int(pair.group(1)) != page_number:
                    raise StructureDrift("可见当前/总页数格式不明或与请求页码不一致")
                value = pair.group(2)
            elif pagination.total_pages_format == "article_pages":
                pair = re.fullmatch(r"共([0-9]{1,9})篇文章/共([1-9][0-9]{0,4})页", value)
                if pair is None:
                    raise StructureDrift("可见文章/总页数字串格式不明确")
                value = pair.group(2)
            values.append(value)
        if pagination.total_pages_variable:
            matches = re.findall(
                r"\b(?:var|let|const)\s+"
                + re.escape(pagination.total_pages_variable)
                + r"\s*=\s*(\d+)\s*(?=;|,|//|\r?\n|</script>|$)",
                raw.decode("utf-8", "replace"),
            )
            if len(set(matches)) != 1:
                raise StructureDrift("明确总页数变量未可靠定位")
            values.append(matches[0])
        if pagination.total_pages_function:
            assert pagination.total_pages_argument is not None
            calls = re.findall(
                r"\b" + re.escape(pagination.total_pages_function) + r"\s*\(([^()]{1,2000})\)",
                raw.decode("utf-8", "replace"),
            )
            extracted = []
            token = re.compile(
                r"""\s*('[^'\\]*'|"[^"\\]*"|[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\s*(,|$)"""
            )
            for call in calls:
                args, cursor = [], 0
                while cursor < len(call):
                    match = token.match(call, cursor)
                    if match is None:
                        raise StructureDrift("登记总页数函数参数不是受限字面值")
                    args.append(match.group(1))
                    cursor = match.end()
                if len(args) <= pagination.total_pages_argument:
                    raise StructureDrift("登记总页数函数参数缺失")
                extracted.append(args[pagination.total_pages_argument])
            if len(set(extracted)) != 1:
                raise StructureDrift("明确总页数函数未可靠定位")
            values.append(extracted[0])
        if not values:
            return None
        if len(set(values)) != 1:
            raise StructureDrift("多个总页数依据不一致")
        value = values[0]
        if not re.fullmatch(r"\d{1,5}", value) or int(value) > MAX_PAGE:
            raise StructureDrift("总页数不明确或超过上限")
        return int(value)

    def _listing(self, raw, url, media, number):
        tree = _tree(raw, media)
        listing, pagination = self.profile.listing, self.profile.pagination
        containers = _nodes(tree, listing.container)
        if len(containers) != 1:
            raise StructureDrift("登记列表容器未唯一定位，可能是JS空壳或结构变化")
        container = containers[0]
        items = _nodes(container, listing.items)
        empty_nodes = _nodes(tree, listing.empty_selector)
        empty_confirmed = any(_value(node) for node in empty_nodes) or any(
            marker in _visible_text(container) for marker in listing.empty_text
        )
        if not items and not empty_confirmed:
            raise StructureDrift("列表无条目且没有明确空结果证据")
        if items and empty_confirmed:
            raise StructureDrift("列表条目与空结果提示冲突")
        total = self._total_pages(tree, raw, number)
        if total is not None and (total < number or total == 0) and items:
            raise StructureDrift("条目与总页数不一致")
        if not items:
            if number != 1 or (total is not None and total != 0):
                raise StructureDrift("非初始空列表或空列表总页数冲突")
            return [], None
        base = self._base(tree, url)
        refs, seen = [], set()
        for item in items:
            links = _nodes(item, listing.link)
            if len(links) != 1 or not isinstance(links[0], etree._Element):
                raise StructureDrift("列表条目链接未唯一定位")
            link = links[0]
            href = link.get("href") or ""
            if not href or href.startswith(("#", "javascript:")):
                raise StructureDrift("列表条目不是可核验的公开URL")
            full = self._document_url(urljoin(base, href))
            title = (
                _field(item, listing.title)
                if listing.title
                else (text_of(link) or link.get("title", ""))
            )
            if not title or "{{" in title:
                raise StructureDrift("列表标题为空或仍为JS模板")
            date_nodes = _nodes(item, listing.date)
            date_text = "\n".join(
                node.get(listing.date_attribute, "")
                if listing.date_attribute and isinstance(node, etree._Element)
                else _value(node)
                for node in date_nodes
            )
            if full in seen:
                continue
            seen.add(full)
            refs.append(
                DiscoveredRef(
                    source_id=self.source_id,
                    url=full,
                    listing_title=title,
                    listing_date=_one_date(date_text),
                    listing_date_kind=self.config.listing_date_kind,
                    discovered_from=url,
                )
            )
        if pagination.mode == "none":
            return refs, None
        if total is not None:
            if number == total:
                return refs, None
            if number > total:
                raise StructureDrift("页码超出明确总页数")
        if pagination.mode in ("template", "query"):
            if total is None:
                raise StructureDrift("模板分页缺少可信总页数")
            return refs, number + 1
        next_nodes = _nodes(tree, pagination.next)
        if not next_nodes:
            if _nodes(tree, pagination.end_selector) and (total is None or number == total):
                return refs, None
            raise StructureDrift("下一页及可信末页标志均未定位")
        if len(next_nodes) != 1:
            raise StructureDrift("下一页控件不唯一")
        if pagination.mode == "next_link":
            target = self._document_url(urljoin(base, next_nodes[0].get("href") or ""))
            if any(
                target == known for page, known in self._page_urls.items() if page != number + 1
            ):
                raise StructureDrift("下一页URL重复或形成循环")
            self._page_urls[number + 1] = target
        return refs, number + 1

    def discover(self, page_number):
        if not 1 <= page_number <= MAX_PAGE:
            raise ValueError("页码必须从1开始且不超过10000")
        self.last_original_responses = []
        pagination = self.profile.pagination
        if pagination.mode == "none" and page_number != 1:
            raise StructureDrift("单页栏目不接受后续页码")
        if pagination.mode in ("next_link", "browser_next"):
            # Resume by observing real previous links/UI; never invent a URL.
            start = self._browser_page_number + 1 if pagination.mode == "browser_next" else 1
            if pagination.mode == "browser_next" and page_number < start:
                self._browser_page_number, start = 0, 1
            for current in range(start, page_number + 1):
                if (
                    pagination.mode == "next_link"
                    and current < page_number
                    and current + 1 in self._page_urls
                ):
                    continue
                url = self._page_urls.get(current, self.config.entry)
                raw, final, media = self._read(
                    url, listing=True, click=pagination.mode == "browser_next" and current > 1
                )
                self.last_response = (raw, final, media, current)
                refs, next_page = self._listing(raw, final, media, current)
                if pagination.mode == "browser_next":
                    self._browser_page_number = current
                if current < page_number and next_page is None:
                    raise StructureDrift("已到末页，无法恢复所请求页码")
            return refs, next_page, raw, media
        url = self.config.entry
        if page_number != 1:
            if pagination.mode == "query":
                assert pagination.query_parameter is not None
                parsed = urlsplit(url)
                parts = parsed.query.split("&")
                matching = [
                    i
                    for i, part in enumerate(parts)
                    if unquote(part.split("=", 1)[0]) == pagination.query_parameter
                ]
                if len(matching) != 1:
                    raise StructureDrift("入口未含唯一的已登记公开分页参数")
                parts[matching[0]] = pagination.query_parameter + "=" + str(page_number)
                url = urlunsplit(
                    (parsed.scheme, parsed.netloc, parsed.path, "&".join(parts), parsed.fragment)
                )
            else:
                assert pagination.template is not None
                url = urljoin(
                    self.config.entry,
                    pagination.template.format(page=page_number, index=page_number - 1),
                )
        raw, final, media = self._read(url, listing=True)
        self.last_response = (raw, final, media, page_number)
        refs, next_page = self._listing(raw, final, media, page_number)
        return refs, next_page, raw, media

    def fetch_detail(self, url):
        self.last_original_responses = []
        raw, final, media = self._read(url, listing=False)
        self.last_response = (raw, final, media, 0)
        self._final_urls[url], self._media_types[url] = final, media
        return raw, final, media

    def parse_detail(self, ref, raw, evidence_id):
        self._url(ref.url)
        if ref.source_id != self.source_id:
            raise ValueError("详情来源与适配器身份不一致")
        final = self._final_urls.get(ref.url, ref.url)
        tree = _tree(raw, self._media_types.get(ref.url, ""))
        detail = self.profile.detail
        titles = _nodes(tree, detail.title)
        title = _value(titles[0]) if len(titles) == 1 else ""
        bodies = _nodes(tree, detail.body)
        if not title or len(bodies) != 1 or "{{" in title:
            raise StructureDrift("登记正文或标题未可靠定位")
        body = _body(bodies[0])
        if not body and bodies[0].xpath(".//img[@src]"):
            raise StructureDrift("正文仅呈现图片，尚未登记原始图片/OCR内容，不得作为完整文本")
        if not body or "{{" in body:
            raise StructureDrift("正文为空或仍为JS模板")
        base = self._base(tree, final)
        attachments, limitations = [], []
        links = list(bodies[0].xpath(".//a[@href]"))
        controls = list(bodies[0].xpath(".//a | .//button | .//input"))
        primary = set()
        for path in detail.primary_attachments:
            nodes = _nodes(tree, path)
            links.extend(nodes)
            controls.extend(nodes)
            primary.update(nodes)
        for path in detail.attachments:
            nodes = _nodes(tree, path)
            links.extend(nodes)
            controls.extend(nodes)
        seen, registered_controls = set(), set()
        for link in links:
            if not isinstance(link, etree._Element) or not link.get("href"):
                continue
            full = urljoin(base, link.get("href"))
            parsed = urlsplit(full)
            label = text_of(link) or link.get("title") or ""
            decoded = unquote(parsed.path)
            query = unquote(parsed.query)
            is_file = (
                _FILES.search(decoded)
                or re.search(r"\.(?:pdf|docx?|wps|xlsx?|zip)(?:&|$)", query, re.IGNORECASE)
                or _FILES.search(label)
            )
            is_download = link.get("download") is not None or bool(
                re.search(
                    r"(?:^|/)(?:download|downfile|filedownload)(?:[./?]|$)",
                    parsed.path,
                    re.IGNORECASE,
                )
            )
            if not (is_file or is_download or link in primary or _attachment_label(label)):
                continue
            if parsed.scheme == "http" and parsed.hostname in self.config.allowed_hosts:
                try:
                    full = self._document_url(full)
                    parsed = urlsplit(full)
                except AccessBlocked:
                    limitations.append("附件HTTP链接不能安全规范为已允许的HTTPS文档地址")
                    continue
            if (
                parsed.scheme != "https"
                or parsed.username is not None
                or parsed.password is not None
            ):
                limitations.append("附件链接含未允许协议或凭据，未纳入下载队列")
                continue
            if full in seen:
                registered_controls.add(link)
                continue
            seen.add(full)
            registered_controls.add(link)
            blocked = parsed.hostname not in self.config.allowed_hosts or parsed.port not in (
                None,
                443,
            )
            attachments.append(
                Attachment(
                    url=full,
                    label=label,
                    content_role="primary" if link in primary else "unknown",
                    download_state="blocked" if blocked else "pending",
                )
            )
            if blocked:
                limitations.append("附件精确主机尚未获许可，原件下载待核")
        for control in set(controls):
            if not isinstance(control, etree._Element) or not _visible(control):
                continue
            if control.tag == "input" and (control.get("type") or "").lower() == "hidden":
                continue
            label = (
                _visible_text(control).strip() or control.get("value") or control.get("title") or ""
            )
            if (
                _attachment_label(label) or control in primary
            ) and control not in registered_controls:
                limitations.append(
                    "可见附件下载控件未获得可核验的原件URL，可能使用按钮或JS链接，附件完整性待核"
                )
        declarations = list(
            re.finditer(r"(?m)^\s*附件[0-9一二三四五六七八九十]*[：:]\s*[^\n]{0,200}$", body)
        )
        inline_attachment = bool(
            declarations
            and (
                any(body.find("<table", marker.end()) >= 0 for marker in declarations)
                or (len(declarations) > 1 and len(body[declarations[-1].end() :].strip()) >= 100)
            )
        )
        if declarations and not attachments and not inline_attachment:
            limitations.append(
                "正文明确列示附件，但未发现下载链接或可确认的内联附件内容，附件完整性待核"
            )
        related = set()
        for path in detail.related_links:
            for link in _nodes(tree, path):
                if not isinstance(link, etree._Element) or not link.get("href"):
                    continue
                try:
                    full = self._document_url(urljoin(base, link.get("href")))
                except AccessBlocked:
                    continue
                if full != ref.url:
                    related.add(full)
        fields = {
            key: _field(tree, getattr(detail, key))
            for key in (
                "published_date",
                "issued_date",
                "document_number",
                "source_publisher",
                "issuing_authorities",
                "applicable_regions",
                "stage",
            )
        }
        stage = "unknown"
        if re.search(r"征求意见|草案", title) or fields["stage"] in ("草案", "征求意见稿"):
            stage = "draft"
        elif fields["stage"] == "正式" or re.search(r"现予公布|现予印发|现印发", body):
            stage = "formal"
        doc_type = classify(title, ref.source_type_hint, body, self.source_id)
        if (
            doc_type == DocumentType.OTHER
            and self.config.channel == "normative"
            and stage == "formal"
        ):
            doc_type = DocumentType.POLICY_FILE
        if self.config.channel == "qa" and (
            re.search(r"问答|答复|咨询", title) or re.search(r"问[：:].*答[：:]", body, re.DOTALL)
        ):
            doc_type = DocumentType.OFFICIAL_QA
        # An interpretation column can also cross-list a normative file.
        elif self.config.channel == "interpretation" and re.search(
            r"解读|答记者问|发布.*及.*公告", title
        ):
            doc_type = DocumentType.OFFICIAL_INTERPRETATION
        evidence = {
            "title": evidence_id,
            "body_text": evidence_id,
            "listing_date": ref.discovered_from,
        }
        if stage != "unknown":
            evidence["content_stage"] = evidence_id
        if inline_attachment:
            evidence["inline_attachment_content"] = evidence_id
        for key, value in fields.items():
            if value:
                evidence[key] = evidence_id
        if self.config.categories:
            evidence["categories"] = "source_registry:" + self.config.registry_version
        split_values = lambda value: [
            x.strip() for x in re.split(r"[、，,；;\n]+", _label_value(value)) if x.strip()
        ]
        record = DocumentRecord(
            source_id=self.source_id,
            source_url=ref.url,
            canonical_url=urlunsplit((*urlsplit(final)[:4], "")),
            listing_title=ref.listing_title,
            title=title,
            document_type=doc_type,
            source_publisher=_label_value(fields["source_publisher"]),
            issuing_authorities=split_values(fields["issuing_authorities"]),
            applicable_regions=split_values(fields["applicable_regions"]),
            content_stage=stage,
            categories=list(self.config.categories),
            listing_date=ref.listing_date,
            listing_date_kind=ref.listing_date_kind,
            issued_date=_one_date(fields["issued_date"]),
            published_date=_one_date(fields["published_date"]),
            document_number=_label_value(fields["document_number"]) or None,
            body_text=body,
            body_sha256=digest(body.encode()),
            evidence_id=evidence_id,
            attachments=attachments,
            related_urls=sorted(related),
            parser_version="public-html-v1",
            field_evidence=evidence,
            limitations=limitations,
        )
        if tree.xpath("//meta[@name='ftr-capture-kind'][@content='browser_dom']"):
            record.collection_notes.append("正文取自浏览器DOM快照；原始导航响应须分别留存")
            if self.blocked_resources:
                record.collection_notes.append(
                    f"已阻断{len(self.blocked_resources)}个非内容资源，未扩大允许主机；正文完整性仍待复核"
                )
        return record
