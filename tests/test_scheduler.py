"""Isolated scheduling clocks, controlled sources and real child processes; no official requests."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import ClassVar

import portalocker
import pytest
from fastapi.testclient import TestClient

from ftr.backup import create_backup, restore_backup
from ftr.cli import parser, run
from ftr.config import RuntimeSettings, SchedulerSettings, load_settings
from ftr.models import OperationResponse, TaskRequest
from ftr.network import BoundedClient, TransientFailure
from ftr.repository import Repository
from ftr.runtime import Collector, data_lock
from ftr.scheduler import (
    Scheduler,
    latest_slot,
    next_slot,
    preview,
    read_states,
    resume_source,
    save_state,
    status,
    window,
)
from ftr.web.app import create_app


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def settings(root, **values):
    return RuntimeSettings(
        data_dir=root,
        network={"proxy_mode": "direct", "interval_seconds": 0},
        scheduler={"enabled": True, **values},
    )


class ControlledCollector:
    """Persist realistic task outcomes without any HTTP/browser use."""

    calls: ClassVar[list[str]] = []
    outcomes: ClassVar[dict[str, str]] = {}

    def __init__(self, root, config, **kwargs):
        self.repo = Repository(root)

    def resume(self, task):
        source = json.loads(self.repo.task(task)["request_json"])["source_ids"][0]
        self.calls.append(source)
        outcome = self.outcomes.get(source, "complete")
        if outcome == "crash":
            raise RuntimeError("unknown password=secret")
        if outcome == "interrupt":
            stops = ["INTERRUPTED"]
        elif outcome in ("network", "restricted", "structure"):
            category = {
                "network": "TRANSIENT_NETWORK",
                "restricted": "ACCESS_RESTRICTED",
                "structure": "STRUCTURE_DRIFT",
            }[outcome]
            self.repo.add_failure(
                task, source, category, {"stage": "discover", "message": "controlled failure"}
            )
            stops = ["SOURCE_FAILURE"]
        else:
            stops = ["BUDGET_REACHED"] if outcome in ("budget", "stalled") else []
        if outcome == "budget":
            self.repo.db.execute(
                "UPDATE source_runs SET pages_count=pages_count+1 WHERE task_id=?", (task,)
            )
        if outcome == "complete":
            self.repo.db.execute("UPDATE source_runs SET discovery_done=1 WHERE task_id=?", (task,))
            self.repo.resolve_failures(task, source, "discover", None)
        self.repo.db.commit()
        self.repo.set_task_state(task, "WAITING_DECISION" if outcome == "complete" else "PARTIAL")
        self.repo.audit(task, "batch_stopped", {"reasons": stops})
        return OperationResponse(
            operation="collect",
            task_id=task,
            status="WAITING_DECISION" if outcome == "complete" else "PARTIAL",
        )

    def close(self):
        self.repo.close()


@pytest.fixture(autouse=True)
def reset_controlled():
    ControlledCollector.calls = []
    ControlledCollector.outcomes = {}


def states(root):
    with closing(Repository(root, readonly=True)) as repo:
        return read_states(repo.db)


def test_schedule_timezone_window_midnight_and_dst():
    config = SchedulerSettings(enabled=True)
    assert next_slot(at("2026-06-30T00:59:59"), config) == at("2026-06-30T01:00:00")
    assert next_slot(at("2026-06-30T01:00:00"), config) == at("2026-06-30T10:00:00")
    assert next_slot(at("2026-06-30T10:00:00"), config) == at("2026-07-01T01:00:00")
    assert latest_slot(at("2026-06-30T00:00:00"), config) == at("2026-06-29T10:00:00")
    assert window(at("2026-06-30T16:00:00"), config)["date_from"] == "2026-06-02"
    dst = SchedulerSettings(timezone="America/New_York", times=["02:30"])
    assert next_slot(at("2026-03-08T06:00:00"), dst) == at("2026-03-09T06:30:00")
    assert preview(config, at("2026-06-30T01:00:00"))["next_window"]["date_to"] == "2026-06-30"


@pytest.mark.parametrize(
    "value",
    [
        {"times": ["9:00"]},
        {"times": ["09:00", "09:00"]},
        {"times": []},
        {"timezone": "bad/zone"},
        {"lookback_days": 0},
        {"batch_interval_seconds": float("nan")},
        {"retry_delays_seconds": [1, 2, 3, 4]},
        {"retry_delays_seconds": [True]},
        {"retry_delays_seconds": [1.5]},
        {"enabled": "yes"},
    ],
)
def test_invalid_config_no_side_effects(tmp_path, value):
    config = tmp_path / "bad.yaml"
    import yaml

    config.write_text(yaml.safe_dump({"scheduler": value}))
    with pytest.raises(ValueError):
        run(
            parser().parse_args(
                ["--config", str(config), "--data-dir", str(tmp_path / "data"), "scheduler", "run"]
            )
        )
    assert not (tmp_path / "data").exists()


def test_disabled_preview_status_no_writes(tmp_path):
    root = tmp_path / "not-created"
    for action in ("preview", "status", "run"):
        run(parser().parse_args(["--data-dir", str(root), "scheduler", action]))
    with pytest.raises(ValueError):
        resume_source(root, "mof")
    assert not root.exists()
    env = load_settings(
        environ={"FTR_SCHEDULER__TIMES": "['08:00']", "FTR_SCHEDULER__ENABLED": "true"}
    )
    assert env.scheduler.times == ["08:00"] and env.scheduler.enabled


def test_round_robin_budget_completion_and_repeated_wake(tmp_path):
    config = settings(tmp_path)
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    ControlledCollector.outcomes["mof"] = "budget"
    scheduler.tick(now)
    scheduler.tick(now)
    assert ControlledCollector.calls == ["mof", "chinatax"]
    scheduler.tick(now)
    assert len(ControlledCollector.calls) == 2
    frozen = states(tmp_path)["mof"]["prepared_request"]
    ControlledCollector.outcomes["mof"] = "complete"
    scheduler.tick(now + timedelta(seconds=61))
    assert states(tmp_path)["mof"]["active_task_id"] is None
    scheduler.tick(at("2026-06-30T10:00:00"))
    scheduler.tick(at("2026-06-30T10:00:00"))
    assert len(ControlledCollector.calls) == 5  # New round despite WAITING_DECISION.
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 4
        assert (
            json.loads(
                repo.task(
                    repo.db.execute(
                        "SELECT id FROM tasks WHERE idempotency_key=?", (frozen["idempotency_key"],)
                    ).fetchone()[0]
                )["request_json"]
            )
            == frozen
        )


def test_downtime_over_30_days_and_pending_merge(tmp_path):
    config = settings(tmp_path)
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    ControlledCollector.outcomes["mof"] = "budget"
    scheduler.tick(at("2026-06-01T01:00:00"))
    frozen = states(tmp_path)["mof"]["prepared_request"]
    Scheduler(config, collector_factory=ControlledCollector).tick(at("2026-08-10T10:00:00"))
    state = states(tmp_path)["mof"]
    assert state["prepared_request"] == frozen
    assert state["pending_window"]["date_from"] == "2026-05-03"
    assert state["pending_window"]["date_to"] == "2026-08-10"
    scheduler.tick(at("2026-08-11T10:00:00"))
    assert states(tmp_path)["mof"]["pending_window"]["date_from"] == "2026-05-03"
    assert states(tmp_path)["mof"]["pending_window"]["date_to"] == "2026-08-11"


@pytest.mark.parametrize(
    "outcome,reason",
    [
        ("restricted", "ACCESS_RESTRICTED"),
        ("structure", "STRUCTURE_DRIFT"),
        ("crash", "EXECUTION_ERROR"),
    ],
)
def test_fatal_source_paused_other_continues(tmp_path, outcome, reason):
    ControlledCollector.outcomes["mof"] = outcome
    scheduler = Scheduler(settings(tmp_path), collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    scheduler.tick(now)
    scheduler.tick(now)
    assert states(tmp_path)["mof"]["paused_reason"] == reason
    assert ControlledCollector.calls == ["mof", "chinatax"]
    assert "secret" not in json.dumps(states(tmp_path))
    resume_source(tmp_path, "mof")
    assert states(tmp_path)["mof"]["paused_reason"] is None


def test_network_three_additional_retries_then_pause(tmp_path):
    ControlledCollector.outcomes["mof"] = "network"
    scheduler = Scheduler(settings(tmp_path), collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    scheduler.tick(now)
    assert states(tmp_path)["mof"]["retry_count"] == 1
    scheduler.tick(now)  # Other source gets its batch.
    scheduler.tick(now + timedelta(seconds=299))
    assert ControlledCollector.calls.count("mof") == 1
    for delay in (300, 900, 3600):
        now += timedelta(seconds=delay)
        scheduler.tick(now)
    assert ControlledCollector.calls.count("mof") == 4
    assert states(tmp_path)["mof"]["paused_reason"] == "RETRIES_EXHAUSTED"


def test_three_stalled_batches_and_interrupt_not_failure(tmp_path):
    ControlledCollector.outcomes["mof"] = "stalled"
    scheduler = Scheduler(settings(tmp_path), collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    scheduler.tick(now)
    scheduler.tick(now)
    scheduler.tick(now + timedelta(seconds=60))
    scheduler.tick(now + timedelta(seconds=120))
    assert states(tmp_path)["mof"]["paused_reason"] == "NO_PROGRESS"
    resume_source(tmp_path, "mof")
    ControlledCollector.outcomes["mof"] = "interrupt"
    scheduler.tick(now + timedelta(seconds=180))
    assert states(tmp_path)["mof"]["paused_reason"] is None
    assert states(tmp_path)["mof"]["no_progress_count"] == 0


@pytest.mark.parametrize("field", ["pause_requested", "cancel_requested"])
def test_manual_flags_and_unowned_tasks_never_resumed(tmp_path, field):
    ControlledCollector.outcomes["mof"] = "budget"
    config = settings(tmp_path)
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    scheduler.tick(now)
    task = states(tmp_path)["mof"]["active_task_id"]
    with closing(Repository(tmp_path)) as repo:
        repo.db.execute(f"UPDATE tasks SET {field}=1 WHERE id=?", (task,))
        repo.db.commit()
        manual = repo.create_task(
            TaskRequest(date_from="2020-01-01", date_to="2020-01-02", source_ids=["mof"])
        )
    scheduler.tick(now)
    scheduler.tick(now + timedelta(seconds=60))
    assert ControlledCollector.calls.count("mof") == 1
    if field == "pause_requested":
        with pytest.raises(ValueError):
            resume_source(tmp_path, "mof")
    else:
        resume_source(tmp_path, "mof")
        assert states(tmp_path)["mof"]["active_task_id"] is None
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.task(manual)["state"] == "CREATED"
        assert repo.task(task)[field] == 1


def test_data_lock_conflict_defers_without_state_or_failure(tmp_path):
    scheduler = Scheduler(settings(tmp_path), collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    with data_lock(tmp_path):
        assert scheduler.tick(now)["state"] == "DATA_LOCKED"
    assert not (tmp_path / "database.sqlite3").exists()
    assert scheduler.tick(now + timedelta(seconds=59))["state"] == "DATA_LOCKED"
    scheduler.tick(now + timedelta(seconds=60))
    assert states(tmp_path)["mof"]["retry_count"] == 0


def test_crash_after_task_create_reuses_frozen_input(tmp_path, monkeypatch):
    import ftr.scheduler as module

    original = module.save_state

    def crash(db, source, state):
        if state["active_task_id"]:
            raise RuntimeError("hard crash before association")
        original(db, source, state)

    monkeypatch.setattr(module, "save_state", crash)
    now = at("2026-06-30T01:00:00")
    with pytest.raises(RuntimeError):
        Scheduler(settings(tmp_path), collector_factory=ControlledCollector).tick(now)
    assert states(tmp_path)["mof"]["active_task_id"] is None
    with closing(Repository(tmp_path, readonly=True)) as repo:
        created = repo.db.execute("SELECT id,request_json,request_digest FROM tasks").fetchone()
    monkeypatch.setattr(module, "save_state", original)
    Scheduler(settings(tmp_path), collector_factory=ControlledCollector).tick(
        now + timedelta(days=40)
    )
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert tuple(
            repo.db.execute("SELECT id,request_json,request_digest FROM tasks").fetchone()
        ) == tuple(created)


def test_rescan_delayed_listing_old_update_and_versions(tmp_path, monkeypatch):
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )
    listing = (
        sample["listing"]
        .replace("2026-09-01", "2026-06-25")
        .replace("countPage = 2", "countPage = 1")
    )
    detail = sample["detail"]
    calls = []
    visible = False

    def get(_, url):
        calls.append(url)
        if url == sample["url"]:
            return (
                (listing if visible else listing.replace("2026-06-25", "2020-01-01")).encode(),
                url,
                "text/html",
            )
        return detail.encode(), url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def factory(root, config, **kwargs):
        return Collector(root, config, proxy=None, **kwargs)

    config = settings(tmp_path)
    scheduler = Scheduler(config, collector_factory=factory)
    scheduler.tick(at("2026-06-29T01:00:00"))
    # Pause unused tax source without launching its browser.
    with closing(Repository(tmp_path)) as repo:
        tax = read_states(repo.db)["chinatax"]
        tax["paused_reason"] = "TEST_NOT_SELECTED"
        save_state(repo.db, "chinatax", tax)
    visible = True
    scheduler.tick(at("2026-06-30T01:00:00"))
    with closing(Repository(tmp_path, readonly=True)) as repo:
        old = dict(repo.db.execute("SELECT * FROM documents").fetchone())
        assert json.loads(old["record_json"])["listing_date"] == "2026-06-25"
    # >14 days old, still inside the 30-day window: recheck and retain new version.
    detail = detail.replace("可信样本正文", "变更后的正文")
    scheduler.tick(at("2026-07-15T01:00:00"))
    scheduler.tick(at("2026-07-15T01:01:01"))  # Pending merged window can follow completion.
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert (
            dict(repo.db.execute("SELECT * FROM documents WHERE id=?", (old["id"],)).fetchone())
            == old
        )
        assert (
            repo.db.execute(
                "SELECT COUNT(*) FROM documents WHERE quality_state='validated'"
            ).fetchone()[0]
            == 0
        )
    scheduler.tick(at("2026-07-15T10:00:00"))
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
    assert len([url for url in calls if url != sample["url"]]) >= 3


def test_status_api_backup_old_db_readonly_and_heartbeat(tmp_path):
    config = settings(tmp_path)
    now = at("2026-06-30T01:00:00")
    Scheduler(config, collector_factory=ControlledCollector).tick(now)
    database = tmp_path / "database.sqlite3"
    before = database.read_bytes()
    heartbeat = {"at": now.isoformat(), "running": True, "pid": 123, "instance_id": "test"}
    (tmp_path / ".scheduler-heartbeat.json").write_text(json.dumps(heartbeat))
    assert status(tmp_path, config.scheduler, now)["heartbeat_fresh"]
    assert not status(tmp_path, config.scheduler, now + timedelta(seconds=91))["heartbeat_fresh"]
    with TestClient(
        create_app(tmp_path, scheduler_settings=config.scheduler), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/scheduler").status_code == 200
        assert client.post("/api/scheduler").status_code == 405
        assert any(item["report"] for item in client.get("/api/scheduler").json()["sources"])
    assert database.read_bytes() == before
    backup = tmp_path.parent / (tmp_path.name + "-backup")
    create_backup(tmp_path, backup)
    target = tmp_path.parent / (tmp_path.name + "-restored")
    restore_backup(backup, target)
    assert states(target) == states(tmp_path)
    assert not (target / ".scheduler-heartbeat.json").exists()
    with sqlite3.connect(database) as db:
        db.execute("DROP TABLE scheduler_sources")
        db.execute("DROP TABLE scheduler_meta")
        db.execute("PRAGMA user_version=2")
    before = database.read_bytes()
    assert status(tmp_path)["sources"] == []
    assert database.read_bytes() == before
    with closing(Repository(tmp_path)) as repo:
        assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 4


def child_environment():
    return {k: v for k, v in os.environ.items() if not k.startswith("FTR_")}


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX SIGTERM semantics; Windows core scheduling tested separately"
)
def test_real_process_singleton_signal_and_hard_exit(tmp_path):
    # Hold the writer lock so the live scheduler cannot issue a source request.
    config = tmp_path / "runtime.yaml"
    data = tmp_path / "中文 数据"
    config.write_text(
        f"data_dir: '{data}'\nscheduler:\n  enabled: true\nnetwork:\n  proxy_mode: direct\n"
    )
    command = [sys.executable, "-m", "ftr.cli", "--config", str(config), "scheduler", "run"]
    with data_lock(data):
        process = subprocess.Popen(
            command,
            env=child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not (data / ".scheduler-heartbeat.json").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert (data / ".scheduler-heartbeat.json").is_file()
            other = subprocess.run(
                command,
                env=child_environment(),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert other.returncode != 0
            process.send_signal(signal.SIGTERM)
            stdout, _stderr = process.communicate(timeout=10)
            assert process.returncode == 0 and json.loads(stdout)["status"] == "STOPPED"
            assert json.loads((data / ".scheduler-heartbeat.json").read_text())["running"] is False
            assert not (data / "database.sqlite3").exists()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        hard = subprocess.Popen(
            command, env=child_environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if json.loads((data / ".scheduler-heartbeat.json").read_text())["running"]:
                    break
                time.sleep(0.05)
            hard.kill()
            hard.communicate(timeout=10)
        finally:
            if hard.poll() is None:
                hard.kill()
                hard.communicate()
        with portalocker.Lock(str(data / ".scheduler.lock"), timeout=0):
            pass  # OS lock released on hard exit; never delete the file.


def test_stop_event_collector_checkpoint_preserves_task(tmp_path):
    stop = threading.Event()
    with closing(Collector(tmp_path, settings(tmp_path), proxy=None, stop_event=stop)) as collector:
        task = collector.repo.create_task(
            TaskRequest(source_ids=["mof"], date_from="2026-06-01", date_to="2026-06-30")
        )
        stop.set()
        response = collector.resume(task)
        assert response.status == "PARTIAL"
        assert "INTERRUPTED" in response.data["stop_reasons"]
        assert collector.repo.source_run(task, "mof")["next_page"] == 1


def test_scheduler_attachment_recovery_after_window_moves(tmp_path, monkeypatch):
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )
    listing = (
        sample["listing"]
        .replace("2026-09-01", "2026-06-25")
        .replace("countPage = 2", "countPage = 1")
    )
    detail = sample["detail"].replace(
        "</div>", '<a href="https://www.mof.gov.cn/form.xls">附件</a></div>'
    )
    fail_attachment = True
    calls = []

    def get(_, url):
        calls.append(url)
        if url == sample["url"]:
            return listing.encode(), url, "text/html"
        if url.endswith(".xls"):
            if fail_attachment:
                raise TransientFailure("HTTP 503")
            return b"xls", url, "application/vnd.ms-excel"
        return detail.encode(), url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def factory(root, config, **kwargs):
        return Collector(root, config, proxy=None, **kwargs)

    config = settings(tmp_path)
    scheduler = Scheduler(config, collector_factory=factory)
    scheduler.tick(at("2026-06-30T01:00:00"))
    old_task = states(tmp_path)["mof"]["active_task_id"]
    with closing(Repository(tmp_path)) as repo:
        tax = read_states(repo.db)["chinatax"]
        tax["paused_reason"] = "TEST_NOT_SELECTED"
        save_state(repo.db, "chinatax", tax)
        old = dict(repo.db.execute("SELECT * FROM documents").fetchone())
    fail_attachment = False
    calls.clear()
    # Restart and recover the frozen June window even in August.
    Scheduler(config, collector_factory=factory).tick(at("2026-08-10T01:00:00"))
    assert calls == ["https://www.mof.gov.cn/form.xls"]
    assert states(tmp_path)["mof"]["last_task_id"] == old_task
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert (
            dict(repo.db.execute("SELECT * FROM documents WHERE id=?", (old["id"],)).fetchone())
            == old
        )
        latest = json.loads(
            repo.db.execute(
                "SELECT record_json FROM documents ORDER BY extraction_version DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert latest["attachments"][0]["download_state"] == "saved"


def test_real_collector_network_recovery_resets_retry_count(tmp_path, monkeypatch):
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )
    fail = True

    def get(_, url):
        if fail:
            raise TransientFailure("HTTP 503")
        text = (
            sample["listing"].replace("countPage = 2", "countPage = 1")
            if url == sample["url"]
            else sample["detail"]
        )
        return text.encode(), url, "text/html"

    monkeypatch.setattr(BoundedClient, "get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: None)

    def factory(root, config, **kwargs):
        return Collector(root, config, proxy=None, **kwargs)

    scheduler = Scheduler(settings(tmp_path), collector_factory=factory)
    now = at("2026-09-30T01:00:00")
    scheduler.tick(now)
    assert states(tmp_path)["mof"]["retry_count"] == 1
    with closing(Repository(tmp_path)) as repo:
        tax = read_states(repo.db)["chinatax"]
        tax["paused_reason"] = "TEST_NOT_SELECTED"
        save_state(repo.db, "chinatax", tax)
    fail = False
    scheduler.tick(now + timedelta(seconds=300))
    assert states(tmp_path)["mof"]["retry_count"] == 0
    assert states(tmp_path)["mof"]["active_task_id"] is None


def test_template_has_every_runtime_default():
    import yaml

    template = yaml.safe_load(Path("ftr.example.yaml").read_text())
    defaults = RuntimeSettings().model_dump(mode="json")
    template.pop("data_dir")
    defaults.pop("data_dir")
    assert template == defaults


def test_crashed_running_owned_task_recovers(tmp_path):
    ControlledCollector.outcomes["mof"] = "budget"
    scheduler = Scheduler(settings(tmp_path), collector_factory=ControlledCollector)
    now = at("2026-06-30T01:00:00")
    scheduler.tick(now)
    task = states(tmp_path)["mof"]["active_task_id"]
    with closing(Repository(tmp_path)) as repo:
        repo.set_task_state(task, "RUNNING")
    scheduler.tick(now)  # Other source first.
    ControlledCollector.outcomes["mof"] = "complete"
    Scheduler(settings(tmp_path), collector_factory=ControlledCollector).tick(
        now + timedelta(seconds=60)
    )
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert repo.task(task)["state"] == "WAITING_DECISION"
        events = [
            json.loads(row[0])
            for row in repo.db.execute(
                "SELECT details_json FROM audit WHERE task_id=? AND event='batch_stopped'", (task,)
            )
        ]
        assert any("INTERRUPTED_PREVIOUS_WRITER" in event.get("reasons", []) for event in events)
    assert states(tmp_path)["mof"]["last_task_id"] == task


@pytest.mark.skipif(
    os.environ.get("FTR_WEB_BROWSER") != "1", reason="set FTR_WEB_BROWSER=1 for Chromium"
)
def test_browser_scheduler_status_narrow_readonly(tmp_path):
    import socket

    import httpx
    from playwright.sync_api import expect, sync_playwright

    config = settings(tmp_path)
    ControlledCollector.outcomes["mof"] = "restricted"
    Scheduler(config, collector_factory=ControlledCollector).tick(at("2026-06-30T01:00:00"))
    before = (tmp_path / "database.sqlite3").read_bytes()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = child_environment()
    env.update(FTR_DATA_DIR=str(tmp_path), FTR_SCHEDULER__ENABLED="true")
    process = subprocess.Popen(
        [sys.executable, "-m", "ftr.cli", "serve", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url, timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("scheduler browser test server unavailable")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url + "/#tasks")
            expect(page.locator("#scheduler-status")).to_contain_text("09:00、18:00")
            expect(page.locator("#scheduler-status")).to_contain_text("官网访问受限")
            expect(page.locator("#scheduler-status")).to_contain_text("每次回看 30 天")
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_role("button", name="刷新", exact=True).click()
            expect(page.locator("#scheduler-status")).to_contain_text("未读匹配数量未知")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            assert not errors
            browser.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert (tmp_path / "database.sqlite3").read_bytes() == before
