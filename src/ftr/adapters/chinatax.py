from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from ftr.adapters.common import parse_date
from ftr.config import SourceConfig
from ftr.models import DiscoveredRef
from ftr.network import AccessBlocked, BrowserSessionClient, check_url

LIST_PATH = "/getFileListByCodeId"


class ChinataxAdapter:
    source_id = "chinatax"

    def __init__(
        self,
        config: SourceConfig,
        page: Page,
        client: BrowserSessionClient,
        *,
        timeout_seconds: float = 30,
    ):
        self.config = config
        self.page = page
        self.client = client
        self.list_url: str | None = None
        self.channel_id: str | None = None
        self.code_id = ""
        self.page_size = 10
        self.timeout_ms = timeout_seconds * 1000

    @staticmethod
    def normalize_link(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.hostname == "www.chinatax.gov.cn" and parsed.path.startswith("/zcfgk/"):
            return urlunsplit(("https", "fgk.chinatax.gov.cn", parsed.path, parsed.query, ""))
        return url

    def _bootstrap(self) -> None:
        try:
            with self.page.expect_response(
                lambda r: urlsplit(r.url).path == LIST_PATH and r.request.method == "POST",
                timeout=self.timeout_ms,
            ) as event:
                entry = self.page.goto(
                    self.config.entry, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
            response = event.value
        except PlaywrightTimeout as exc:
            raise AccessBlocked("税务列表请求未出现，可能存在访问限制") from exc
        except PlaywrightError as exc:
            raise RuntimeError("税务站浏览器导航失败") from exc
        if entry is None or entry.status in (401, 403, 429):
            raise AccessBlocked("税务栏目页面访问受限")
        if response.status in (401, 403, 429):
            raise AccessBlocked(f"税务列表访问受限：HTTP {response.status}")
        if response.status != 200:
            raise ValueError(f"税务列表响应异常：HTTP {response.status}")
        request = response.request
        fields = parse_qs(request.post_data or "", keep_blank_values=True)
        channel = fields.get("channelId", [""])[0]
        size = fields.get("size", [""])[0]
        if not re.fullmatch(r"[0-9a-zA-Z]{1,128}", channel) or not size.isdigit():
            raise ValueError("浏览器请求缺少可信栏目参数")
        self.page_size = int(size)
        if not 1 <= self.page_size <= 100:
            raise ValueError("税务列表页容量异常")
        self.code_id = fields.get("codeId", [""])[0]
        self.list_url = request.url
        check_url(self.list_url, self.config.allowed_hosts, getattr(self.client, "proxy", None))
        if urlsplit(self.list_url).path != LIST_PATH:
            raise AccessBlocked("浏览器列表接口路径不在允许范围")
        self.channel_id = channel
        self.client.set_browser_session(request.headers, self.page.context.cookies())

    def ensure_session(self) -> None:
        if self.list_url is None:
            self._bootstrap()

    def discover(self, page_number: int) -> tuple[list[DiscoveredRef], int | None, bytes, str]:
        if page_number < 1:
            raise ValueError("页码必须从 1 开始")
        self.ensure_session()
        assert self.list_url is not None and self.channel_id is not None
        raw, final_url, media_type = self.client.post_form(
            self.list_url,
            {
                "codeId": self.code_id,
                "channelId": self.channel_id,
                "page": str(page_number),
                "size": str(self.page_size),
            },
        )
        if final_url != self.list_url:
            raise AccessBlocked("税务列表接口发生非预期跳转")
        try:
            payload = json.loads(raw)
            data = payload["results"]["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("税务列表响应不是预期 JSON") from exc
        if payload.get("code") != 200 or not isinstance(data.get("results"), list):
            raise ValueError("税务列表响应结构异常")
        if data.get("channelId") and data["channelId"] != self.channel_id:
            raise ValueError("税务列表栏目参数与请求不一致")
        if int(data.get("page", page_number)) != page_number:
            raise ValueError("税务列表页码与请求不一致")
        refs = []
        for item in data["results"]:
            metadata = {
                part.get("key"): part.get("value")
                for group in item.get("domainMetaList", [])
                for part in group.get("resultList", [])
            }
            refs.append(
                DiscoveredRef(
                    source_id=self.source_id,
                    url=self.normalize_link(item.get("url", "")),
                    listing_title=item.get("titleHtml") or item.get("title") or "",
                    listing_date=parse_date(metadata.get("writtendate")),
                    listing_date_kind="issued_date",
                    document_number_hint=metadata.get("writtentext"),
                    source_type_hint=metadata.get("typename"),
                    discovered_from=self.config.entry,
                )
            )
        next_page = (
            page_number + 1
            if page_number * int(data.get("rows", self.page_size)) < int(data.get("total", 0))
            else None
        )
        return refs, next_page, raw, media_type
