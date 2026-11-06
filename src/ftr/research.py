from __future__ import annotations

import re
import unicodedata
from datetime import date

from pydantic import BaseModel, Field

from ftr.knowledge.indexing import check_anchor, fingerprint, read_evidence, segments
from ftr.knowledge.models import Anchor, SupportCheck, TemporalBasis
from ftr.knowledge.service import Knowledge
from ftr.models import DocumentRecord
from ftr.repository import Repository


class Citation(BaseModel):
    record_id: str
    evidence_id: str
    quote: str = Field(min_length=1)
    anchor: Anchor | None = None


class ResearchContext(BaseModel):
    tax_period: str | None = None
    filing_date: date | None = None
    region: str | None = None
    subject_facts: dict[str, str] = Field(default_factory=dict)
    required_authorities: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    text: str = Field(min_length=1)
    citations: list[Citation] = Field(min_length=1)
    kind: str
    conditions: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    temporal_basis: list[TemporalBasis] = Field(default_factory=list)
    checks: list[SupportCheck] = Field(default_factory=list)


class ResearchDraft(BaseModel):
    question: str
    as_of: str
    claims: list[Claim]
    limitations: list[str] = Field(default_factory=list)
    context: ResearchContext = Field(default_factory=ResearchContext)
    precheck_input_digest: str | None = None

    def support_digest(self) -> str:
        value = self.model_dump(mode="json", exclude={"precheck_input_digest"})
        for claim in value["claims"]:
            claim.pop("checks")
        return fingerprint(value)


def _source_available(repo: Repository, record: DocumentRecord) -> None:
    if record.quality_state != "validated":
        raise ValueError("引用资料未经验证")
    if Knowledge(repo).available():
        if Knowledge(repo).source_state(record.record_id) != "checked":
            raise ValueError("引用资料已有未处理变化或依赖受限")
        return
    # Legacy stores remain readable; all saved evidence still has to be intact.
    if record.limitations or any(
        a.download_state != "saved" or a.extraction_state != "text" for a in record.attachments
    ):
        raise ValueError("引用资料依赖受限")
    latest = repo.db.execute(
        "SELECT id FROM documents WHERE source_id=? AND canonical_url=? "
        "ORDER BY source_version DESC,extraction_version DESC LIMIT 1",
        (record.source_id, record.canonical_url),
    ).fetchone()
    if latest[0] != record.record_id:
        raise ValueError("引用资料已有未处理变化")
    for evidence_id in [
        record.evidence_id,
        *[a.evidence_id for a in record.attachments if a.evidence_id],
    ]:
        read_evidence(repo, evidence_id)


def verified_anchor(repo: Repository, citation: Citation) -> tuple[DocumentRecord, Anchor]:
    record = repo.get_document(citation.record_id)
    _source_available(repo, record)
    if citation.anchor is not None:
        anchor = citation.anchor
        if (anchor.record_id, anchor.evidence_id, anchor.quote) != (
            citation.record_id,
            citation.evidence_id,
            citation.quote,
        ):
            raise ValueError("引用身份与定位锚点不匹配")
        check_anchor(repo, anchor.model_dump())
        return record, anchor
    if record.evidence_id != citation.evidence_id or citation.quote not in record.body_text:
        raise ValueError("附件引用须提供实际附件证据和精确锚点；摘录无法在指定正文定位")
    if record.body_text.count(citation.quote) != 1:
        raise ValueError("正文摘录重复，需要精确锚点消歧")
    read_evidence(repo, record.evidence_id)
    for segment in segments(repo, record):
        if segment["source_part"] == "body" and citation.quote in segment["text"]:
            start = segment["text"].index(citation.quote)
            anchor = Anchor(
                **{
                    key: segment[key]
                    for key in (
                        "segment_id",
                        "record_id",
                        "evidence_id",
                        "source_part",
                        "text_version_digest",
                        "page_number",
                    )
                },
                quote=citation.quote,
                start_offset=start,
                end_offset=start + len(citation.quote),
            )
            return record, anchor
    raise ValueError("摘录跨越证据片段，请缩小摘录或提供分段锚点")


def _authority_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"[\s《》〔〕\[\]（）()]", "", value).removeprefix("中华人民共和国")


def assess_and_render(repo: Repository, draft: ResearchDraft) -> dict:
    if (
        any(claim.checks for claim in draft.claims)
        and draft.precheck_input_digest != draft.support_digest()
    ):
        raise ValueError("预审结果与当前研究输入摘要不一致")
    gaps = [*draft.limitations, *draft.context.missing_inputs]
    try:
        date.fromisoformat(draft.as_of)
    except ValueError:
        gaps.append("资料截止日需核实为具体日期，当前保留旧稿原值")
    if not draft.claims:
        gaps.append("尚无可核对研究陈述，请补充研究结果或来源材料")
    authority_names: set[str] = set()
    rendered_claims = []
    assessments = []
    for number, claim in enumerate(draft.claims, 1):
        labels = []
        anchors = []
        for citation in claim.citations:
            record, anchor = verified_anchor(repo, citation)
            anchors.append(anchor.model_dump(mode="json"))
            authority_names.update(
                _authority_name(value) for value in (record.title, record.document_number or "")
            )
            labels.append(
                f"[{record.title}]({record.source_url})（资料版本 {record.record_id}，"
                f"证据 {citation.evidence_id}，定位 {anchor.segment_id}，"
                f"字符区间 [{anchor.start_offset}, {anchor.end_offset})，"
                f"页 {anchor.page_number or '不适用'}）\n\n  > "
                + anchor.quote.replace("\n", "\n  > ")
            )
        for basis in claim.temporal_basis:
            for anchor in basis.citations:
                verified_anchor(
                    repo,
                    Citation(
                        record_id=anchor.record_id,
                        evidence_id=anchor.evidence_id,
                        quote=anchor.quote,
                        anchor=anchor,
                    ),
                )
        for check in claim.checks:
            if check.statement != claim.text:
                raise ValueError("预审陈述与研究结论不一致")
            if check.status != "insufficient" and not check.citations:
                raise ValueError("支持或冲突检查须提供引用")
            for anchor in check.citations:
                verified_anchor(
                    repo,
                    Citation(
                        record_id=anchor.record_id,
                        evidence_id=anchor.evidence_id,
                        quote=anchor.quote,
                        anchor=anchor,
                    ),
                )
        dimensions = {check.dimension for check in claim.checks}
        complete = dimensions == {"support", "conditions", "temporal", "authority"}
        supported = complete and all(check.status == "supported" for check in claim.checks)
        if (
            re.search(r"\d+(?:\.\d+)?%|税负|税率|加计|税额", claim.text)
            and not draft.context.tax_period
            and not claim.temporal_basis
        ):
            gaps.append(f"陈述{number}缺少税款所属期或有引用的时间依据")
            supported = False
        status = "AI检查通过，供使用者复核" if supported else "待核实"
        checks = [check.model_dump(mode="json") for check in claim.checks]
        assessments.append(
            {"claim": number, "status": status, "checks": checks, "anchors": anchors}
        )
        rendered_claims.extend(
            [f"### 陈述 {number}", "", claim.text, "", f"性质 {claim.kind} · 检查结果 {status}", ""]
        )
        for title, values in (("条件", claim.conditions), ("例外", claim.exceptions)):
            rendered_claims.append(f"{title} · " + ("、".join(values) or "待核实或未列明"))
        rendered_claims.extend(
            f"- 时间依据 {basis.kind} · {basis.value}" for basis in claim.temporal_basis
        )
        rendered_claims.extend(
            f"- {check.dimension} · {check.status} · {check.reason}" for check in claim.checks
        )
        rendered_claims.extend(["", *[f"- {label}" for label in labels], ""])
    missing = [
        name
        for name in draft.context.required_authorities
        if _authority_name(name) not in authority_names
    ]
    gaps.extend(f"指定法源未匹配 · {name}，请补充原文依据" for name in missing)
    state = (
        "needs_review"
        if gaps or any(a["status"] == "待核实" for a in assessments)
        else "ai_checked"
    )
    state_label = "待核实" if state == "needs_review" else "AI检查完成，供使用者复核"
    lines = [
        f"# {draft.question}",
        "",
        f"资料截至 {draft.as_of} · 报告状态 {state_label}",
        "",
        "## 研究背景",
        "",
        f"税款所属期 {draft.context.tax_period or '待核实'}",
        f"申报日期 {draft.context.filing_date or '待核实'}",
        f"地区 {draft.context.region or '待核实'}",
        *[f"- {key} · {value}" for key, value in draft.context.subject_facts.items()],
        "",
        "## 结论与依据",
        "",
        *rendered_claims,
        "## 冲突与证据缺口",
        "",
        *[f"- {gap}" for gap in dict.fromkeys(gaps)],
    ]
    if not gaps:
        lines.append("本轮未列出额外缺口，逐项检查范围见上文。")
    return {
        "markdown": "\n".join(lines) + "\n",
        "report_state": state,
        "claim_checks": assessments,
        "missing_authorities": missing,
        "precheck_input_digest": draft.support_digest(),
    }


def verify_and_render(repo: Repository, draft: ResearchDraft) -> str:
    return assess_and_render(repo, draft)["markdown"]
