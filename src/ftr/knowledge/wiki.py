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

    def export(self, id, version=None, *, preview=False):
        o = self.knowledge.object(id)
        vid = version or o["current_version"]
        if not vid:
            raise KnowledgeError("NOT_PUBLISHED", "尚无正式版本")
        v = self.knowledge.version(vid)
        if v["object_id"] != id:
            raise KnowledgeError("OBJECT_MISMATCH", "导出版本不属于对象")
        if preview:
            self.knowledge.validate_content(o["kind"], v["content"], preview=True)
        lines = [
            f"# {v['title']}",
            "",
            "候选预览 · 等待使用者审核发布" if preview else "版本导出",
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
        reviews = self.knowledge.db.execute(
            "SELECT actor,decision,reason FROM kw_reviews WHERE version_id=? ORDER BY rowid", (vid,)
        ).fetchall()
        lines.extend(["", "## 检查记录", *[f"- {row[0]} · {row[1]} · {row[2]}" for row in reviews]])
        return "\n".join(lines) + "\n"

    def item_lines(self, id):
        c = self.knowledge.version(id)["content"]
        lines = [
            f"知识版本：{id}",
            c["statement"],
            f"性质：{c['nature']}",
            "条件：" + "；".join(c["conditions"]),
            "例外：" + "；".join(c["exceptions"]),
            f"适用期间：{c['valid_from'] or '未知'} 至 {c['valid_to'] or ('未规定终止' if c.get('valid_to_kind') == 'open_ended' else '待核实')}（人工核验：{c['applicability_verified']}）",
            f"知识审核：{self.knowledge.approved(id)} · 依赖：{self.knowledge.dependency(id)}",
            *c["limitations"],
        ]
        for basis in c.get("temporal_basis", []):
            lines.append(f"- 时间依据 {basis['kind']} · {basis['value']}")
        for relation in c.get("legal_relations", []):
            lines.append(
                f"- 法律关系 {relation['kind']} · 目标资料 {relation['target_record_id']} · {relation['scope']}"
            )
        for a in c["citations"]:
            r = self.knowledge.repo.get_document(a["record_id"])
            lines.append(
                f"- 依据：[{r.title}]({r.source_url}) · 资料 {r.record_id} · 证据 {a['evidence_id']} · 片段 {a['segment_id']} · 页 {a['page_number'] or '不适用'} · 位置 {a['start_offset']}:{a['end_offset']}：{a['quote']}"
            )
        return lines
