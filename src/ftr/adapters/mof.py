from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html

from ftr.adapters.common import parse_date, text_of
from ftr.config import SourceConfig
from ftr.models import DiscoveredRef
from ftr.network import BoundedClient


class MofAdapter:
    source_id = "mof"

    def __init__(self, config: SourceConfig, client: BoundedClient):
        self.config = config
        self.client = client

    def discover(self, page_number: int) -> tuple[list[DiscoveredRef], int | None, bytes, str]:
        if page_number < 1:
            raise ValueError("页码必须从 1 开始")
        page_url = (
            self.config.entry
            if page_number == 1
            else urljoin(self.config.entry, f"index_{page_number - 1}.htm")
        )
        raw, final_url, media_type = self.client.get(page_url)
        tree = html.fromstring(raw)
        refs = []
        for link in tree.xpath("//a[@href]"):
            url = urljoin(final_url, link.get("href") or "")
            parsed = urlsplit(url)
            if parsed.scheme == "http" and parsed.hostname in self.config.allowed_hosts:
                url = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
            if not re.search(r"/t20\d{6}_\d+\.htm(?:\?.*)?$", url):
                continue
            title = text_of(link)
            if not title:
                continue
            parent = link.getparent()
            parent_text = text_of(parent) if parent is not None else ""
            refs.append(
                DiscoveredRef(
                    source_id=self.source_id,
                    url=url,
                    listing_title=title,
                    listing_date=parse_date(parent_text),
                    listing_date_kind="column_date",
                    discovered_from=final_url,
                )
            )
        if not refs:
            raise ValueError("财政部列表未找到资料，可能发生页面变化")
        next_url = urljoin(self.config.entry, f"index_{page_number}.htm")
        count_match = re.search(r"var\s+countPage\s*=\s*(\d+)", raw.decode("utf-8", "replace"))
        if not count_match:
            raise ValueError("财政部列表缺少可验证的总页数")
        next_page = page_number + 1 if page_number < int(count_match.group(1)) else None
        if next_page and next_url == final_url:
            raise ValueError("财政部分页重复")
        return refs, next_page, raw, media_type
