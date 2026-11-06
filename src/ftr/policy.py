"""Policy identity candidates preserve all source records and evidence versions."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from ftr.models import DocumentRecord, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_records (
 record_id TEXT PRIMARY KEY REFERENCES documents(id),
 policy_key TEXT NOT NULL,
 identity_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS policy_record_key ON policy_records(policy_key);
PRAGMA user_version=6;
"""


def policy_identity(number: str | None, title: str, record_id: str) -> dict:
    normalized = unicodedata.normalize("NFKC", number or "")
    normalized = re.sub(r"[\s〔〕\[\]（）()年第、]", "", normalized)
    normalized = normalized.replace("国家税务总局", "税务总局").replace("科学技术部", "科技部")
    match = re.fullmatch(r"(.+?)((?:19|20)\d{2})(\d+)号", normalized)
    result: dict[str, Any] = {
        "title": title,
        "original_number": number,
        "normalized_number": normalized,
        "identity_status": "unresolved",
        "policy_key": "record:" + record_id,
    }
    if not match:
        return result
    prefix, year, serial = match.groups()
    known_series = {"财税", "财会", "国税发", "国税函", "税总发", "税总函"}
    if prefix in known_series:
        issuer_key = prefix
    else:
        names = ("财政部", "税务总局", "科技部", "商务部", "工业和信息化部", "国家发展改革委")
        issuers = sorted(name for name in names if name in prefix)
        remainder = prefix.removesuffix("公告")
        for name in issuers:
            remainder = remainder.replace(name, "")
        if not issuers or remainder:
            return result
        issuer_key = "+".join(issuers) + (":公告" if prefix.endswith("公告") else "")
    identity = {"issuer_key": issuer_key, "year": int(year), "serial": int(serial)}
    result.update(identity)
    result.update(
        identity_status="candidate",
        policy_key=digest(json.dumps(identity, sort_keys=True).encode()),
    )
    return result


def register_record(db, record: DocumentRecord) -> None:
    identity = policy_identity(record.document_number, record.title, record.record_id)
    db.execute(
        "INSERT OR IGNORE INTO policy_records(record_id,policy_key,identity_json) VALUES(?,?,?)",
        (record.record_id, identity["policy_key"], json.dumps(identity, ensure_ascii=False)),
    )


def policy_groups(repo) -> list[dict]:
    if not repo.db.execute("SELECT 1 FROM sqlite_master WHERE name='policy_records'").fetchone():
        return []
    groups: dict[str, dict] = {}
    for row in repo.db.execute(
        "SELECT p.policy_key,p.identity_json,d.record_json FROM policy_records p "
        "JOIN documents d ON p.record_id=d.id ORDER BY d.rowid"
    ):
        identity = json.loads(row[1])
        record = DocumentRecord.model_validate_json(row[2])
        group = groups.setdefault(
            row[0], {"policy_key": row[0], "identity": identity, "sources": []}
        )
        group["sources"].append(
            {
                "record_id": record.record_id,
                "source_id": record.source_id,
                "url": record.source_url,
                "evidence_id": record.evidence_id,
                "body_digest": digest(record.body_text.encode()),
                "quality_state": record.quality_state,
                "limitations": record.limitations,
                "attachments": [
                    {
                        "label": a.label,
                        "download": a.download_state,
                        "extraction": a.extraction_state,
                    }
                    for a in record.attachments
                ],
            }
        )
    for group in groups.values():
        texts = {source["body_digest"] for source in group["sources"]}
        group["comparison"] = (
            "different_text"
            if len(texts) > 1
            else "same_text"
            if len(group["sources"]) > 1
            else "single_source"
        )
    return list(groups.values())
