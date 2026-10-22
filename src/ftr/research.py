from __future__ import annotations

from pydantic import BaseModel, Field

from ftr.knowledge.models import Anchor
from ftr.models import DocumentRecord
from ftr.repository import Repository


class Citation(BaseModel):
    record_id: str
    evidence_id: str
    quote: str = Field(min_length=1)
    anchor: Anchor | None = None


class Claim(BaseModel):
    text: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)
    kind: str


class ResearchDraft(BaseModel):
    question: str
    as_of: str
    claims: list[Claim]
    limitations: list[str] = Field(default_factory=list)


def verify_and_render(repo: Repository, draft: ResearchDraft) -> str:
    lines = [f"# {draft.question}", "", f"资料截至：{draft.as_of}", ""]
    for claim in draft.claims:
        labels = []
        for citation in claim.citations:
            record: DocumentRecord = repo.get_document(citation.record_id)
            if record.quality_state != "validated":
                raise ValueError("引用资料未经验证")
            if citation.anchor is not None:
                from ftr.knowledge.indexing import check_anchor
                from ftr.knowledge.service import Knowledge

                anchor = citation.anchor
                if (anchor.record_id, anchor.evidence_id, anchor.quote) != (
                    citation.record_id,
                    citation.evidence_id,
                    citation.quote,
                ):
                    raise ValueError("引用身份与定位锚点不匹配")
                check_anchor(repo, anchor.model_dump())
                if Knowledge(repo).source_state(record.record_id) != "checked":
                    raise ValueError("引用资料已有未处理变化或依赖受限")
            else:
                if (
                    record.evidence_id != citation.evidence_id
                    or citation.quote not in record.body_text
                ):
                    raise ValueError("附件引用须提供实际附件证据和精确锚点；摘录无法在指定正文定位")
                if record.body_text.count(citation.quote) > 1:
                    raise ValueError("正文摘录重复，需要精确锚点消歧")
            labels.append(
                f"[{record.title}]({record.source_url})（资料版本 {record.record_id}，证据 {citation.evidence_id}，定位 {citation.anchor.segment_id if citation.anchor else '旧版正文未细分'}）"
            )
        lines.append(f"- {claim.text}（{claim.kind}） {'；'.join(labels)}")
    if draft.limitations:
        lines.extend(["", "## 证据缺口", *[f"- {x}" for x in draft.limitations]])
    return "\n".join(lines) + "\n"
