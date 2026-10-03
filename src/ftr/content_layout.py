"""共享正文结构提取：只保留文字与原始块边界，不执行页面内容。"""

from __future__ import annotations

import re

BLOCKS = frozenset(
    (
        "p",
        "div",
        "section",
        "article",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "pre",
    )
)
IGNORED = frozenset(("script", "style", "noscript"))


def extract_blocks(root) -> list[dict]:
    blocks: list[dict] = []

    def walk(node, align="left"):
        if not isinstance(node.tag, str) or node.tag in IGNORED:
            return
        style = node.get("style", "")
        match = re.search(
            r"(?:^|;)\s*text-align\s*:\s*(center|right|left)\s*(?:;|$)", style, re.IGNORECASE
        )
        own_align = match.group(1).lower() if match else node.get("align", "").lower()
        if own_align in ("center", "right", "left"):
            align = own_align
        buffer = [node.text or ""]

        def flush():
            value = re.sub(r"[ \t\r\f\v]+", " ", "".join(buffer)).strip()
            buffer.clear()
            if value:
                blocks.append(
                    {
                        "type": "heading"
                        if node.tag in ("h1", "h2", "h3", "h4", "h5", "h6")
                        else "paragraph",
                        "text": value,
                        "align": align,
                    }
                )

        def inline(child):
            if not isinstance(child.tag, str) or child.tag in IGNORED:
                return
            if child.tag in BLOCKS:
                flush()
                walk(child, align)
                return
            if child.tag == "br":
                buffer.append("\n")
                return
            buffer.append(child.text or "")
            for sub in child:
                inline(sub)
                buffer.append(sub.tail or "")

        for child in node:
            if child.tag in BLOCKS:
                flush()
                walk(child, align)
            else:
                if node.tag == "tr" and child.tag in ("td", "th") and buffer:
                    buffer.append(" ")
                inline(child)
            buffer.append(child.tail or "")
        flush()

    walk(root)
    return blocks


def body_text_of(node) -> str:
    return "\n\n".join(block["text"] for block in extract_blocks(node))
