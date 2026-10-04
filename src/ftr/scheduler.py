"""Persistent, single-writer scheduling; source content is never executable input."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import portalocker

from ftr.config import RuntimeSettings, SchedulerSettings
from ftr.diagnostics import recover_interrupted, safe_message, task_report
from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.runtime import Collector, data_lock

SourceId = Literal["mof", "chinatax"]
SOURCES: tuple[SourceId, SourceId] = ("mof", "chinatax")
HEARTBEAT_SECONDS = 15
HEARTBEAT_STALE_SECONDS = 90


def slots(day: date, settings: SchedulerSettings) -> list[datetime]:
    zone = ZoneInfo(settings.timezone)
    result = []
    for value in settings.times:
        local = datetime.combine(day, time.fromisoformat(value), zone)
        utc = local.astimezone(UTC)
        # Skip nonexistent local times on a daylight-saving transition.
        if utc.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            result.append(utc)
    return sorted(result)


def next_slot(after: datetime, settings: SchedulerSettings) -> datetime:
    after = after.astimezone(UTC)
    day = after.astimezone(ZoneInfo(settings.timezone)).date()
    for offset in range(4):
        for slot in slots(day + timedelta(days=offset), settings):
            if slot > after:
                return slot
    raise ValueError("无法确定下次计划时间")


def latest_slot(now: datetime, settings: SchedulerSettings) -> datetime:
    now = now.astimezone(UTC)
    day = now.astimezone(ZoneInfo(settings.timezone)).date()
    for offset in range(4):
        available = [s for s in slots(day - timedelta(days=offset), settings) if s <= now]
        if available:
            return max(available)
    raise ValueError("无法确定最近计划时间")


def window(at: datetime, settings: SchedulerSettings) -> dict:
    end = at.astimezone(ZoneInfo(settings.timezone)).date()
    return {
        "date_from": (end - timedelta(days=settings.lookback_days - 1)).isoformat(),
        "date_to": end.isoformat(),
        "slot": at.astimezone(UTC).isoformat(),
    }


def preview(settings: SchedulerSettings, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    following = next_slot(now, settings)
    upcoming = []
    cursor = now
    for _ in range(4):
        cursor = next_slot(cursor, settings)
        upcoming.append({"at": cursor.isoformat(), "window": window(cursor, settings)})
    return {
        "upcoming": upcoming,
        "settings": settings.model_dump(mode="json"),
        "next_run_at": following.isoformat() if settings.enabled else None,
        "next_window": window(following, settings),
        "startup_window": window(now, settings),
        "sources": list(SOURCES),
    }


def initial_state() -> dict:
    return {
        "active_task_id": None,
        "prepared_request": None,
        "pending_window": None,
        "paused_reason": None,
        "retry_count": 0,
        "no_progress_count": 0,
        "next_attempt_at": None,
        "last_task_id": None,
        "last_result": None,
    }


def read_states(db: sqlite3.Connection) -> dict[str, dict]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_sources'").fetchone():
        return {}
    return {
        row[0]: json.loads(row[1])
        for row in db.execute("SELECT source_id,state_json FROM scheduler_sources")
    }


def save_state(db: sqlite3.Connection, source: str, state: dict) -> None:
    db.execute(
        "INSERT OR REPLACE INTO scheduler_sources VALUES(?,?)",
        (source, json.dumps(state, ensure_ascii=False)),
    )
    db.commit()


def status(
    root: Path, settings: SchedulerSettings | None = None, now: datetime | None = None
) -> dict:
    """Read-only, including absent/old databases. Heartbeat is ephemeral, not backup state."""
    now = now or datetime.now(UTC)
    config = settings or SchedulerSettings()
    result = {
        **preview(config, now),
        "configured_settings": config.model_dump(mode="json"),
        "config_source": "current",
        "heartbeat_fresh": False,
        "heartbeat": None,
        "state": "NOT_STARTED",
        "sources": [],
    }
    database = root / "database.sqlite3"
    if database.is_file():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_meta'").fetchone():
                meta = db.execute("SELECT * FROM scheduler_meta WHERE id=1").fetchone()
                if meta:
                    config = SchedulerSettings.model_validate_json(meta["config_json"])
                    result.update({k: v for k, v in preview(config, now).items() if k != "sources"})
                    result["config_source"] = "last_scheduler_run"
                    result["last_check_at"] = meta["last_check"]
                for source, state in read_states(db).items():
                    task_id = state["active_task_id"] or state["last_task_id"]
                    report = task_report(db, task_id) if task_id else None
                    result["sources"].append(
                        {
                            "source_id": source,
                            **state,
                            "report": report,
                            "next_step": "处理故障后执行 scheduler resume --source " + source
                            if state["paused_reason"]
                            else "按计划自动获取与补漏；待复核资料按现有门禁复核",
                        }
                    )
    try:
        heartbeat = json.loads((root / ".scheduler-heartbeat.json").read_text(encoding="utf-8"))
        age = (now - datetime.fromisoformat(heartbeat["at"])).total_seconds()
        result["heartbeat"] = heartbeat
        result["heartbeat_fresh"] = 0 <= age <= HEARTBEAT_STALE_SECONDS and heartbeat["running"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result["state"] = (
        "RUNNING"
        if result["heartbeat_fresh"]
        else "STOPPED_OR_STALE"
        if result.get("last_check_at")
        else "NOT_STARTED"
    )
    from ftr.management import effective_plan

    persisted = effective_plan(root, settings)
    if persisted["revision"]:
        config = SchedulerSettings.model_validate(persisted["settings"])
        result.update({k: v for k, v in preview(config, now).items() if k != "sources"})
        result.update({k: v for k, v in persisted.items() if k != "settings"})
    result["config_matches"] = result["settings"] == result["configured_settings"]
    return result


def progress(repo: Repository, task_id: str, source: str) -> tuple:
    run = repo.source_run(task_id, source)
    counts = tuple(
        tuple(r)
        for r in repo.db.execute(
            "SELECT state,COUNT(*) FROM discovered WHERE task_id=? GROUP BY state ORDER BY state",
            (task_id,),
        )
    )
    attachments = tuple(
        tuple(r)
        for r in repo.db.execute(
            "SELECT state,COUNT(*) FROM attachment_work WHERE task_id=? GROUP BY state ORDER BY state",
            (task_id,),
        )
    )
    return (
        run["pages_count"],
        run["documents_count"],
        run["bytes_count"],
        run["discovery_done"],
        counts,
        attachments,
    )


class Scheduler:
    def __init__(
        self,
        settings: RuntimeSettings,
        *,
        collector_factory=Collector,
        stop_event: threading.Event | None = None,
    ):
        self.settings = settings
        self.root = settings.data_dir
        self.config = settings.scheduler
        self.collector_factory = collector_factory
        self.stop = stop_event if stop_event is not None else threading.Event()
        self.activity: dict = {"state": "STARTING"}
        self.lock_retry_at: datetime | None = None

    def _plan(self, repo: Repository, now: datetime, *, commit: bool = True) -> dict[str, dict]:
        meta = repo.db.execute("SELECT * FROM scheduler_meta WHERE id=1").fetchone()
        states = read_states(repo.db)
        if meta is None:
            due: dict | None = window(
                now, self.config
            )  # Explicitly enabling starts with today's 30-day window.
        else:
            earliest = next_slot(datetime.fromisoformat(meta["last_check"]), self.config)
            due = window(earliest, self.config) if earliest <= now else None
            if due:
                due["date_to"] = window(latest_slot(now, self.config), self.config)["date_to"]
        if (
            meta is not None
            and due is None
            and meta["config_json"] == self.config.model_dump_json()
        ):
            return states
        for source in SOURCES:
            state = states.setdefault(source, initial_state())
            if due:
                pending = state["pending_window"]
                state["pending_window"] = (
                    {
                        **due,
                        "date_from": min(due["date_from"], pending["date_from"]),
                        "date_to": max(due["date_to"], pending["date_to"]),
                    }
                    if pending
                    else dict(due)
                )
            repo.db.execute(
                "INSERT OR REPLACE INTO scheduler_sources VALUES(?,?)",
                (source, json.dumps(state, ensure_ascii=False)),
            )
        last_check = max(now.isoformat(), meta["last_check"]) if meta else now.isoformat()
        repo.db.execute(
            "INSERT OR REPLACE INTO scheduler_meta VALUES(1,?,?,?)",
            (self.config.model_dump_json(), last_check, meta["last_source"] if meta else None),
        )
        if commit:
            repo.db.commit()
        return states

    def tick(self, now: datetime | None = None) -> dict:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if not self.config.enabled:
            database = self.root / "database.sqlite3"
            if not database.exists():
                return {"state": "DISABLED"}
            with closing(Repository(self.root, readonly=True)) as reader:
                if not any(state.get("explicit_run") for state in read_states(reader.db).values()):
                    from ftr.management import effective_plan

                    if not effective_plan(self.root, self.config)["settings"]["enabled"]:
                        return {"state": "DISABLED"}
        if self.stop.is_set():
            return {"state": "DISABLED" if not self.config.enabled else "STOPPED"}
        if self.lock_retry_at and now < self.lock_retry_at:
            return {"state": "DATA_LOCKED", "next_attempt_at": self.lock_retry_at.isoformat()}
        lock = data_lock(self.root)
        try:
            lock.__enter__()
        except RuntimeError as exc:
            if "数据目录正由另一宿主使用" not in str(exc):
                raise
            self.lock_retry_at = now + timedelta(seconds=60)
            self.activity = {
                "state": "DATA_LOCKED",
                "next_attempt_at": self.lock_retry_at.isoformat(),
            }
            return {"state": "DATA_LOCKED", "next_attempt_at": self.lock_retry_at.isoformat()}
        try:
            self.lock_retry_at = None
            with closing(Repository(self.root)) as repo:
                recover_interrupted(repo)
                from ftr.rule_repair import recover

                recover(self.root, repo)
                from ftr.management import effective_plan

                persisted = effective_plan(self.root, self.config)
                self.config = SchedulerSettings.model_validate(persisted["settings"])
                self.settings = self.settings.model_copy(update={"scheduler": self.config})
                states = self._plan(repo, now) if self.config.enabled else read_states(repo.db)
                if not states:
                    return {"state": "DISABLED"}
                repo.db.execute(
                    "INSERT OR IGNORE INTO scheduler_meta VALUES(1,?,?,NULL)",
                    (self.config.model_dump_json(), now.isoformat()),
                )
                repo.db.commit()
                meta = repo.db.execute(
                    "SELECT last_source FROM scheduler_meta WHERE id=1"
                ).fetchone()
                order = list(SOURCES)
                if meta[0] == order[0]:
                    order.reverse()
                for source in order:
                    state = states.get(source, initial_state())
                    if not self.config.enabled and not state.get("explicit_run"):
                        continue
                    if state["paused_reason"] or state.get("user_paused"):
                        continue
                    if (
                        state["next_attempt_at"]
                        and datetime.fromisoformat(state["next_attempt_at"]) > now
                    ):
                        continue
                    if not (
                        state["active_task_id"]
                        or state["prepared_request"]
                        or state["pending_window"]
                    ):
                        continue
                    self._batch(repo, source, state, now)
                    repo.db.execute("UPDATE scheduler_meta SET last_source=? WHERE id=1", (source,))
                    repo.db.commit()
                    self.activity = {"state": "IDLE", "last_source": source}
                    return {"state": "BATCH_FINISHED", "source_id": source, **state}
                self.activity = {"state": "IDLE"}
                return {"state": "IDLE"}
        finally:
            lock.__exit__(None, None, None)

    def _batch(self, repo: Repository, source: SourceId, state: dict, now: datetime) -> None:
        if not state["active_task_id"]:
            if state["prepared_request"] is None:
                pending = state["pending_window"]
                request = TaskRequest(
                    source_ids=[source],
                    date_from=pending["date_from"],
                    date_to=pending["date_to"],
                    mode="rescan",
                    date_basis="source_listing",
                    max_pages=self.settings.collection.max_pages,
                    max_documents=self.settings.collection.max_documents,
                    idempotency_key=f"scheduler:{source}:{pending['slot']}",
                )
                state["prepared_request"] = request.model_dump(mode="json")
                state["pending_window"] = None
                save_state(repo.db, source, state)
            # A crash after create_task commits reuses exactly this saved request and key.
            task_id = repo.create_task(TaskRequest.model_validate(state["prepared_request"]))
            state["active_task_id"] = task_id
            save_state(repo.db, source, state)
        task_id = state["active_task_id"]
        task = repo.task(task_id)
        previous_reason = state["paused_reason"]
        if task["pause_requested"] or task["cancel_requested"] or task["state"] == "CANCELLED":
            state["paused_reason"] = (
                "MANUAL_CANCEL"
                if task["cancel_requested"] or task["state"] == "CANCELLED"
                else "MANUAL_PAUSE"
            )
            if state["paused_reason"] != previous_reason:
                state["management_revision"] = state.get("management_revision", 0) + 1
            save_state(repo.db, source, state)
            return
        self.activity = {"state": "COLLECTING", "source_id": source, "task_id": task_id}
        before = progress(repo, task_id, source)
        collector = None
        try:
            collector = self.collector_factory(self.root, self.settings, stop_event=self.stop)
            result = collector.resume(task_id)
            report = task_report(repo.db, task_id)
            state["last_result"] = {
                "at": now.isoformat(),
                "status": result.status,
                "completion": report["completion"],
                "stop_reasons": report["stop_reasons"],
            }
            self._outcome(state, report, now, progress(repo, task_id, source) != before)
        except Exception as exc:  # noqa: BLE001 - unknown failures preserve checkpoints and pause
            state["paused_reason"] = "EXECUTION_ERROR"
            state["last_result"] = {
                "at": now.isoformat(),
                "status": "FAILED",
                "message": safe_message(exc),
            }
        finally:
            if collector is not None:
                collector.close()
        if state["paused_reason"] != previous_reason:
            state["management_revision"] = state.get("management_revision", 0) + 1
        state["next_attempt_at"] = (
            state["next_attempt_at"]
            or (now + timedelta(seconds=self.config.batch_interval_seconds)).isoformat()
        )
        save_state(repo.db, source, state)

    def _outcome(self, state: dict, report: dict, now: datetime, advanced: bool) -> None:
        state["next_attempt_at"] = None
        stops = report["stop_reasons"]
        if report["state"] == "CANCELLED" or "CANCEL_REQUESTED" in stops:
            state["paused_reason"] = "MANUAL_CANCEL"
            return
        if "PAUSE_REQUESTED" in stops:
            state["paused_reason"] = "MANUAL_PAUSE"
            return
        if "INTERRUPTED" in stops:
            state["no_progress_count"] = 0
            return
        active = [
            f
            for f in report["failures"]
            if not f["resolved"] and f["stage"] != "attachment_extraction"
        ]
        fatal = [f for f in active if f["category"] != "TRANSIENT_NETWORK"]
        if fatal:
            state["paused_reason"] = fatal[0]["category"]
        elif active:
            delays = self.config.retry_delays_seconds
            if state["retry_count"] >= len(delays):
                state["paused_reason"] = "RETRIES_EXHAUSTED"
            else:
                delay = delays[state["retry_count"]]
                state["retry_count"] += 1
                state["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat()
        elif (
            report["completion"]["coverage"] == "COMPLETE"
            and report["completion"]["downloads"] == "COMPLETE"
        ):
            state["last_task_id"] = state["active_task_id"]
            state["active_task_id"] = state["prepared_request"] = None
            state["retry_count"] = state["no_progress_count"] = 0
            if not state["pending_window"]:
                state["explicit_run"] = False
        else:
            state["retry_count"] = 0
            state["no_progress_count"] = 0 if advanced else state["no_progress_count"] + 1
            if state["no_progress_count"] >= 3:
                state["paused_reason"] = "NO_PROGRESS"


def resume_source(root: Path, source: str) -> dict:
    if source not in SOURCES or not (root / "database.sqlite3").is_file():
        raise ValueError("该来源尚无定时计划")
    with closing(Repository(root, readonly=True)) as reader:
        if not reader.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='scheduler_sources'"
        ).fetchone():
            raise ValueError("该来源尚无定时计划")
        if source not in read_states(reader.db):
            raise ValueError("该来源尚无定时计划")
    with data_lock(root), closing(Repository(root)) as repo:
        states = read_states(repo.db)
        if source not in states:
            raise ValueError("该来源尚无定时计划")
        state = states[source]
        if state["active_task_id"]:
            task = repo.task(state["active_task_id"])
            if task["cancel_requested"] or task["state"] == "CANCELLED":
                state["last_task_id"] = state["active_task_id"]
                state["active_task_id"] = state["prepared_request"] = None
            elif task["pause_requested"]:
                raise ValueError("任务由人工暂停；先明确 task resume 原任务，再恢复定时来源")
        state.update(
            paused_reason=None,
            user_paused=False,
            retry_count=0,
            no_progress_count=0,
            next_attempt_at=None,
            management_revision=state.get("management_revision", 0) + 1,
        )
        save_state(repo.db, source, state)
        repo.audit(state["active_task_id"], "scheduler_source_resumed", {"source_id": source})
        return {"source_id": source, **state}


def run_scheduler(settings: RuntimeSettings) -> dict:
    from ftr.management import effective_plan

    settings = settings.model_copy(
        update={
            "scheduler": SchedulerSettings.model_validate(
                effective_plan(settings.data_dir, settings.scheduler)["settings"]
            )
        }
    )
    if not settings.scheduler.enabled:
        return {"state": "DISABLED"}
    root = settings.data_dir
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".scheduler.lock"
    if lock_path.is_symlink():
        raise ValueError("调度锁路径无效")
    with portalocker.Lock(str(lock_path), timeout=0):
        stop = threading.Event()
        scheduler = Scheduler(settings, stop_event=stop)
        previous = {}
        instance_id = uuid4().hex
        heartbeat_path = root / ".scheduler-heartbeat.json"
        temporary = root / f".scheduler-heartbeat-{instance_id}.tmp"

        def heartbeat(running=True):
            temporary.write_text(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "running": running,
                        "pid": os.getpid(),
                        "instance_id": instance_id,
                        "settings": settings.scheduler.model_dump(mode="json"),
                        "activity": scheduler.activity,
                    }
                ),
                encoding="utf-8",
            )
            temporary.replace(heartbeat_path)

        def heartbeats():
            while not stop.wait(HEARTBEAT_SECONDS):
                heartbeat()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, lambda *_: stop.set())
        heartbeat()
        reporter = threading.Thread(target=heartbeats, daemon=True)
        reporter.start()
        try:
            while not stop.is_set():
                result = scheduler.tick()
                if result["state"] != "IDLE":
                    print(
                        json.dumps({"scheduler": result}, ensure_ascii=False),
                        file=sys.stderr,
                        flush=True,
                    )
                stop.wait(1)
        finally:
            stop.set()
            reporter.join(timeout=2)
            heartbeat(False)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return {"state": "STOPPED"}
