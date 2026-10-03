"""从已校验原件恢复展示结构，不改变存储正文。"""

from __future__ import annotations

import re

from lxml import etree, html

from ftr.adapters.common import text_of
from ftr.content_layout import extract_blocks


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def recover_body(raw: bytes, record: dict) -> dict:
    fallback = {"mode": "plain", "blocks": [], "reason": "STRUCTURE_UNAVAILABLE"}
    try:
        tree = html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8"))
    except (etree.ParserError, ValueError):
        return fallback
    candidates = tree.xpath(
        "//*[contains(@class,'TRS_Editor') or contains(@class,'article-content') "
        "or contains(@class,'arc_cont') or contains(@class,'articleCon') "
        "or contains(@class,'content-main') or @id='zoom' or @id='fontzoom']"
    )
    expected = compact(record.get("body_text", ""))
    if not expected:
        return fallback
    for root in candidates:
        if compact(text_of(root)) != expected or any(
            text_of(table) for table in root.xpath(".//table")
        ):
            continue
        blocks = extract_blocks(root)
        if not blocks or compact("".join(b["text"] for b in blocks)) != expected:
            continue
        if len(blocks) == 1 and "\n" not in blocks[0]["text"]:
            continue
        # 标题已经由详情头部展示，只移除完整独立且完全相同的标题块。
        if blocks[0]["text"].strip() == record.get("title", "").strip():
            blocks = blocks[1:]
        return {"mode": "structured", "blocks": blocks, "reason": None}
    return fallback
