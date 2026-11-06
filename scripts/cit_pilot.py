"""Prepare an explicit, empty replay store from the captured public pilot HTML.

This utility performs no network requests or reviews. Use normal CLI review/job
commands after inspecting the exported requests and corpus.
"""

from __future__ import annotations

import argparse
import json
from contextlib import closing
from datetime import date
from pathlib import Path

from ftr.adapters.common import parse_detail
from ftr.evidence import EvidenceStore
from ftr.models import DecisionRequest, DiscoveredRef, TaskRequest, digest
from ftr.repository import Repository
from ftr.runtime import data_lock


def prepare(seed_dir: Path, data_dir: Path, output: Path) -> None:
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError("回放目标须为空目录")
    captures = []
    for manifest_name, folder in (
        ("seed-manifest.json", "raw"),
        ("additional-seeds.json", "raw-extra"),
    ):
        manifest = json.loads((seed_dir / manifest_name).read_text(encoding="utf-8"))
        for document in manifest["documents"]:
            path = seed_dir / folder / (document["seed_id"] + ".html")
            if path.exists():
                captures.append((document, path))
    targets = [
        {
            "source_id": document["source_id"],
            "url": document["url"],
            "title": document["title"],
            "document_number": document["document_number"],
            "topic_id": "cit-pilot",
            "reason": "已捕获官方原件的隔离回放",
        }
        for document, _path in captures
    ]
    request = TaskRequest(
        mode="targeted",
        official_urls=targets,
        source_ids=list(dict.fromkeys(target["source_id"] for target in targets)),
        date_from=date(2015, 1, 1),
        date_to=date(2026, 11, 6),
    )
    corpus = []
    output.mkdir(parents=True, exist_ok=True)
    with data_lock(data_dir), closing(Repository(data_dir)) as repo:
        task = repo.create_task(request)
        store = EvidenceStore(data_dir)
        for document, path in captures:
            raw = path.read_bytes()
            ref = DiscoveredRef(
                source_id=document["source_id"],
                url=document["url"],
                listing_title=document["title"],
                document_number_hint=document["document_number"],
                listing_date_kind="captured_replay_date_unknown",
                discovered_from="seed_snapshot:" + digest(raw),
            )
            evidence = store.save(raw, ref.url, ref.url, "text/html")
            repo.save_evidence(evidence)
            record = parse_detail(ref, raw, evidence.evidence_id)
            record_id, _ = repo.add_document(task, record)
            decision = DecisionRequest(
                task_id=task,
                record_id=record_id,
                input_digest=digest(record.model_dump_json().encode()),
                excerpt=record.body_text[:1500],
                evidence_id=evidence.evidence_id,
            )
            repo.add_decision(decision)
            repo.set_ref_state(task, ref.source_id, ref.url, "SAVED")
            corpus.append(
                {
                    "seed_id": document["seed_id"],
                    "record": record.model_dump(mode="json"),
                    "decision_request": decision.model_dump(mode="json"),
                    "capture_source": str(path.relative_to(seed_dir)),
                    "capture_sha256": digest(raw),
                    "known_gaps": document["gaps"],
                }
            )
        repo.audit(
            task, "official_snapshot_replay", {"network_requests": 0, "records": len(corpus)}
        )
        repo.set_task_state(task, "WAITING_DECISION")
    result = {
        "mode": "captured_official_html_replay",
        "network_requests": 0,
        "data_dir": str(data_dir.resolve()),
        "task_id": task,
        "documents": corpus,
    }
    (output / "corpus.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"records": len(corpus), "task_id": task, "mode": result["mode"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.seed_dir, args.data_dir, args.output)
