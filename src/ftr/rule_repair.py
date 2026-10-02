"""Trusted gates for local rule releases; candidates are data, not code."""

from __future__ import annotations

import json
import re
import tempfile
import time
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from ftr.adapters.common import parse_detail
from ftr.config import load_sources
from ftr.evidence import EvidenceStore
from ftr.models import DiscoveredRef, TaskRequest, digest, new_id
from ftr.rules import Rules, active_rules, active_state, atomic_json, extract_listing, read_rules


def executor_fingerprint():
    root = files("ftr")
    paths = (
        "rules.py",
        "rule_repair.py",
        "runtime.py",
        "network.py",
        "browser.py",
        "adapters/common.py",
        "models.py",
        "config.py",
        "repository.py",
    )
    values = [digest(root.joinpath(name).read_bytes()) for name in paths]
    values.append(digest(load_sources().model_dump_json().encode()))
    contract = root.joinpath("data/official-material.yaml")
    if not contract.is_file():
        contract = Path(__file__).resolve().parents[2] / "contracts/official-material.yaml"
    values.append(digest(contract.read_bytes()))
    return digest(json.dumps(values).encode())


def failure_context(repo, failure_id):
    row = repo.db.execute("SELECT * FROM failures WHERE id=?", (failure_id,)).fetchone()
    if row is None:
        raise KeyError("失败记录不存在")
    return {**dict(row), "context": json.loads(row["details_json"])}


def _folder(root, candidate):
    if not re.fullmatch(r"[a-f0-9]{32}", candidate):
        raise ValueError("候选 ID 无效")
    path = root / "rules" / "candidates" / candidate
    if not path.resolve().is_relative_to(root.resolve()) or any(
        p.is_symlink() for p in (root / "rules", path.parent, path)
    ):
        raise ValueError("候选路径越界")
    return path


def submit(root, repo, failure_id, path):
    failure = failure_context(repo, failure_id)
    if failure["category"] != "STRUCTURE_DRIFT" or not failure["context"].get("evidence_id"):
        raise ValueError("只有有证据的结构漂移可自动规则修复；访问限制或未知原因不可修复")
    task = repo.task(failure["task_id"])
    if task["cancel_requested"] or task["pause_requested"]:
        raise ValueError("任务已暂停或取消")
    events = repo.db.execute(
        "SELECT details_json FROM audit WHERE task_id=? AND event='rule_candidate'",
        (failure["task_id"],),
    ).fetchall()
    if sum(json.loads(row[0])["source_id"] == failure["source_id"] for row in events) >= 2:
        raise ValueError("每任务每来源规则候选预算已耗尽（最多两轮），请人工处理")
    rules = read_rules(path)
    if rules.source_id != failure["source_id"]:
        raise ValueError("规则来源与故障不匹配")
    base = active_rules(root, rules.source_id).version
    if rules.version == base:
        raise ValueError("候选规则与活动版本相同，不构成修复")
    if failure["context"]["rule_version"] != base:
        raise ValueError("失败现场规则已过期，请重新诊断")
    candidate = new_id()
    folder = _folder(root, candidate)
    manifest = {
        "candidate_id": candidate,
        "failure_id": failure_id,
        "task_id": failure["task_id"],
        "source_id": rules.source_id,
        "version": rules.version,
        "base_version": base,
    }
    atomic_json(folder / "rules.json", rules.model_dump())
    atomic_json(folder / "manifest.json", manifest)
    repo.audit(failure["task_id"], "rule_candidate", manifest)
    return {**manifest, "state": "RULE_PROPOSED"}


def candidate_data(root, repo, candidate):
    folder = _folder(root, candidate)
    rules = read_rules(folder / "rules.json")
    manifest = json.loads((folder / "manifest.json").read_text())
    rows = repo.db.execute("SELECT details_json FROM audit WHERE event='rule_candidate'").fetchall()
    if manifest not in [json.loads(row[0]) for row in rows] or rules.version != manifest["version"]:
        raise ValueError("候选制品或登记记录被篡改")
    if rules.source_id != manifest["source_id"]:
        raise ValueError("来源不匹配")
    return folder, rules, manifest


def _links(refs, source):
    hosts = load_sources().sources[source].allowed_hosts
    if not refs:
        raise ValueError("修复验证没有实际条目")
    for ref in refs:
        url = urlsplit(ref.url)
        if (
            url.scheme != "https"
            or url.hostname not in hosts
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError("候选生成了不可信链接")
        if not ref.listing_title or ref.listing_date is None:
            raise ValueError("候选条目标题或日期缺失")
    if len({ref.url for ref in refs}) != len(refs):
        raise ValueError("候选列表存在重复链接")


def offline(root, repo, rules, failure):
    samples = json.loads((files("ftr") / "data" / "rule-fixtures.json").read_text())
    selected = [sample for sample in samples if sample["source_id"] == rules.source_id]
    if not selected:
        raise ValueError("可信样本缺失")
    for sample in selected:
        refs, next_page = extract_listing(rules, sample["listing"].encode(), sample["url"], 1)
        _links(refs, rules.source_id)
        baseline, baseline_next = extract_listing(
            Rules(source_id=rules.source_id), sample["listing"].encode(), sample["url"], 1
        )
        if [ref.model_dump() for ref in refs] != [
            ref.model_dump() for ref in baseline
        ] or next_page != baseline_next:
            raise ValueError("历史列表样本发生语义回归")
        record = parse_detail(refs[0], sample["detail"].encode(), "fixture", rules)
        if (
            record.title != sample["title"]
            or record.body_text != sample["body"]
            or str(record.listing_date) != sample["date"]
        ):
            raise ValueError("历史正文样本发生语义回归")
    context = failure["context"]
    evidence = repo.get_evidence(context["evidence_id"])
    raw = EvidenceStore(root).read(evidence)
    if context["stage"] == "discover":
        refs, next_page = extract_listing(
            rules, raw, load_sources().sources[rules.source_id].entry, context["page"]
        )
        _links(refs, rules.source_id)
        if next_page is not None and next_page != context["page"] + 1:
            raise ValueError("分页不前进")
    elif context["stage"] == "detail":
        ref = DiscoveredRef.model_validate(context["ref"])
        record = parse_detail(ref, raw, evidence.evidence_id, rules)
        _links([ref], rules.source_id)
        if not record.body_text or not record.title or record.limitations:
            raise ValueError("故障正文仍未可靠恢复")
    else:
        raise ValueError("缺少支持的失败阶段")
    return {
        "fixture_count": len(selected),
        "fixture_sha256": digest(json.dumps(samples).encode()),
        "failure_evidence": evidence.sha256,
    }


def _test_candidate(root, repo, candidate, settings, *, live=False):
    folder, rules, manifest = candidate_data(root, repo, candidate)
    failure = failure_context(repo, manifest["failure_id"])
    task = repo.task(manifest["task_id"])
    if task["cancel_requested"] or task["pause_requested"]:
        raise ValueError("任务已暂停或取消，停止规则验证")
    checks = offline(root, repo, rules, failure)
    report = {
        **manifest,
        "checks": checks,
        "executor_sha256": executor_fingerprint(),
        "settings_sha256": digest(settings.model_dump_json().encode()),
        "created_at": time.time(),
        "state": "RULE_OFFLINE_VALIDATED",
        "live_verified": False,
    }
    if live:
        # Only trusted Collector code performs requests; this isolated DB never changes real materials.
        from ftr.runtime import Collector

        request = TaskRequest.model_validate_json(repo.task(manifest["task_id"])["request_json"])
        request = request.model_copy(
            update={
                "source_ids": [rules.source_id],
                "max_pages": 1,
                "max_documents": 2,
                "idempotency_key": None,
            }
        )
        with tempfile.TemporaryDirectory(prefix="ftr-rule-probe-") as temp:
            isolated = Path(temp)
            bounded = settings.model_copy(
                update={
                    "data_dir": isolated,
                    "collection": settings.collection.model_copy(
                        update={
                            "max_duration_seconds": min(
                                120, settings.collection.max_duration_seconds
                            )
                        }
                    ),
                }
            )
            collector = Collector(
                isolated, bounded, rule_overrides={rules.source_id: rules}, detail_limit=2
            )
            try:
                task_id = collector.repo.create_task(request)
                context = failure["context"]
                collector.repo.db.execute(
                    "UPDATE source_runs SET next_page=? WHERE task_id=?", (context["page"], task_id)
                )
                if context["stage"] == "detail":
                    collector.repo.save_page(
                        task_id,
                        rules.source_id,
                        [DiscoveredRef.model_validate(context["ref"])],
                        context["page"],
                    )
                collector.repo.db.commit()
                result = collector.resume(task_id)
                records = collector.repo.db.execute(
                    "SELECT record_json FROM documents WHERE task_id=?", (task_id,)
                ).fetchall()
                if (
                    collector.failure_ids
                    or not records
                    or {"INTERRUPTED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}.intersection(
                        result.data.get("stop_reasons", [])
                    )
                ):
                    raise ValueError("真实来源验证失败或没有范围内资料；禁止启用")
                for row in records:
                    record = json.loads(row[0])
                    if not record["title"] or not record["body_text"] or record["limitations"]:
                        raise ValueError("真实资料内容不完整；禁止启用")
                report.update(
                    state="RULE_VERIFIED",
                    live_verified=True,
                    live_records=len(records),
                    live_status=result.status,
                    live_evidence=[
                        json.loads(row[0])["sha256"]
                        for row in collector.repo.db.execute("SELECT metadata_json FROM evidence")
                    ],
                )
            finally:
                collector.close()
    atomic_json(folder / "report.json", report)
    repo.audit(manifest["task_id"], "rule_validation", report)
    return report


def test_candidate(root, repo, candidate, settings, *, live=False):
    folder, _rules, manifest = candidate_data(root, repo, candidate)
    pending = {**manifest, "state": "RULE_VALIDATING", "live_verified": False}
    atomic_json(folder / "report.json", pending)
    repo.audit(manifest["task_id"], "rule_validation", pending)
    try:
        return _test_candidate(root, repo, candidate, settings, live=live)
    except Exception as exc:
        failed = {
            **manifest,
            "state": "RULE_VALIDATION_FAILED",
            "live_verified": False,
            "error_type": type(exc).__name__,
        }
        atomic_json(folder / "report.json", failed)
        repo.audit(manifest["task_id"], "rule_validation", failed)
        raise


def rollback(root, repo, source, reason="用户回退"):
    state = active_state(root, source)
    if not state or not state.get("previous"):
        raise ValueError("没有可回退规则版本")
    previous = state["previous"]
    restored = Rules.model_validate(previous["rules"])
    if restored.source_id != source or restored.version != previous["version"]:
        raise ValueError("上一规则版本损坏，禁止回退写入")
    atomic_json(root / "rules" / f"{source}.json", previous)
    repo.audit(
        state.get("task_id"),
        "rule_rollback",
        {"source_id": source, "version": state["version"], "reason": reason},
    )
    return {"state": "RULE_ROLLED_BACK", "source_id": source, "version": previous["version"]}


def recover(root, repo):
    for source in ("mof", "chinatax"):
        if active_state(root, source).get("probation"):
            rollback(root, repo, source, "上次启用验证中断，恢复上一版本")


def activate(root, repo, candidate, settings):
    folder, rules, manifest = candidate_data(root, repo, candidate)
    failure = failure_context(repo, manifest["failure_id"])
    checks = offline(root, repo, rules, failure)
    report = json.loads((folder / "report.json").read_text())
    audits = repo.db.execute(
        "SELECT details_json FROM audit WHERE event='rule_validation'"
    ).fetchall()
    if (
        report not in [json.loads(row[0]) for row in audits]
        or not report.get("live_verified")
        or report["state"] != "RULE_VERIFIED"
    ):
        raise ValueError("没有可信真实来源验证报告")
    if (
        report.get("checks") != checks
        or report.get("executor_sha256") != executor_fingerprint()
        or report.get("settings_sha256") != digest(settings.model_dump_json().encode())
    ):
        raise ValueError("可信样本、执行器或运行配置已变化，请重新验证")
    if (
        any(report[key] != manifest[key] for key in manifest)
        or time.time() - report["created_at"] > 900
    ):
        raise ValueError("报告过期或未绑定候选")
    if active_rules(root, rules.source_id).version != manifest["base_version"]:
        raise ValueError("活动版本发生并发变化，请重新验证")
    task = repo.task(manifest["task_id"])
    if task["pause_requested"] or task["cancel_requested"]:
        raise ValueError("任务已暂停或取消")
    previous_rules = active_rules(root, rules.source_id)
    previous = {"rules": previous_rules.model_dump(), "version": previous_rules.version}
    state = {**manifest, "rules": rules.model_dump(), "previous": previous, "probation": True}
    atomic_json(root / "rules" / f"{rules.source_id}.json", state)
    repo.audit(manifest["task_id"], "rule_activation", manifest)
    from ftr.runtime import Collector

    collector = None
    try:
        bounded = settings.model_copy(
            update={
                "collection": settings.collection.model_copy(
                    update={
                        "max_duration_seconds": min(120, settings.collection.max_duration_seconds)
                    }
                )
            }
        )
        collector = Collector(root, bounded, detail_limit=2)
        result = collector.resume(manifest["task_id"], bounded_source=rules.source_id)
        if (
            collector.failure_ids
            or not collector.processed_documents
            or collector.incomplete_documents
            or result.status == "CANCELLED"
            or {"INTERRUPTED", "PAUSE_REQUESTED", "CANCEL_REQUESTED"}.intersection(
                result.data.get("stop_reasons", [])
            )
        ):
            return rollback(root, repo, rules.source_id, "首次有界续跑失败")
        state["probation"] = False
        atomic_json(root / "rules" / f"{rules.source_id}.json", state)
        repo.audit(manifest["task_id"], "rule_enabled", manifest)
        return {
            "state": "RULE_ACTIVE",
            "version": rules.version,
            "task_id": manifest["task_id"],
            "resume": result.model_dump(mode="json"),
        }
    except Exception:
        rollback(root, repo, rules.source_id, "首次有界续跑异常")
        raise
    finally:
        if collector:
            collector.close()
