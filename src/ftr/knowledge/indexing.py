from __future__ import annotations

import io
import json

from pypdf import PdfReader

from ftr.knowledge.models import Anchor, KnowledgeError
from ftr.models import digest


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return digest(encoded(value).encode())


def read_evidence(repo, eid):
    from ftr.backup import _checked_file

    ev = repo.get_evidence(eid)
    path = _checked_file(repo.path.parent, "evidence/" + ev.relative_path)
    raw = path.read_bytes()
    if digest(raw) != ev.sha256:
        raise KnowledgeError("EVIDENCE_INVALID", "原件哈希不匹配")
    return raw


def segments(repo, record):
    """Never reinterpret old text silently. PDF page joins must equal stored text."""
    parts = [("body", record.evidence_id, record.body_text, None)]
    for a in record.attachments:
        if a.extraction_state != "text" or not a.evidence_id or not a.text:
            continue
        raw = read_evidence(repo, a.evidence_id)
        if raw.startswith(b"%PDF"):
            try:
                pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages]
            except Exception as exc:
                raise KnowledgeError("PARSING_FAILED", "PDF 页码提取失败；保留原版本") from exc
            if "\n".join(pages) != a.text:
                raise KnowledgeError(
                    "EXTRACTION_CHANGED", "PDF 重解析与历史文本不一致；须创建新提取版本并复核"
                )
            parts.extend(("attachment", a.evidence_id, t, i + 1) for i, t in enumerate(pages))
        else:
            parts.append(("attachment", a.evidence_id, a.text, None))
    result = []
    for part, eid, text, page in parts:
        if not text:
            continue
        # Paragraph slices retain exact source text. Offsets are local Unicode code points.
        for index in range(0, len(text), 4000):
            t = text[index : index + 4000]
            value = {
                "record_id": record.record_id,
                "source_part": part,
                "evidence_id": eid,
                "parent_evidence_id": record.evidence_id,
                "text_version_digest": digest(text.encode()),
                "page_number": page,
                "text": t,
                "part_offset": index,
                "parser_version": record.parser_version,
            }
            value["segment_id"] = fingerprint(value)
            result.append(value)
    return result


def check_anchor(repo, value, *, allowed=None):
    anchor = Anchor.model_validate(value)
    row = repo.db.execute(
        "SELECT data_json FROM kw_segments WHERE id=?", (anchor.segment_id,)
    ).fetchone()
    if not row:
        raise KnowledgeError("CITATION_MISMATCH", "引用片段不存在")
    s = json.loads(row[0])
    if allowed is not None and anchor.segment_id not in allowed:
        raise KnowledgeError("CITATION_NOT_IN_INPUT", "引用不在冻结输入集合中")
    for key in ("record_id", "evidence_id", "source_part", "text_version_digest", "page_number"):
        if getattr(anchor, key) != s[key]:
            raise KnowledgeError("CITATION_MISMATCH", "引用位置或附件不匹配")
    if s["text"][
        anchor.start_offset : anchor.end_offset
    ] != anchor.quote or anchor.end_offset > len(s["text"]):
        raise KnowledgeError("CITATION_MISMATCH", "摘录与码点位置不匹配")
    record = repo.get_document(anchor.record_id)
    ids = [record.evidence_id, *[a.evidence_id for a in record.attachments]]
    if anchor.evidence_id not in ids:
        raise KnowledgeError("CITATION_MISMATCH", "证据不属于资料版本")
    read_evidence(repo, anchor.evidence_id)
    return s


def prepare_index(root):
    """Copy database facts briefly, then parse originals without any DB/write lock."""
    from contextlib import closing
    from types import SimpleNamespace

    from ftr.repository import Repository

    with closing(Repository(root, readonly=True)) as repo:
        records = [
            repo.get_document(r[0]) for r in repo.db.execute("SELECT id FROM documents").fetchall()
        ]
        evidence = {
            r[0]: repo.get_evidence(r[0])
            for r in repo.db.execute("SELECT id FROM evidence").fetchall()
        }
    snapshot = SimpleNamespace(path=root / "database.sqlite3", get_evidence=lambda id: evidence[id])
    values = []
    for record in records:
        try:
            parts = segments(snapshot, record)
            error = None
        except (KnowledgeError, OSError, KeyError, ValueError) as exc:
            parts = []
            error = {
                "code": getattr(exc, "code", "EVIDENCE_UNAVAILABLE"),
                "message": "原件或历史提取结果需核查",
            }
        values.append(
            {
                "record_id": record.record_id,
                "fingerprint": fingerprint(record.model_dump(mode="json")),
                "title": record.title,
                "document_number": record.document_number,
                "segments": parts,
                "error": error,
            }
        )
    return values


def prepare_refresh(root, record_id):
    from contextlib import closing

    from ftr.models import new_id
    from ftr.repository import Repository

    with closing(Repository(root, readonly=True)) as repo:
        record = repo.get_document(record_id)
        baseline = fingerprint(record.model_dump(mode="json"))
        originals = [
            (a, read_evidence(repo, a.evidence_id)) for a in record.attachments if a.evidence_id
        ]
    changed = False
    for attachment, raw in originals:
        if raw.startswith(b"%PDF"):
            try:
                text = "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(raw)).pages)
            except Exception as exc:
                raise KnowledgeError(
                    "PARSING_FAILED", "PDF 重新提取失败，保留原件与旧版本"
                ) from exc
            attachment.text = text or None
            attachment.extraction_state = "text" if text else "scanned"
            changed = True
    if not changed:
        raise KnowledgeError("PARSING_UNSUPPORTED", "该资料没有可重新提取的文本 PDF")
    record.record_id = new_id()
    record.quality_state = "collected"
    record.parser_version += ":wiki-v1"
    return {
        "original_id": record_id,
        "fingerprint": baseline,
        "record": record.model_dump(mode="json"),
    }
