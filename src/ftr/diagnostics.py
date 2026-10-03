"""Read-only explanations derived from existing evidence and audit records."""

from __future__ import annotations

import json
from pathlib import Path

import portalocker

REASONS = {
    "筛选日期缺失，范围待确认": ("missing_filter_date", "所选日期口径缺失，范围待确认"),
    "标题或正文缺失": ("missing_content", "标题或正文缺失"),
    "正文区域未可靠定位": ("unreliable_body", "正文区域未可靠定位"),
    "资料类型待确认": ("unknown_document_type", "资料类型待确认"),
    "部分附件未保存": ("attachment_not_saved", "部分附件未保存"),
    "主附件原件已登记，但内容尚未解析": ("primary_attachment_unparsed", "主附件内容尚未解析"),
    "主内容可能位于未解析附件": ("primary_content_unparsed", "主内容可能位于未解析附件"),
    "PDF 解析产生警告，内容完整性待复核": ("pdf_extraction_warning", "PDF 内容完整性待复核"),
}


def limitation_reasons(limitations):
    result = []
    for original in limitations:
        code, label = REASONS.get(original, ("other_historical", "其他／历史原因"))
        result.append({"code": code, "label": label, "original": original})
    return result


def writer_activity(root: Path):
    path = root / ".runtime.lock"
    if path.is_symlink():
        return "UNKNOWN"
    if not path.exists():
        return "NO_WRITER_OBSERVED"
    try:
        with path.open("rb") as handle:
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                return "WRITER_LOCK_HELD_TASK_UNKNOWN"
            portalocker.unlock(handle)
        return "NO_WRITER_OBSERVED"
    except OSError:
        return "UNKNOWN"


def task_diagnostics(db, task_id, root):
    row = db.execute(
        "SELECT details_json,created_at FROM audit WHERE task_id=? AND event='batch_stopped' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return {
        "writer_activity": writer_activity(root),
        "last_batch": {**json.loads(row[0]), "recorded_at": row[1]} if row else None,
    }


def recover_interrupted(repo):
    """Caller must hold the exclusive runtime lock. No process/PID guesses."""
    for row in repo.db.execute("SELECT id FROM tasks WHERE state='RUNNING'").fetchall():
        repo.set_task_state(row[0], "PARTIAL")
        repo.audit(
            row[0],
            "batch_stopped",
            {
                "reasons": ["INTERRUPTED_PREVIOUS_WRITER"],
                "message": "取得独占锁后发现旧 RUNNING 记录；可从检查点续跑",
            },
        )


def safe_url(value):
    """URLs in diagnostics never retain credentials, query values or fragments."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = parts.hostname
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return None


def safe_message(value):
    import re

    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>）]+", lambda m: safe_url(m[0]) or "[地址已隐藏]", text)
    text = re.sub(
        r"(?im)\b(cookie|set-cookie|authorization|proxy-authorization)\s*[:=][^\n]+",
        r"\1=[已隐藏]",
        text,
    )
    text = re.sub(
        r"(?i)\b(token|password|passwd|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text
    )
    return text[:2000]


def failure_explanation(category, details):
    status = details.get("http_status")
    message = details.get("message") or "历史记录未保存具体原因，证据不足"
    if details.get("stage") == "attachment_extraction":
        label, action = (
            "原件已保存，文本提取失败",
            "下载原件用对应软件查看；由 Agent 检查提取证据，需解析能力修复时交开发者处理，重复下载不会自动解决解析问题。",
        )
    elif status == 429:
        label, action = (
            "官网限流（HTTP 429）",
            "等待官网限流解除后，说“继续”；Agent 不绕过访问限制。",
        )
    elif status in (401, 403):
        label, action = (
            f"官网限制访问（HTTP {status}）",
            "由使用者检查官网是否可正常访问；恢复访问后说“继续”，Agent 不绕过验证码或访问控制。",
        )
    elif category == "ACCESS_RESTRICTED":
        label, action = (
            "访问或来源安全检查未通过",
            "按具体原因检查来源地址、域名解析或官网访问条件；恢复后说“继续”，不要关闭安全门禁。",
        )
    elif category == "TRANSIENT_NETWORK":
        label, action = (
            "请求暂时失败，已停止重试",
            "由使用者检查网络或等待官网恢复，再说“继续”；Agent 从原检查点补齐。",
        )
    elif category == "STRUCTURE_DRIFT":
        label, action = (
            "页面结构变化",
            "Agent 根据已保存原件执行受限 Repair；验证通过后再恢复，候选耗尽时交开发者处理。",
        )
    elif category == "BROWSER_ERROR":
        label, action = (
            "浏览器或显示环境不可用",
            "Agent 检查 setup 与浏览器配置；需系统显示依赖时由部署者处理，环境恢复后说“继续”。",
        )
    else:
        label, action = (
            "原因尚不能确定",
            "Agent 查看失败详情和证据；确认原因及恢复条件后再继续，不将未知故障统一归为网络。",
        )
    certainty = (
        "insufficient_evidence"
        if not details.get("message") or category == "UNKNOWN"
        else "suspected"
        if "可能" in message
        else "confirmed"
    )
    if certainty == "suspected":
        label = "访问条件尚不能确定"
        action = "Agent 检查浏览器导航、列表请求与已保存证据，确认是访问限制还是页面故障；条件恢复后再说“继续”，不绕过访问控制。"
    return {
        "label": label,
        "reason": safe_message(message),
        "certainty": certainty,
        "next_step": action,
        "resolved": bool(details.get("resolved")),
    }


def task_report(db, task_id):
    """One evidence-based summary shared by collector, readonly CLI and Web."""
    from collections import Counter

    from ftr.adapters.common import canonical_url
    from ftr.repository import associated_document

    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise KeyError(f"任务不存在: {task_id}")
    request = json.loads(task["request_json"])
    basis = request.get("date_basis", "source_listing")
    date_key = {
        "source_listing": "listing_date",
        "issued_date": "issued_date",
        "published_date": "published_date",
    }[basis]
    sources, missing, content_limits = [], [], []
    for run in db.execute("SELECT * FROM source_runs WHERE task_id=?", (task_id,)):
        records = {}
        queue = Counter()
        for row in db.execute(
            "SELECT * FROM discovered WHERE task_id=? AND source_id=?", (task_id, run["source_id"])
        ):
            queue[row["state"]] += 1
            ref = json.loads(row["ref_json"])
            if row["state"] in ("PENDING", "FAILED"):
                missing.append(
                    {
                        "source_id": run["source_id"],
                        "kind": "document",
                        "title": ref.get("listing_title"),
                        "url": safe_url(row["url"]),
                        "state": row["state"],
                        "reason": safe_message(row["error"]) if row["error"] else "尚未处理",
                        "next_step": "故障恢复或下一批时说“继续”，Agent 补齐该条目。",
                    }
                )
            if row["state"] not in ("SAVED", "UNCHANGED_SKIP"):
                continue
            record = associated_document(db, task_id, run["source_id"], canonical_url(row["url"]))
            if record:
                data = json.loads(record["record_json"])
                data["quality_state"] = record["quality_state"]
                records[record["id"]] = data
        attachment_counts = Counter()
        review = Counter()
        unknown = 0
        for record_id, record in records.items():
            review[record["quality_state"]] += 1
            unknown += not record.get(date_key)
            for att in record.get("attachments", []):
                saved = att.get("download_state") == "saved" and bool(att.get("evidence_id"))
                attachment_counts["saved" if saved else "missing"] += 1
                if not saved:
                    missing.append(
                        {
                            "source_id": run["source_id"],
                            "kind": "attachment",
                            "record_id": record_id,
                            "title": record.get("title"),
                            "label": att.get("label"),
                            "url": safe_url(att.get("url")),
                            "state": att.get("download_state"),
                            "reason": "附件尚未保存；具体失败原因见失败详情，预算停止时不是采集故障",
                            "next_step": "原因或预算条件允许后说“继续”，Agent 只补未保存的附件。",
                        }
                    )
                elif att.get("extraction_state") != "text":
                    content_limits.append(
                        {
                            "source_id": run["source_id"],
                            "record_id": record_id,
                            "title": record.get("title"),
                            "url": safe_url(att.get("url")),
                            "evidence_id": att.get("evidence_id"),
                            "reason": "原件已保存，格式暂不支持或文本未成功提取",
                            "next_step": "下载原件用对应软件查看；扫描件需可读文本或后续 OCR，重复采集不会增加解析能力。",
                        }
                    )
            if record.get("limitations"):
                content_limits.append(
                    {
                        "source_id": run["source_id"],
                        "record_id": record_id,
                        "title": record.get("title"),
                        "reason": "；".join(record["limitations"]),
                        "next_step": "查看正文、原件和限制详情；有内容限制时不能声称全文可供研究。",
                    }
                )
        done = bool(run["discovery_done"])
        sources.append(
            {
                "source_id": run["source_id"],
                "pages_scanned": run["pages_count"],
                "discovery_done": done,
                "coverage": "COMPLETE" if done else "UNKNOWN_REMAINDER",
                "discovery_strategy": "full_enumeration",
                "coverage_reason": "已到登记栏目末页"
                if done
                else "官方日期筛选与无日期条目的完整覆盖尚未获得证明，按栏目逐页核查",
                "next_page": run["next_page"] if not done else None,
                "unseen_matches": None if not done else 0,
                "documents_saved": len(records),
                "attachments": dict(attachment_counts),
                "date_unknown_documents": unknown,
                "review": dict(review),
                "queue": dict(queue),
            }
        )
    failures = []
    for row in db.execute(
        "SELECT * FROM failures WHERE task_id=? ORDER BY created_at,id", (task_id,)
    ):
        details = json.loads(row["details_json"])
        failures.append(
            {
                "failure_id": row["id"],
                "source_id": row["source_id"],
                "category": row["category"],
                "stage": details.get("stage"),
                "url": details.get("url"),
                "details": details,
                **failure_explanation(row["category"], details),
            }
        )
    active = [f for f in failures if not f["resolved"]]
    coverage_complete = all(s["discovery_done"] for s in sources)
    acquisition_failures = [f for f in active if f["stage"] != "attachment_extraction"]
    downloads_complete = not missing and not acquisition_failures
    last = db.execute(
        "SELECT details_json FROM audit WHERE task_id=? AND event='batch_stopped' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    stops = json.loads(last[0]).get("reasons", []) if last else []
    cancelled = bool(task["cancel_requested"]) or task["state"] == "CANCELLED"
    next_step = (
        "任务已取消；如需重新采集，请提出新任务。"
        if cancelled
        else "故障按详情处理后，说“继续”，Agent 沿原范围和检查点补齐。"
        if acquisition_failures
        else "说“继续”，Agent 从检查点补齐未完成内容。"
        if not coverage_complete or missing
        else "采集工作已结束，但有内容阅读限制；下载原件查看，需解析修复时交开发者处理，再按门禁复核。"
        if content_limits
        else "采集工作已结束；查看资料和原件，按需进行语义复核。"
    )
    return {
        "task_id": task_id,
        "state": task["state"],
        "request": request,
        "sources": sources,
        "missing_items": missing,
        "unseen_matches": None if not coverage_complete else 0,
        "failures": failures,
        "active_failure_count": len(active),
        "content_limits": content_limits,
        "completion": {
            "coverage": "COMPLETE" if coverage_complete else "INCOMPLETE",
            "downloads": "COMPLETE" if downloads_complete else "INCOMPLETE",
            "readability": "LIMITED"
            if content_limits
            else "READABLE"
            if any(s["documents_saved"] for s in sources)
            else "NO_CONTENT",
            "review": "NO_CONTENT"
            if not any(s["documents_saved"] for s in sources)
            else "PENDING_OR_LIMITED"
            if any(s["review"].get(k, 0) for s in sources for k in ("collected", "quarantined"))
            else "DECIDED",
        },
        "stop_reasons": stops,
        "next_step": next_step,
        "view": {"task": f"/api/tasks/{task_id}", "library": "/"},
    }
