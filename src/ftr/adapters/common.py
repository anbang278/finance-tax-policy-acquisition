from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml import html

from ftr.content_layout import body_text_of
from ftr.models import Attachment, DiscoveredRef, DocumentRecord, DocumentType, digest

DATE_PATTERN = re.compile(r"(20\d{2})[年./-]\s*(\d{1,2})[月./-]\s*(\d{1,2})")


def parse_date(value: str | None) -> date | None:
    match = DATE_PATTERN.search(value or "")
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def text_of(node) -> str:
    visible = node.xpath(".//text()[not(ancestor::style or ancestor::script)]")
    return re.sub(r"\s+", " ", " ".join(str(part) for part in visible)).strip()


def canonical_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, parsed.query, ""))


def classify(title: str, source_hint: str | None, body: str, source_id: str) -> DocumentType:
    if source_hint == "解读" or "解读" in title or "答记者问" in title:
        return DocumentType.OFFICIAL_INTERPRETATION
    if "公告" in title and (source_hint == "文件" or "公告20" in body or "第" in title):
        return DocumentType.POLICY_ANNOUNCEMENT
    if source_hint == "文件":
        return DocumentType.POLICY_FILE
    if "印发" in title and len(body) >= 500 and "第一条" in body:
        return DocumentType.POLICY_FILE
    if source_id == "mof" and ("印发" in title or "明确" in title or "出台" in title):
        return DocumentType.RELEASE_MESSAGE
    if "通知" in title or "办法" in title or "准则" in title:
        return DocumentType.POLICY_FILE
    return DocumentType.OTHER


def parse_detail(ref: DiscoveredRef, raw: bytes, evidence_id: str, rules=None) -> DocumentRecord:
    if rules is not None:
        from ftr.rules import bounded_tree

        tree = bounded_tree(raw)
    else:
        tree = html.fromstring(raw, parser=html.HTMLParser(encoding="utf-8"))
    if ref.source_id == "chinatax":
        heads = tree.xpath("//div[contains(@class,'detials')]//h3[1]") or tree.xpath("//h3")
        title = text_of(heads[0]) if heads else ref.listing_title
        body_nodes = tree.xpath(
            "//div[contains(@class,'arc_cont')] | //*[contains(@class,'article-content') or contains(@class,'articleCon') or contains(@class,'content-main') or @id='zoom' or @id='fontzoom']"
        )
        if not body_nodes:
            body_nodes = tree.xpath("//div[contains(@class,'zw') or contains(@class,'content')]//p")
        body = body_text_of(body_nodes[0]) if body_nodes else ""
        status_nodes = tree.xpath("//div[contains(@class,'arctips')]")
        status_match = re.search(
            r"尚未生效|现行有效|已失效|已废止",
            text_of(status_nodes[0])[:250] if status_nodes else "",
        )
        status_text = status_match.group(0) if status_match else ""
        published = None
        issued_match = re.search(
            r"成文日期[：:]\s*(20\d{2}[-./年]\d{1,2}[-./月]\d{1,2})",
            text_of(status_nodes[0])[:250] if status_nodes else "",
        )
        issued = parse_date(issued_match.group(1)) if issued_match else ref.listing_date
    else:
        heads = tree.xpath("//h1 | //h2")
        candidates = [
            text_of(node)
            for node in heads
            if text_of(node) and "中华人民共和国财政部" not in text_of(node)
        ]
        title = candidates[-1] if candidates else ref.listing_title
        body_nodes = tree.xpath(
            "//*[contains(@class,'TRS_Editor') or contains(@class,'article-content') or @id='zoom']"
        )
        if not body_nodes:
            body_nodes = tree.xpath("//div[contains(@class,'content')]//p")
        body = body_text_of(body_nodes[0]) if body_nodes else ""
        status_text = ""
        issued = None
        published = None
    if rules is not None:
        from ftr.rules import nodes

        if rules.titles:
            heads = nodes(tree, rules.titles)
            title = text_of(heads[0]) if heads else ""
        if rules.bodies:
            body_nodes = nodes(tree, rules.bodies)
            body = body_text_of(body_nodes[0]) if body_nodes else ""
    if not body:
        # 只保存可核验的页面文本，并保持隔离状态；不会误称正文完整。
        body = ""
    attachments: list[Attachment] = []
    related: list[str] = []
    for link in tree.xpath("//a[@href]"):
        href = link.get("href") or ""
        full = urljoin(ref.url, href)
        parsed_link = urlsplit(full)
        if parsed_link.scheme == "http":
            full = urlunsplit(
                ("https", parsed_link.netloc, parsed_link.path, parsed_link.query, "")
            )
        if not full.startswith("https://"):
            continue
        label = text_of(link)
        if re.search(r"\.(pdf|docx?|xlsx?)(?:\?|$)", full, re.IGNORECASE):
            primary = ref.source_id == "chinatax" and bool(
                link.xpath("ancestor::div[contains(@class,'arc_cont')]")
            )
            attachments.append(
                Attachment(
                    url=full,
                    label=label,
                    content_role="primary" if primary else "supplement",
                )
            )
        elif ("/content.html" in full or "/t20" in full) and full != ref.url:
            related.append(full)
    number_match = re.search(
        r"(?:财会|财税|国税发|国家税务总局公告|财政部\s*税务总局公告)[^。\n]{0,45}(?:号)",
        body[:1200] + " " + title,
    )
    number = ref.document_number_hint or (number_match.group(0).strip() if number_match else None)
    if not body and attachments:
        attachments[0].content_role = "primary"
    record = DocumentRecord(
        source_id=ref.source_id,
        source_url=ref.url,
        canonical_url=canonical_url(ref.url),
        listing_title=ref.listing_title,
        title=title,
        document_type=classify(title, ref.source_type_hint, body, ref.source_id),
        listing_date=ref.listing_date,
        listing_date_kind=ref.listing_date_kind,
        issued_date=issued,
        published_date=published,
        document_number=number,
        source_status_claim=status_text or None,
        parser_version="0.1.4",
        body_text=body,
        body_sha256=digest(body.encode()),
        evidence_id=evidence_id,
        attachments=attachments,
        related_urls=sorted(set(related)),
        field_evidence={
            "title": evidence_id,
            "body_text": evidence_id,
            "listing_date": ref.discovered_from,
        },
    )
    if not body:
        record.limitations.append("正文区域未可靠定位")
    if record.document_type == DocumentType.UNKNOWN:
        record.limitations.append("资料类型待确认")
    return record
