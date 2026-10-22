from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Anchor(StrictModel):
    segment_id: str
    record_id: str
    evidence_id: str
    source_part: Literal["body", "attachment"]
    text_version_digest: str
    quote: str = Field(min_length=1)
    start_offset: int = Field(ge=0, strict=True)
    end_offset: int = Field(gt=0, strict=True)
    page_number: int | None = Field(default=None, ge=1, strict=True)


class ItemContent(StrictModel):
    statement: str = Field(min_length=1)
    nature: Literal["source_rule", "official_explanation", "synthesis", "business_suggestion"]
    conditions: list[str]
    exceptions: list[str]
    limitations: list[str]
    citations: list[Anchor] = Field(min_length=1)
    regions: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    scenarios: list[str] = Field(default_factory=list)
    valid_from: str | None = None
    valid_to: str | None = None
    applicability_verified: bool = False
    reasoning: str = ""
    relations: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def dates(self):
        from datetime import date

        for value in (self.valid_from, self.valid_to):
            if value:
                date.fromisoformat(value)
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("适用期间顺序无效")
        if self.nature == "synthesis" and not self.reasoning.strip():
            raise ValueError("综合分析须给出推导说明")
        return self


class Section(StrictModel):
    section_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str
    knowledge_versions: list[str] = Field(min_length=1)
    human_maintained: bool = False


class PageContent(StrictModel):
    sections: list[Section] = Field(min_length=1)
    limitations: list[str]
    overview: str = ""
    coverage: str = ""

    @model_validator(mode="after")
    def unique(self):
        ids = [s.section_id for s in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("章节身份重复")
        return self


class Output(StrictModel):
    kind: Literal["item", "page"]
    object_id: str | None = None
    title: str = Field(min_length=1, max_length=300)
    content: dict[str, Any]


class ProcessingResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    attempt_id: str
    input_digest: str
    submission_key: str = Field(min_length=1, max_length=200)
    outputs: list[Output] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    executor: dict[str, str] = Field(default_factory=dict)


class JobRequest(StrictModel):
    job_type: Literal["extract", "merge", "compile", "revise", "precheck"]
    topic_id: str
    records: list[str] = Field(default_factory=list)
    knowledge_versions: list[str] = Field(default_factory=list)
    object_id: str | None = None
    target_version: str | None = None
    executor: Literal["host", "api"] = "host"
    max_calls: int = Field(default=3, ge=1, le=20, strict=True)
    max_input_chars: int = Field(default=60000, ge=1000, le=1000000, strict=True)


class EditRequest(StrictModel):
    object_id: str
    title: str
    content: dict[str, Any]
    expected_revision: int = Field(ge=0, strict=True)


class ReviewRequest(StrictModel):
    version_id: str
    decision: Literal["approved", "rejected", "withdrawn"]
    reason: str = Field(min_length=1)
    confirmed: Literal[True]


class PublishRequest(StrictModel):
    version_id: str
    expected_revision: int = Field(ge=0, strict=True)
    expected_current: str | None
    confirmed: Literal[True]
