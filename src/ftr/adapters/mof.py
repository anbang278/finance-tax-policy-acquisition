from __future__ import annotations

from urllib.parse import urljoin

from ftr.config import SourceConfig
from ftr.models import DiscoveredRef
from ftr.network import BoundedClient


class MofAdapter:
    source_id = "mof"

    def __init__(self, config: SourceConfig, client: BoundedClient):
        self.config = config
        self.client = client
        from ftr.rules import Rules

        self.rules = Rules(source_id="mof")
        self.last_response: tuple[bytes, str, str, int] | None = None

    def discover(self, page_number: int) -> tuple[list[DiscoveredRef], int | None, bytes, str]:
        if page_number < 1:
            raise ValueError("页码必须从 1 开始")
        page_url = (
            self.config.entry
            if page_number == 1
            else urljoin(self.config.entry, self.rules.page_template.format(index=page_number - 1))
        )
        self.last_request_url = page_url
        raw, final_url, media_type = self.client.get(page_url)
        self.last_response = (raw, final_url, media_type, page_number)
        from ftr.rules import extract_listing

        refs, next_page = extract_listing(self.rules, raw, final_url, page_number)
        return refs, next_page, raw, media_type
