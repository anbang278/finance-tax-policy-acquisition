"""Wiki differences, source-backed rendering and reproducible Markdown export."""

from __future__ import annotations

import difflib
import json

from ftr.knowledge.models import KnowledgeError


class Wiki:
    def __init__(self, knowledge):
        self.knowledge = knowledge

    def diff(self, id, candidate):
        o = self.knowledge.object(id)
        v = self.knowledge.version(candidate)
        if v["object_id"] != id:
            raise KnowledgeError("OBJECT_MISMATCH", "候选不属于页面")
        old = (
            self.knowledge.version(o["current_version"])["content"] if o["current_version"] else {}
        )
        return {
            "before": old,
            "after": v["content"],
            "diff": "\n".join(
                difflib.unified_diff(
                    json.dumps(old, ensure_ascii=False, indent=2).splitlines(),
                    json.dumps(v["content"], ensure_ascii=False, indent=2).splitlines(),
                    fromfile="正式版本",
                    tofile="候选版本",
                )
            ),
        }

    def export(self, id, version=None):
        o = self.knowledge.object(id)
        vid = version or o["current_version"]
        if not vid:
            raise KnowledgeError("NOT_PUBLISHED", "尚无正式版本")
        v = self.knowledge.version(vid)
        if v["object_id"] != id:
            raise KnowledgeError("OBJECT_MISMATCH", "导出版本不属于对象")
        lines = [
            f"# {v['title']}",
            "",
            f"专题：{o['topic_id']} · 对象：{id} · 版本：{vid}",
            f"知识核验：{self.knowledge.approved(vid)} · 依赖：{self.knowledge.dependency(vid)}",
            f"版本建立：{v['created_at']}",
            "",
        ]
        c = v["content"]
        if o["kind"] == "page":
            lines.extend([c["overview"], c["coverage"]])
            for s in c["sections"]:
                lines.extend(["", f"## {s['title']}", s["text"]])
                for kv in s["knowledge_versions"]:
                    lines.extend(self.item_lines(kv))
        else:
            lines.extend(self.item_lines(vid))
        lines.extend(["", "## 证据缺口", *c["limitations"]])
        return "\n".join(lines) + "\n"

    def item_lines(self, id):
        c = self.knowledge.version(id)["content"]
        lines = [
            f"知识版本：{id}",
            c["statement"],
            f"性质：{c['nature']}",
            "条件：" + "；".join(c["conditions"]),
            "例外：" + "；".join(c["exceptions"]),
            f"适用期间：{c['valid_from'] or '未知'} 至 {c['valid_to'] or '未知'}（人工核验：{c['applicability_verified']}）",
            *c["limitations"],
        ]
        for a in c["citations"]:
            r = self.knowledge.repo.get_document(a["record_id"])
            lines.append(
                f"- 依据：[{r.title}]({r.source_url}) · 资料 {r.record_id} · 证据 {a['evidence_id']} · 片段 {a['segment_id']} · 页 {a['page_number'] or '不适用'} · 位置 {a['start_offset']}:{a['end_offset']}：{a['quote']}"
            )
        return lines
