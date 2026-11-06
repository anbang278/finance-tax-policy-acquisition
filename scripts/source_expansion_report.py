"""Summarize bounded samples without fetching, migrating or reviewing source data."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from ftr.adapters.public import PublicAdapter
from ftr.config import load_sources
from ftr.models import DiscoveredRef
from ftr.sources import catalog_entries


def summarize(samples: Path, output: Path) -> dict:
    registry = load_sources().sources
    rows = []
    for entry in catalog_entries():
        key = entry["source_id"]
        path = samples / "results" / (key + ".json")
        result = json.loads(path.read_text()) if path.exists() else {}
        prior = result.get("request", {}).get("source_snapshot", {}).get(key, {}).get("config", {})
        same_scope = bool(prior) and all(
            prior.get(field) == entry.get(field)
            for field in ("entry", "profile", "adapter", "listing_date_kind")
        )
        checks, gaps = [], []
        # Recheck old captured originals with the final parser. This does not
        # replace the separately recorded live attempt or write semantic quality.
        for record in result.get("details", []):
            check = {"url": record["source_url"], "evidence_id": record["evidence_id"]}
            evidence_id = record["evidence_id"]
            original = Path(result["data_dir"]) / "evidence" / evidence_id[:2] / evidence_id
            try:
                raw = original.read_bytes()
                if sha256(raw).hexdigest() != evidence_id:
                    raise ValueError("原件哈希不一致")
                if same_scope and entry.get("adapter", "").startswith("public_"):
                    adapter = PublicAdapter(key, registry[key], None)
                    adapter._final_urls[record["source_url"]] = record["canonical_url"]
                    if b"ftr-capture-kind" in raw:
                        adapter._media_types[record["source_url"]] = (
                            "application/vnd.ftr.browser-dom+html; charset=utf-8"
                        )
                    ref = DiscoveredRef(
                        source_id=key,
                        url=record["source_url"],
                        listing_title=record["listing_title"],
                        listing_date=record.get("listing_date"),
                        listing_date_kind=record["listing_date_kind"],
                        discovered_from=result["pages"][0]["evidence_id"],
                    )
                    parsed = adapter.parse_detail(ref, raw, evidence_id)
                    check["limitations"] = parsed.limitations
                    gaps.extend(
                        gap
                        for gap in parsed.limitations
                        if gap.startswith(("附件", "可见附件", "正文明确列示附件"))
                    )
                check["status"] = "PASS"
            except Exception as exc:  # noqa: BLE001 -- preserve a per-source verification result.
                check.update(status="FAIL", reason=str(exc))
                gaps.append(str(exc))
            checks.append(check)
        status = "BLOCKED"
        reason = entry.get("verification_note", "未完成真实产品链路验证")
        if result:
            status = result["status"]
            reason = result.get("reason", "")
            if result.get("request") and not same_scope:
                status, reason = (
                    "PARTIAL",
                    "前次样本保留；当前栏目或规则已调整，尚未完成当前范围复测",
                )
            if gaps and status == "PASS":
                status, reason = "PARTIAL", "当前解析器复核发现原件或附件缺口：" + "；".join(gaps)
        if not entry["selectable"] and entry.get("verification_note"):
            reason = str(reason) + "；栏目核验：" + entry["verification_note"]
        attachments = [
            a for record in result.get("details", []) for a in record.get("attachments", [])
        ]
        rows.append(
            {
                "source_id": key,
                "publisher": entry.get("publisher"),
                "region": entry.get("region"),
                "channel": entry.get("channel"),
                "categories": entry.get("categories", []),
                "entry": entry.get("entry"),
                "adapter": entry.get("adapter"),
                "listing_date_kind": entry.get("listing_date_kind"),
                "allowed_hosts": entry.get("allowed_hosts", []),
                "registered": key in registry,
                "selectable": entry["selectable"],
                "implementation_state": (
                    "pending_entry"
                    if key not in registry
                    else "adapter_ready"
                    if entry["selectable"]
                    else "profile_pending"
                ),
                "status": status,
                "reason": reason,
                "sample_result": str(path.resolve()) if result else None,
                "pages": len(result.get("pages", [])),
                "selected_details": len(result.get("selected_urls", [])),
                "saved_details": len(result.get("details", [])),
                "attachments": len(attachments),
                "saved_attachments": sum(a.get("download_state") == "saved" for a in attachments),
                "sample_dates": {
                    name: result.get("request", {}).get(name) for name in ("date_from", "date_to")
                },
                "same_scope": same_scope,
                "original_checks": checks,
                "parse_gaps": result.get("parse_gaps", []),
                "attachment_gaps": gaps + result.get("attachment_gaps", []),
                "idempotent_same_task": result.get("idempotent_same_task"),
                "repeat_new_versions": result.get("repeat_new_versions"),
            }
        )
    counts = Counter(row["status"] for row in rows)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "semantics": "PASS仅代表限定栏目样本的获取和原件检查；不证明历史全量、语义复核或所有附件已可解析。",
        "counts": dict(counts),
        "registered": len(registry),
        "selectable": sum(row["selectable"] for row in rows),
        "live_attempted": sum(row["sample_result"] is not None for row in rows),
        "saved_details": sum(row["saved_details"] for row in rows),
        "saved_attachments": sum(row["saved_attachments"] for row in rows),
        "rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "coverage.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    fields = [
        "source_id",
        "publisher",
        "region",
        "channel",
        "entry",
        "adapter",
        "status",
        "pages",
        "saved_details",
        "saved_attachments",
        "reason",
    ]
    with (output / "coverage.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# 逐来源覆盖矩阵",
        "",
        payload["semantics"],
        "",
        "| 来源ID | 地区/机构 | 栏目 | 结果 | 列表/正文/附件 | 原因 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        name = row["region"] if row["region"] != "全国" else row["publisher"]
        reason = str(row["reason"]).replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {row['source_id']} | {name} | {row['channel']} | {row['status']} | {row['pages']}/{row['saved_details']}/{row['saved_attachments']} | {reason} |"
        )
    (output / "coverage.md").write_text("\n".join(lines) + "\n")
    regional = [row for row in rows if row["source_id"].startswith("tax_")]
    regions = sorted({row["region"] for row in regional})
    lines = [
        "# 36局栏目结果",
        "",
        "每格对应独立采集栏目；不能用同局一个栏目通过代替其他栏目。",
        "",
        "| 地区 | 政策文件 | 规范性文件 | 解读 | 问答 |",
        "|---|---|---|---|---|",
    ]
    for region in regions:
        cells = []
        for channel in ("policy", "normative", "interpretation", "qa"):
            found = [
                r
                for r in regional
                if r["region"] == region and r["source_id"].endswith("_" + channel)
            ]
            cells.append("、".join(r["status"] for r in found) or "BLOCKED（待定位）")
        lines.append("| " + " | ".join([region, *cells]) + " |")
    (output / "regional-matrix.md").write_text("\n".join(lines) + "\n")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize(args.samples, args.output)
    print(
        json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2)
    )
