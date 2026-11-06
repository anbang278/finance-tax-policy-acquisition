"""Exercise the change-to-Wiki publication workflow with isolated fictional policies."""

from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import closing
from datetime import date
from pathlib import Path

from ftr.evidence import EvidenceStore
from ftr.knowledge.commands import dispatch
from ftr.knowledge.service import Knowledge
from ftr.models import DocumentRecord, DocumentType, TaskRequest, digest
from ftr.repository import Repository


def submit(k, job, output, key):
    package = k.package(job["id"])
    payload = {name: package[name] for name in ("job_id", "attempt_id", "input_digest")}
    payload.update(
        submission_key=key, outputs=[output], executor={"mode": "fixture", "model": "none"}
    )
    return k.submit(payload)["versions"][0]


def simulate_review_and_publish(k, version):
    k.review(version, "approved", "仅虚构隔离夹具模拟使用者审核", actor="human")
    obj = k.object(k.version(version)["object_id"])
    k.publish(version, obj["edit_revision"], obj["current_version"])


def anchor_for(k, job, record):
    segment = next(
        s for s in k.package(job["id"])["segments"] if s["record_id"] == record.record_id
    )
    value = {
        name: segment[name]
        for name in (
            "segment_id",
            "record_id",
            "evidence_id",
            "source_part",
            "text_version_digest",
            "page_number",
        )
    }
    value.update(quote=segment["text"], start_offset=0, end_offset=len(segment["text"]))
    return value


def demo(output_dir: Path):
    root = Path(tempfile.mkdtemp(prefix="ftr-cit-change-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    with closing(Repository(root)) as repo:
        task = repo.create_task(
            TaskRequest(source_ids=["mof"], date_from=date(2026, 1, 1), date_to=date(2026, 11, 6))
        )
        store = EvidenceStore(root)

        def record(name, text):
            url = "https://www.mof.gov.cn/fixture/" + name
            evidence = store.save(text.encode(), url, url, "text/plain")
            repo.save_evidence(evidence)
            material = DocumentRecord(
                source_id="mof",
                source_url=url,
                canonical_url=url,
                listing_title=name,
                title=name,
                document_type=DocumentType.POLICY_FILE,
                listing_date_kind="fictional",
                body_text=text,
                body_sha256=digest(text.encode()),
                evidence_id=evidence.evidence_id,
                quality_state="validated",
            )
            repo.add_document(task, material)
            return material

        old = record("虚构旧规则", "虚构政策规定留存三年。")
        k = Knowledge(repo)
        k.seed()
        k.reindex()
        job = k.create_job("extract", "cit-conditions", [old.record_id])
        old_anchor = anchor_for(k, job, old)
        item_content = {
            "statement": "虚构旧规则要求留存三年。",
            "nature": "source_rule",
            "conditions": ["仅隔离测试"],
            "exceptions": [],
            "limitations": ["虚构政策"],
            "citations": [old_anchor],
        }
        item = submit(
            k, job, {"kind": "item", "title": "虚构留存规则", "content": item_content}, "old-item"
        )
        simulate_review_and_publish(k, item)
        page_content = {
            "overview": "虚构来源变化流程",
            "coverage": "仅测试",
            "sections": [
                {
                    "section_id": "retention",
                    "title": "留存条件",
                    "text": "本虚构场景留存三年。",
                    "knowledge_versions": [item],
                }
            ],
            "limitations": ["自动化夹具模拟人工动作"],
        }
        job = k.create_job("compile", "cit-conditions", [], knowledge_versions=[item])
        page = submit(
            k, job, {"kind": "page", "title": "虚构专题", "content": page_content}, "old-page"
        )
        simulate_review_and_publish(k, page)
        (output_dir / "before.md").write_text(k.export(k.version(page)["object_id"]))
        new = record("虚构修订规则", "虚构修订文件将旧规则的留存三年改为四年。")
        k.reindex()
        job = k.create_job("extract", "cit-conditions", [new.record_id, old.record_id])
        new_anchor = anchor_for(k, job, new)
        change_content = {
            **item_content,
            "statement": "虚构新规则改为留存四年。",
            "citations": [new_anchor],
            "legal_relations": [
                {
                    "kind": "amends",
                    "target_record_id": old.record_id,
                    "scope": "留存年限",
                    "citations": [new_anchor],
                }
            ],
        }
        submit(
            k,
            job,
            {"kind": "item", "title": "虚构修订关系", "content": change_content},
            "new-relation",
        )
        assert k.dependency(item) == k.dependency(page) == "needs_review"
        impacts = {
            i["version_id"]: i
            for i in k.listing("impacts")
            if i["reason"] == "legal_relation_candidate"
        }
        item_args = {
            "id": impacts[item]["id"],
            "topic_id": "cit-conditions",
            "reason": "虚构修订演示",
        }
        item_job = dispatch(repo, {"operation": "impact.resolve", "arguments": item_args}, None)[
            "job"
        ]
        assert k.dependency(item) == "needs_review"
        replacement = submit(
            k,
            item_job,
            {
                "kind": "item",
                "object_id": k.version(item)["object_id"],
                "title": "虚构留存新规则",
                "content": {
                    **item_content,
                    "statement": "虚构新规则要求留存四年。",
                    "citations": [new_anchor],
                },
            },
            "revised-item",
        )
        simulate_review_and_publish(k, replacement)
        item_resolution = dispatch(
            repo,
            {
                "operation": "impact.resolve",
                "arguments": {**item_args, "resolution": "revision_published"},
            },
            None,
        )
        page_args = {
            "id": impacts[page]["id"],
            "topic_id": "cit-conditions",
            "reason": "虚构专题引用更新",
        }
        page_job = dispatch(repo, {"operation": "impact.resolve", "arguments": page_args}, None)[
            "job"
        ]
        updated_page = {
            **page_content,
            "sections": [
                {
                    "section_id": "retention",
                    "title": "留存条件",
                    "text": "本虚构场景按新规则留存四年。",
                    "knowledge_versions": [replacement],
                }
            ],
        }
        revised_page = submit(
            k,
            page_job,
            {
                "kind": "page",
                "object_id": k.version(page)["object_id"],
                "title": "虚构专题修订版",
                "content": updated_page,
            },
            "revised-page",
        )
        simulate_review_and_publish(k, revised_page)
        page_resolution = dispatch(
            repo,
            {
                "operation": "impact.resolve",
                "arguments": {**page_args, "resolution": "revision_published"},
            },
            None,
        )
        (output_dir / "after.md").write_text(k.export(k.version(revised_page)["object_id"]))
        assert k.version(page)["content"]["sections"][0]["text"] == "本虚构场景留存三年。"
        assert k.dependency(revised_page) == "checked"
        result = {
            "status": "PASS",
            "mode": "fictional_isolated_workflow",
            "human_actions": "simulated_by_fixture",
            "network_requests": 0,
            "old_item": item,
            "new_item": replacement,
            "old_page": page,
            "new_page": revised_page,
            "item_resolution": item_resolution,
            "page_resolution": page_resolution,
            "final_dependency": k.dependency(revised_page),
            "historical_versions_preserved": True,
        }
        (output_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    demo(parser.parse_args().output)
