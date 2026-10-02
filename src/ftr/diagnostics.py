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
