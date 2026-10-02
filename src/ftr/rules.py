"""Bounded, declarative extraction. No candidate Python or XPath functions execute."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ftr.adapters.common import parse_date, text_of
from ftr.models import DiscoveredRef, digest

MAX_INPUT = 2_000_000


class StructureDrift(ValueError):
    pass


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: str = "1"
    source_id: str
    links: list[str] = Field(default_factory=lambda: ["//a[@href]"], max_length=8)
    titles: list[str] = Field(default_factory=list, max_length=8)
    bodies: list[str] = Field(default_factory=list, max_length=8)
    total_variable: str = "countPage"
    page_template: str = "index_{index}.htm"
    data_path: str = "results.data"
    items_key: str = "results"
    title_keys: list[str] = Field(default_factory=lambda: ["titleHtml", "title"], max_length=4)
    url_key: str = "url"
    date_key: str = "writtendate"
    number_key: str = "writtentext"
    type_key: str = "typename"
    total_key: str = "total"
    rows_key: str = "rows"
    page_key: str = "page"
    date_format: str = "source"

    @field_validator("schema_version")
    @classmethod
    def schema(cls, value):
        if value != "1":
            raise ValueError("不支持的规则版本")
        return value

    @field_validator("source_id")
    @classmethod
    def source(cls, value):
        if value not in ("mof", "chinatax"):
            raise ValueError("未知来源")
        return value

    @field_validator("links", "titles", "bodies")
    @classmethod
    def selectors(cls, values):
        # A deliberately small XPath grammar: descendant tag, optional id/class/href predicate.
        grammar = r"//(?:[a-z][a-z0-9]*|\*)(?:\[(?:@href|@(?:id|class)='[A-Za-z0-9_ -]{1,80}'|contains\(@class,'[A-Za-z0-9_ -]{1,80}'\))\])?"
        if any(not re.fullmatch(grammar, value) for value in values):
            raise ValueError("选择器超出受限语法")
        return values

    @field_validator(
        "total_variable",
        "items_key",
        "url_key",
        "date_key",
        "number_key",
        "type_key",
        "total_key",
        "rows_key",
        "page_key",
    )
    @classmethod
    def keys(cls, value):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
            raise ValueError("非法字段名")
        return value

    @field_validator("title_keys")
    @classmethod
    def title_fields(cls, values):
        if not values:
            raise ValueError("标题字段不能为空")
        return [cls.keys(value) for value in values]

    @field_validator("data_path")
    @classmethod
    def path(cls, value):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){0,5}", value):
            raise ValueError("非法 JSON 字段路径")
        return value

    @field_validator("page_template")
    @classmethod
    def template(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9_-]{0,40}\{index\}[A-Za-z0-9_.-]{0,20}", value):
            raise ValueError("分页模板只能包含文件名与 {index}")
        return value

    @field_validator("date_format")
    @classmethod
    def dates(cls, value):
        if value not in ("source", "%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
            raise ValueError("日期格式未预定义")
        return value

    @property
    def version(self):
        return digest(self.model_dump_json().encode())


def read_rules(path: Path) -> Rules:
    if path.is_symlink() or path.stat().st_size > 32_768:
        raise ValueError("规则文件过大或为符号链接")
    return Rules.model_validate_json(path.read_bytes())


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.resolve().is_relative_to(path.parent.resolve()):
        raise ValueError("状态路径越界")
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(value, ensure_ascii=False).encode())
        handle.flush()
        os.fsync(handle.fileno())
        temp = Path(handle.name)
    os.replace(temp, path)


def active_state(root: Path, source: str) -> dict:
    Rules(source_id=source)
    path = root / "rules" / f"{source}.json"
    if path.is_symlink():
        raise ValueError("活动规则路径不得为符号链接")
    return json.loads(path.read_text()) if path.exists() else {}


def active_rules(root: Path, source: str) -> Rules:
    state = active_state(root, source)
    if not state:
        return Rules(source_id=source)
    value = Rules.model_validate(state["rules"])
    if value.source_id != source or value.version != state["version"]:
        raise ValueError("活动规则哈希或来源不匹配")
    return value


def bounded_tree(raw: bytes):
    if len(raw) > MAX_INPUT:
        raise StructureDrift("规则输入超过 2MB 上限")
    tree = html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8"))
    if sum(1 for _ in tree.iter()) > 20_000:
        raise StructureDrift("规则输入节点超过上限")
    return tree


def nodes(tree, selectors: list[str]):
    for selector in selectors:
        result = tree.xpath(selector)
        if result:
            if len(result) > 2000:
                raise StructureDrift("规则匹配节点超过上限")
            return result
    return []


def rule_date(value, rules: Rules):
    if rules.date_format == "source":
        return parse_date(value)
    try:
        return datetime.strptime(value or "", rules.date_format).replace(tzinfo=UTC).date()
    except ValueError:
        return None


def extract_listing(rules: Rules, raw: bytes, url: str, page: int, page_size: int = 10):
    if not 1 <= page <= 10_000 or len(raw) > MAX_INPUT:
        raise StructureDrift("分页或输入超过规则上限")
    refs = []
    if rules.source_id == "mof":
        tree = bounded_tree(raw)
        for link in nodes(tree, rules.links):
            full = urljoin(url, link.get("href") or "")
            parsed = urlsplit(full)
            if parsed.scheme == "http":
                full = urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
            if not re.search(r"/t20\d{6}_\d+\.htm(?:\?.*)?$", full):
                continue
            title = text_of(link)
            if title:
                refs.append(
                    DiscoveredRef(
                        source_id="mof",
                        url=full,
                        listing_title=title,
                        listing_date=rule_date(text_of(link.getparent()), rules),
                        listing_date_kind="column_date",
                        discovered_from=url,
                    )
                )
        match = re.search(
            r"var\s+" + re.escape(rules.total_variable) + r"\s*=\s*(\d+)",
            raw.decode("utf-8", "replace"),
        )
        if not match:
            match = re.search(r"var\s+countPage\s*=\s*(\d+)", raw.decode("utf-8", "replace"))
        if not refs or not match:
            raise StructureDrift("财政部列表条目或可信总页数未定位")
        total = int(match.group(1))
        if not 1 <= total <= 10_000 or page > total:
            raise StructureDrift("总页数异常")
        return refs, page + 1 if page < total else None
    try:
        payload = json.loads(raw)
        if payload.get("code") != 200:
            raise StructureDrift("税务响应状态异常")
        data = payload
        try:
            for key in rules.data_path.split("."):
                data = data[key]
        except KeyError:
            data = payload["results"]["data"]
        items = data.get(rules.items_key, data.get("results"))
        total, rows = (
            int(data.get(rules.total_key, data.get("total"))),
            int(data.get(rules.rows_key, data.get("rows", page_size))),
        )
        if not isinstance(items, list) or len(items) > 100 or not 1 <= rows <= 100:
            raise StructureDrift("税务条目或页容量异常")
        if (
            not 0 <= total <= 1_000_000
            or int(data.get(rules.page_key, data.get("page", page))) != page
        ):
            raise StructureDrift("税务总数或页码异常")
        if not items and page * rows < total:
            raise StructureDrift("非终页为空")
        for item in items:
            metadata = {
                part.get("key"): part.get("value")
                for group in item.get("domainMetaList", [])
                for part in group.get("resultList", [])
            }
            full = item.get(rules.url_key, item.get("url", ""))
            parsed = urlsplit(full)
            if parsed.hostname == "www.chinatax.gov.cn" and parsed.path.startswith("/zcfgk/"):
                full = urlunsplit(("https", "fgk.chinatax.gov.cn", parsed.path, parsed.query, ""))
            refs.append(
                DiscoveredRef(
                    source_id="chinatax",
                    url=full,
                    listing_title=next(
                        (item.get(key) for key in rules.title_keys if item.get(key)), ""
                    ),
                    listing_date=rule_date(
                        metadata.get(rules.date_key, metadata.get("writtendate")), rules
                    ),
                    listing_date_kind="issued_date",
                    document_number_hint=metadata.get(
                        rules.number_key, metadata.get("writtentext")
                    ),
                    source_type_hint=metadata.get(rules.type_key, metadata.get("typename")),
                    discovered_from=url,
                )
            )
        if any(not ref.listing_title or not ref.url for ref in refs):
            raise StructureDrift("标题或链接缺失")
        return refs, page + 1 if page * rows < total else None
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise StructureDrift("税务列表结构变化") from exc
