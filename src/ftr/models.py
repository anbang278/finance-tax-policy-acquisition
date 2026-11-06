from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from ftr.sources import RegisteredSourceId


def utc_now() -> datetime:
    return datetime.now(UTC)


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


def new_id() -> str:
    return uuid4().hex


class DocumentType(StrEnum):
    POLICY_FILE = "policy_file"
    POLICY_ANNOUNCEMENT = "policy_announcement"
    OFFICIAL_INTERPRETATION = "official_interpretation"
    OFFICIAL_QA = "official_qa"
    RELEASE_MESSAGE = "release_message"
    OTHER = "other"
    UNKNOWN = "unknown"


def default_sources() -> list[str]:
    return ["mof", "chinatax"]


class OfficialTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source_id: RegisteredSourceId
    url: str
    title: str = Field(min_length=1)
    topic_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    document_number: str | None = None

    @model_validator(mode="after")
    def trusted_url(self, info: ValidationInfo):
        if (info.context or {}).get("saved"):
            return self
        from urllib.parse import urlsplit

        from ftr.config import load_sources

        parsed = urlsplit(self.url)
        hosts = load_sources().sources[self.source_id].allowed_hosts
        if (
            parsed.scheme != "https"
            or parsed.hostname not in hosts
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("定向URL须为所属来源允许的无凭据HTTPS地址")
        return self


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def from_saved(cls, value: str) -> TaskRequest:
        """Legacy reads keep the original serialized request/digest untouched."""
        import json

        payload = json.loads(value)
        return cls.model_validate(
            {key: item for key, item in payload.items() if key in cls.model_fields},
            context={"saved": True},
        )

    schema_version: Literal["1.0"] = "1.0"
    query: str = ""
    source_ids: list[RegisteredSourceId] = Field(default_factory=default_sources)
    source_snapshot: dict[str, Any] = Field(default_factory=dict)
    date_from: date
    date_to: date
    date_basis: Literal["source_listing", "issued_date", "published_date"] = "source_listing"
    mode: Literal["initial", "incremental", "backfill", "rescan", "targeted"] = "initial"
    official_urls: list[OfficialTarget] = Field(default_factory=list, max_length=100)
    idempotency_key: str | None = None
    max_pages: int = Field(default=1000, ge=1, le=1000)
    max_documents: int = Field(default=1000, ge=1, le=1000)

    @model_validator(mode="after")
    def check_dates(self) -> TaskRequest:
        if self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        if len(self.source_ids) != len(set(self.source_ids)) or not self.source_ids:
            raise ValueError("source_ids 必须非空且不重复")
        if bool(self.official_urls) != (self.mode == "targeted"):
            raise ValueError("定向模式须提供official_urls，其他模式使用栏目发现")
        if self.official_urls:
            identities = {(item.source_id, item.url) for item in self.official_urls}
            if len(identities) != len(self.official_urls):
                raise ValueError("定向URL清单含重复条目")
            if {item.source_id for item in self.official_urls} != set(self.source_ids):
                raise ValueError("定向清单来源须与source_ids一致")
        return self


class DiscoveredRef(BaseModel):
    source_id: str
    url: str
    listing_title: str
    listing_date: date | None = None
    listing_date_kind: str
    document_number_hint: str | None = None
    source_type_hint: str | None = None
    discovered_from: str


class Attachment(BaseModel):
    url: str
    label: str
    content_role: Literal["primary", "supplement", "unknown"] = "unknown"
    download_state: Literal["saved", "failed", "blocked", "pending"] = "pending"
    extraction_state: Literal["text", "unsupported", "scanned", "failed", "pending"] = "pending"
    evidence_id: str | None = None
    text: str | None = None


class Evidence(BaseModel):
    evidence_id: str
    source_url: str
    final_url: str
    sha256: str
    relative_path: str
    media_type: str
    size_bytes: int
    retrieved_at: datetime


class DocumentRecord(BaseModel):
    record_id: str = Field(default_factory=new_id)
    source_id: str
    source_url: str
    canonical_url: str
    listing_title: str
    title: str
    document_type: DocumentType
    source_publisher: str = ""
    issuing_authorities: list[str] = Field(default_factory=list)
    applicable_regions: list[str] = Field(default_factory=list)
    content_stage: Literal["formal", "draft", "unknown"] = "unknown"
    categories: list[str] = Field(default_factory=list)
    listing_date: date | None = None
    listing_date_kind: str
    issued_date: date | None = None
    published_date: date | None = None
    document_number: str | None = None
    source_status_claim: str | None = None
    body_text: str = ""
    body_sha256: str = ""
    evidence_id: str
    attachments: list[Attachment] = Field(default_factory=list)
    related_urls: list[str] = Field(default_factory=list)
    parser_version: str = "0.1.3"
    quality_state: Literal["collected", "validated", "quarantined", "rejected"] = "collected"
    limitations: list[str] = Field(default_factory=list)
    collection_notes: list[str] = Field(default_factory=list)
    field_evidence: dict[str, str] = Field(default_factory=dict)
    date_range_status: Literal["within_range", "unknown"] = "unknown"


class DecisionRequest(BaseModel):
    decision_id: str = Field(default_factory=new_id)
    task_id: str
    record_id: str
    input_digest: str
    allowed_actions: list[str] = Field(default_factory=lambda: ["PASS", "REJECT", "UNCERTAIN"])
    excerpt: str
    evidence_id: str


class SemanticDecision(BaseModel):
    decision_id: str
    input_digest: str
    result: Literal["PASS", "REJECT", "UNCERTAIN"]
    reasons: list[str]
    evidence_ids: list[str]
    model_id: str | None = None


class OperationResponse(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    operation: str
    task_id: str | None = None
    status: str
    data: dict[str, Any] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
