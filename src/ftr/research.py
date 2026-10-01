from __future__ import annotations

from pydantic import BaseModel, Field

from ftr.models import DocumentRecord
from ftr.repository import Repository


class Citation(BaseModel):
    record_id: str
    evidence_id: str
    quote: str = Field(min_length=1)


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
            if record.quality_state != "validated" or record.evidence_id != citation.evidence_id:
                raise ValueError("引用资料未经验证或证据版本不匹配")
            if citation.quote not in record.body_text and not any(
                citation.quote in (a.text or "")
                for a in record.attachments
                if a.extraction_state == "text"
            ):
                raise ValueError("引用摘录无法在原文中定位")
            labels.append(
                f"[{record.title}]({record.source_url})（资料版本 {record.record_id}，证据 {record.evidence_id}）"
            )
        lines.append(f"- {claim.text}（{claim.kind}） {'；'.join(labels)}")
    if draft.limitations:
        lines.extend(["", "## 证据缺口", *[f"- {x}" for x in draft.limitations]])
    return "\n".join(lines) + "\n"
