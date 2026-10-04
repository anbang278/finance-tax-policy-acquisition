"""Constrained persistent commands and human review. HTTP never writes business SQLite."""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import portalocker
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ftr.config import RuntimeSettings, SchedulerSettings
from ftr.diagnostics import safe_message
from ftr.models import DecisionRequest, DocumentRecord, SemanticDecision, digest
from ftr.repository import Repository
from ftr.rules import atomic_json
from ftr.runtime import BudgetReached, data_lock
from ftr.scheduler import SOURCES, initial_state, read_states, window


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    action: Literal[
        "plan.save",
        "plan.enable",
        "plan.disable",
        "plan.now",
        "source.pause",
        "source.resume",
        "review.draft",
        "review.submit",
        "review.reopen",
        "review.recover",
    ]
    expected_revision: int = Field(default=0, ge=0, strict=True)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def constrained(self):
        allowed = {
            "plan.save": set(SchedulerSettings.model_fields),
            "plan.enable": set(),
            "plan.disable": set(),
            "plan.now": {"sources", "date_from", "date_to", "slot"},
            "source.pause": {"source_id"},
            "source.resume": {"source_id"},
            "review.draft": {"record_id", "input_digest", "result", "reasons", "evidence_ids"},
            "review.submit": {"record_id", "input_digest", "result", "reasons", "evidence_ids"},
            "review.reopen": {"record_id", "input_digest"},
            "review.recover": {"record_id", "input_digest"},
        }
        if set(self.payload) - allowed[self.action]:
            raise ValueError("操作包含未允许的参数")
        if self.action == "plan.save":
            SchedulerSettings.model_validate(self.payload)
        if self.action.startswith("source.") and self.payload.get("source_id") not in SOURCES:
            raise ValueError("来源不在登记范围内")
        if self.action.startswith("review."):
            if not isinstance(self.payload.get("record_id"), str) or not self.payload.get(
                "input_digest"
            ):
                raise ValueError("复核操作须包含资料与摘要")
            if self.action in ("review.submit", "review.draft"):
                if self.payload.get("result") not in ("PASS", "REJECT", "UNCERTAIN"):
                    raise ValueError("结论无效")
                reasons = self.payload.get("reasons")
                evidence = self.payload.get("evidence_ids")
                if not isinstance(reasons, list) or not all(isinstance(x, str) for x in reasons):
                    raise ValueError("理由无效")
                if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
                    raise ValueError("证据无效")
                if self.action == "review.submit" and not any(x.strip() for x in reasons):
                    raise ValueError("提交结论必须填写理由")
        if self.action == "plan.now":
            from ftr.models import TaskRequest

            sources = self.payload.get("sources")
            TaskRequest(
                source_ids=sources,
                date_from=self.payload.get("date_from"),
                date_to=self.payload.get("date_to"),
            )
            if datetime.fromisoformat(self.payload["slot"]).tzinfo is None:
                raise ValueError("本轮时点须包含时区")
        return self


class Conflict(ValueError):
    pass


def _table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())


def effective_plan(root: Path, defaults: SchedulerSettings | None = None) -> dict:
    result = {
        "revision": 0,
        "applied_revision": 0,
        "config_source": "yaml_default",
        "settings": (defaults or SchedulerSettings()).model_dump(mode="json"),
    }
    completed = set()
    if (root / "database.sqlite3").exists():
        with closing(Repository(root, readonly=True)) as repo:
            if _table(repo.db, "managed_plan"):
                row = repo.db.execute("SELECT * FROM managed_plan WHERE id=1").fetchone()
                if row:
                    result.update(
                        revision=row["revision"],
                        applied_revision=row["applied_revision"],
                        config_source="persistent_local_plan",
                        settings=json.loads(row["settings_json"]),
                    )
            if _table(repo.db, "management_operations"):
                completed = {
                    row[0] for row in repo.db.execute("SELECT id FROM management_operations")
                }
    result["saved_revision"] = result["revision"]
    result["pending_operation_id"] = None
    for path in sorted(
        (root / "commands").glob("*.json"), key=lambda p: (p.stat().st_mtime_ns, p.name)
    ):
        if path.is_symlink() or path.stem in completed:
            continue
        command = Command.model_validate_json(path.read_text())
        if (
            command.action in ("plan.save", "plan.enable", "plan.disable")
            and command.expected_revision == result["revision"]
        ):
            result["saved_revision"] = command.expected_revision + 1
            result["pending_operation_id"] = command.id
            break
    return result


def preflight(root: Path, command: Command) -> None:
    """Early HTTP conflict detection; the writer repeats validation at its checkpoint."""
    with closing(Repository(root, readonly=True)) as repo:
        db = repo.db
        if _table(db, "management_operations"):
            old = db.execute(
                "SELECT command_json FROM management_operations WHERE id=?", (command.id,)
            ).fetchone()
            if old:
                if Command.model_validate_json(old[0]) != command:
                    raise Conflict("相同操作 ID 对应不同输入")
                return
        if command.action.startswith("plan."):
            plan = (
                db.execute("SELECT revision FROM managed_plan WHERE id=1").fetchone()
                if _table(db, "managed_plan")
                else None
            )
            revision = plan[0] if plan else 0
        elif command.action.startswith("source."):
            revision = (
                read_states(db).get(command.payload["source_id"], {}).get("management_revision", 0)
            )
        else:
            row = db.execute(
                "SELECT * FROM documents WHERE id=?", (command.payload["record_id"],)
            ).fetchone()
            if not row:
                raise ValueError("资料不存在")
            item = _review_item(db, row)
            revision = item["revision"]
            if command.action != "review.draft" and (
                not item["latest"] or item["input_digest"] != command.payload["input_digest"]
            ):
                raise Conflict("资料已有新版本或摘要变化；草稿保留，请重新检查")
        if revision != command.expected_revision:
            raise Conflict("预期版本过期；请刷新后重试")


def enqueue(root: Path, command: Command) -> dict:
    directory = root / "commands"
    if directory.is_symlink():
        raise ValueError("命令目录不得为符号链接")
    directory.mkdir(mode=0o700, exist_ok=True)
    with portalocker.Lock(str(root / ".commands.lock"), timeout=1):
        path = directory / (command.id + ".json")
        if path.is_symlink():
            raise ValueError("命令路径无效")
        if path.exists():
            existing = Command.model_validate_json(path.read_text())
            if existing != command:
                raise Conflict("相同操作 ID 对应不同输入")
        else:
            atomic_json(path, command.model_dump(mode="json"))
            os.chmod(path, 0o600)
    return {"id": command.id, "state": "QUEUED"}


def operations(root: Path, *, limit: int | None = 100) -> list[dict]:
    saved = {}
    if (root / "database.sqlite3").exists():
        with closing(Repository(root, readonly=True)) as repo:
            if _table(repo.db, "management_operations"):
                for row in repo.db.execute("SELECT * FROM management_operations"):
                    saved[row["id"]] = {
                        "id": row["id"],
                        "state": row["state"],
                        "action": json.loads(row["command_json"])["action"],
                        "result": json.loads(row["result_json"]),
                        "at": row["completed_at"],
                    }
    pending = []
    for path in (root / "commands").glob("*.json"):
        if path.is_symlink():
            continue
        command = Command.model_validate_json(path.read_text())
        if command.id not in saved:
            pending.append(
                {
                    "id": command.id,
                    "state": "QUEUED",
                    "action": command.action,
                    "at": datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                }
            )
    items = sorted([*saved.values(), *pending], key=lambda x: x["at"], reverse=True)
    return items[:limit] if limit is not None else items


def _latest(db, row):
    return (
        db.execute(
            "SELECT id FROM documents WHERE source_id=? AND canonical_url=? "
            "ORDER BY source_version DESC, extraction_version DESC LIMIT 1",
            (row["source_id"], row["canonical_url"]),
        ).fetchone()[0]
        == row["id"]
    )


def review_detail(root: Path, record_id: str) -> dict:
    with closing(Repository(root, readonly=True)) as repo:
        row = repo.db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise KeyError("资料不存在")
        item = _review_item(repo.db, row)
        item["item"] = json.loads(row["record_json"])
        item["history"] = (
            [
                dict(r)
                | {
                    "snapshot_json": json.loads(r["snapshot_json"]),
                    "result_json": json.loads(r["result_json"]),
                }
                for r in repo.db.execute(
                    "SELECT * FROM review_rounds WHERE record_id=? ORDER BY round", (record_id,)
                )
            ]
            if _table(repo.db, "review_rounds")
            else []
        )
        return item


def _review_item(db, row):
    record = DocumentRecord.model_validate_json(row["record_json"])
    ws = (
        db.execute("SELECT * FROM review_workspace WHERE record_id=?", (row["id"],)).fetchone()
        if _table(db, "review_workspace")
        else None
    )
    done = db.execute(
        "SELECT result_json FROM decisions WHERE record_id=? AND state='DONE' ORDER BY rowid DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    latest = _latest(db, row)
    result = json.loads(done[0])["result"] if done else None
    missing = [a for a in record.attachments if a.download_state != "saved"]
    category = (
        ("further" if result == "UNCERTAIN" else "done")
        if done and (not ws or ws["submitted"])
        else "done"
        if not ws and record.quality_state in ("validated", "rejected")
        else "limited"
        if record.limitations
        or not record.body_text.strip()
        or missing
        or (record.quality_state == "quarantined" and (not ws or ws["submitted"]))
        else "pending"
    )
    can_pass = latest and bool(record.body_text.strip()) and not record.limitations and not missing
    return {
        "record_id": row["id"],
        "title": record.title,
        "task_id": row["task_id"],
        "category": category,
        "latest": latest,
        "input_digest": digest(row["record_json"].encode()),
        "revision": ws["revision"] if ws else 0,
        "round": ws["open_round"] if ws else 1,
        "submitted": bool(ws["submitted"])
        if ws
        else bool(done) or record.quality_state in ("validated", "rejected"),
        "draft": json.loads(ws["draft_json"]) if ws and ws["draft_json"] else None,
        "can_pass": can_pass,
        "can_recover": latest and (bool(missing) or not record.body_text.strip()),
        "evidence_ids": [
            record.evidence_id,
            *[a.evidence_id for a in record.attachments if a.evidence_id],
        ],
        "limitations": record.limitations,
        "quality_state": record.quality_state,
    }


def review_queue(root: Path, category="pending") -> list[dict]:
    with closing(Repository(root, readonly=True)) as repo:
        items = [
            _review_item(repo.db, row)
            for row in repo.db.execute("SELECT * FROM documents ORDER BY rowid DESC")
            if _latest(repo.db, row)
        ]
    return [i for i in items if category == "all" or i["category"] == category]


def _state_write(db, source, state):
    db.execute("INSERT OR REPLACE INTO scheduler_sources VALUES(?,?)", (source, json.dumps(state)))


def apply_command(repo: Repository, command: Command, settings: RuntimeSettings) -> dict:
    db, payload = repo.db, command.payload
    if command.action.startswith("plan."):
        row = db.execute("SELECT * FROM managed_plan WHERE id=1").fetchone()
        revision = row["revision"] if row else 0
        if revision != command.expected_revision:
            raise Conflict("计划版本已变化，请刷新后重试")
        config = (
            SchedulerSettings.model_validate_json(row["settings_json"])
            if row
            else settings.scheduler.model_copy(deep=True)
        )
        if command.action == "plan.now":
            # Window is previewed by the server; do not accept a wider arbitrary range.
            expected = window(datetime.fromisoformat(payload["slot"]), config)
            if {k: payload[k] for k in expected} != expected:
                raise Conflict("本轮窗口与当前计划不匹配，请重新预览")
            if (
                abs((datetime.now(UTC) - datetime.fromisoformat(payload["slot"])).total_seconds())
                > 900
            ):
                raise Conflict("本轮预览已过期")
            for source in payload["sources"]:
                state = read_states(db).get(source, initial_state())
                pending = state["pending_window"]
                state["pending_window"] = {
                    **expected,
                    "date_from": min(expected["date_from"], pending["date_from"])
                    if pending
                    else expected["date_from"],
                    "date_to": max(expected["date_to"], pending["date_to"])
                    if pending
                    else expected["date_to"],
                }
                state["explicit_run"] = True
                _state_write(db, source, state)
            return {"window": expected, "sources": payload["sources"]}
        previous_config = config.model_copy(deep=True)
        old_enabled = config.enabled
        if command.action == "plan.save":
            config = SchedulerSettings.model_validate(payload)
        else:
            config.enabled = command.action == "plan.enable"
        if not config.enabled:
            for source, state in read_states(db).items():
                state["explicit_run"] = False
                _state_write(db, source, state)
        revision += 1
        db.execute(
            "INSERT OR REPLACE INTO managed_plan VALUES(1,?,?,?)",
            (revision, revision, config.model_dump_json()),
        )
        # Preserve backlog collected before a plan edit, including long downtime.
        if old_enabled:
            from ftr.scheduler import Scheduler

            scheduler = Scheduler(settings.model_copy(update={"scheduler": previous_config}))
            # _plan normally commits; here defer it to the operation's transaction.
            scheduler._plan(repo, datetime.now(UTC), commit=False)
        if config.enabled and not old_enabled:
            due = window(datetime.now(UTC), config)
            for source in SOURCES:
                state = read_states(db).get(source, initial_state())
                pending = state["pending_window"]
                state["pending_window"] = {
                    **due,
                    "date_from": min(due["date_from"], pending["date_from"])
                    if pending
                    else due["date_from"],
                    "date_to": max(due["date_to"], pending["date_to"])
                    if pending
                    else due["date_to"],
                }
                _state_write(db, source, state)
        # Disabled periods are not missed schedules. Retain pending/active windows.
        db.execute(
            "INSERT INTO scheduler_meta VALUES(1,?,?,NULL) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,last_check=excluded.last_check",
            (config.model_dump_json(), datetime.now(UTC).isoformat()),
        )
        return {"revision": revision, "applied_revision": revision}
    if command.action.startswith("source."):
        source = payload["source_id"]
        state = read_states(db).get(source, initial_state())
        revision = state.get("management_revision", 0)
        if revision != command.expected_revision:
            raise Conflict("来源状态已变化")
        if command.action == "source.pause":
            state["user_paused"] = True
        else:
            if state["active_task_id"]:
                task = repo.task(state["active_task_id"])
                if (
                    task["pause_requested"]
                    or task["cancel_requested"]
                    or task["state"] == "CANCELLED"
                ):
                    raise Conflict("原任务已被人工暂停或取消；不能从页面自动恢复")
            state.update(
                user_paused=False,
                paused_reason=None,
                retry_count=0,
                no_progress_count=0,
                next_attempt_at=None,
            )
        state["management_revision"] = revision + 1
        _state_write(db, source, state)
        return {"revision": revision + 1}
    row = db.execute("SELECT * FROM documents WHERE id=?", (payload["record_id"],)).fetchone()
    if not row:
        raise ValueError("资料不存在")
    item = _review_item(db, row)
    if item["revision"] != command.expected_revision:
        raise Conflict("复核表单版本已变化；草稿保留，请刷新")
    # Drafts remain writable for a stale version, retaining their original digest.
    if command.action != "review.draft" and (
        not item["latest"] or item["input_digest"] != payload["input_digest"]
    ):
        raise Conflict("资料已有新版本或摘要已变化；草稿保留，请重新检查")
    if command.action == "review.recover":
        if not item["can_recover"]:
            raise ValueError("没有可补取的缺失正文或附件；未支持解析需另行核查")
        return {"record_id": row["id"], "recovery": "READY"}
    revision, round_number = item["revision"] + 1, item["round"]
    if command.action == "review.reopen":
        if not item["submitted"]:
            raise Conflict("当前轮尚未提交")
        round_number += 1
        db.execute(
            "INSERT OR REPLACE INTO review_workspace VALUES(?,?,?,NULL,0)",
            (row["id"], revision, round_number),
        )
        return {"revision": revision, "round": round_number}
    if item["submitted"]:
        raise Conflict("结论已提交，请先开启新一轮复核")
    if command.action == "review.draft":
        db.execute(
            "INSERT OR REPLACE INTO review_workspace VALUES(?,?,?,?,0)",
            (row["id"], revision, round_number, json.dumps(payload, ensure_ascii=False)),
        )
        return {"revision": revision, "draft_saved": True}
    if payload["evidence_ids"] != item["evidence_ids"]:
        raise ValueError("须确认当前资料的全部登记证据")
    if payload["result"] == "PASS" and not item["can_pass"]:
        raise ValueError("内容受限或证据不完整，禁止通过")
    for evidence_id in item["evidence_ids"]:
        evidence_row = db.execute(
            "SELECT metadata_json FROM evidence WHERE id=?", (evidence_id,)
        ).fetchone()
        if not evidence_row:
            if payload["result"] == "PASS":
                raise ValueError("登记证据不存在")
            continue
        evidence = json.loads(evidence_row[0])
        path = settings.data_dir / "evidence" / evidence["relative_path"]
        if (
            not path.resolve().is_relative_to((settings.data_dir / "evidence").resolve())
            or path.is_symlink()
            or not path.is_file()
            or digest(path.read_bytes()) != evidence["sha256"]
        ) and payload["result"] == "PASS":
            raise ValueError("证据文件或摘要校验失败")
    record = DocumentRecord.model_validate_json(row["record_json"])
    request_row = db.execute(
        "SELECT * FROM decisions WHERE record_id=? AND state='PENDING' ORDER BY rowid DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    if (
        request_row
        and json.loads(request_row["request_json"])["input_digest"] == item["input_digest"]
    ):
        request = DecisionRequest.model_validate_json(request_row["request_json"])
    else:
        request = DecisionRequest(
            decision_id=command.id,
            task_id=row["task_id"],
            record_id=row["id"],
            input_digest=item["input_digest"],
            excerpt=record.body_text[:4000],
            evidence_id=record.evidence_id,
            allowed_actions=["PASS", "REJECT", "UNCERTAIN"]
            if item["can_pass"]
            else ["REJECT", "UNCERTAIN"],
        )
        db.execute(
            "INSERT INTO decisions(id,task_id,record_id,request_json) VALUES(?,?,?,?)",
            (request.decision_id, request.task_id, request.record_id, request.model_dump_json()),
        )
    if payload["result"] not in request.allowed_actions:
        raise ValueError("当前资料不允许该结论")
    decision = SemanticDecision(
        decision_id=request.decision_id,
        input_digest=item["input_digest"],
        result=payload["result"],
        reasons=payload["reasons"],
        evidence_ids=[record.evidence_id],
        model_id="local-human",
    )
    record.quality_state = (
        "validated"
        if decision.result == "PASS"
        else "rejected"
        if decision.result == "REJECT"
        else "quarantined"
    )
    db.execute(
        "UPDATE documents SET record_json=?,quality_state=? WHERE id=?",
        (record.model_dump_json(), record.quality_state, row["id"]),
    )
    db.execute(
        "UPDATE decisions SET result_json=?,state='DONE' WHERE id=?",
        (decision.model_dump_json(), decision.decision_id),
    )
    db.execute(
        "INSERT INTO review_rounds VALUES(?,?,?,?,?,?,?,?)",
        (
            command.id,
            row["id"],
            round_number,
            decision.decision_id,
            row["record_json"],
            json.dumps(payload, ensure_ascii=False),
            "local-human",
            datetime.now(UTC).isoformat(),
        ),
    )
    db.execute(
        "INSERT OR REPLACE INTO review_workspace VALUES(?,?,?,NULL,1)",
        (row["id"], revision, round_number),
    )
    return {"revision": revision, "quality_state": record.quality_state, "round": round_number}


def process_commands(settings: RuntimeSettings, *, recover=None, on_operation=None) -> list[dict]:
    results = []
    with data_lock(settings.data_dir), closing(Repository(settings.data_dir)) as repo:
        for path in sorted(
            (settings.data_dir / "commands").glob("*.json"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
        ):
            if path.is_symlink():
                continue
            command = Command.model_validate_json(path.read_text())
            old = repo.db.execute(
                "SELECT * FROM management_operations WHERE id=?", (command.id,)
            ).fetchone()
            if old:
                if Command.model_validate_json(old["command_json"]) != command:
                    raise Conflict("操作 ID 与历史输入不一致")
                if old["state"] != "RUNNING":
                    continue
                last = json.loads(old["result_json"])
                if datetime.fromisoformat(last["next_attempt_at"]) > datetime.now(UTC):
                    continue
            else:
                last = None
            if on_operation is not None:
                on_operation(command.id, command.action)
            try:
                repo.db.defer_commit = True
                repo.db.execute("BEGIN")
                with repo.db:
                    executable = command
                    if last is not None:
                        executable = command.model_copy(
                            update={
                                "expected_revision": last["next_revision"],
                                "payload": {
                                    "record_id": last["record_id"],
                                    "input_digest": last["next_digest"],
                                },
                            }
                        )
                    result = apply_command(repo, executable, settings)
                    state = "APPLIED"
                    if command.action == "review.recover":
                        # Material writes use existing content idempotence; completion can replay.
                        if recover is None:
                            raise ValueError("补取执行器不可用")
                        try:
                            result = recover(repo, executable.payload["record_id"])
                        except BudgetReached as exc:
                            if exc.code != "INTERRUPTED":
                                raise
                            result = {
                                "record_id": executable.payload["record_id"],
                                "created": False,
                                "recovery_complete": False,
                                "needs_review": True,
                                "warnings": ["检查点已保留"],
                            }
                        if not result.get("recovery_complete", True):
                            if result.get("blocked"):
                                state = "FAILED"
                                result.update(
                                    code="CONTENT_BLOCKED",
                                    message="部分原件已补齐；其余内容受阻，处理后对新版本重新补取",
                                )
                            else:
                                state = "RUNNING"
                                following = repo.db.execute(
                                    "SELECT * FROM documents WHERE id=?", (result["record_id"],)
                                ).fetchone()
                                item = _review_item(repo.db, following)
                                result.update(
                                    next_digest=item["input_digest"],
                                    next_revision=item["revision"],
                                    next_attempt_at=(
                                        datetime.now(UTC)
                                        + timedelta(
                                            seconds=settings.scheduler.batch_interval_seconds
                                        )
                                    ).isoformat(),
                                )
                    repo.db.execute(
                        "INSERT OR REPLACE INTO management_operations VALUES(?,?,?,?,?)",
                        (
                            command.id,
                            command.model_dump_json(),
                            state,
                            json.dumps(result, ensure_ascii=False),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
                repo.db.defer_commit = False
                repo.db.commit()
            except Exception as exc:  # noqa: BLE001 - persist a failed operation without losing its draft
                repo.db.defer_commit = False
                repo.db.rollback()
                with repo.db:
                    result = {
                        "code": "CONFLICT" if isinstance(exc, Conflict) else "INVALID",
                        "message": safe_message(exc),
                    }
                    repo.db.execute(
                        "INSERT OR REPLACE INTO management_operations VALUES(?,?,?,?,?)",
                        (
                            command.id,
                            command.model_dump_json(),
                            "FAILED",
                            json.dumps(result, ensure_ascii=False),
                            datetime.now(UTC).isoformat(),
                        ),
                    )
            finally:
                repo.db.defer_commit = False
                if repo.db.in_transaction:
                    repo.db.rollback()
            results.append({"id": command.id, **result})
    return results
