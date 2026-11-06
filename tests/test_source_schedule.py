"""Explicit source authorization and fair scheduling; all stores are temporary."""

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from ftr.config import RuntimeSettings, SchedulerSettings
from ftr.management import Command, effective_plan, enqueue, operations, process_commands
from ftr.repository import Repository
from ftr.scheduler import Scheduler, initial_state, read_states


def test_legacy_effective_plan_does_not_expand_with_catalog(tmp_path):
    assert effective_plan(tmp_path)["scope"] == {"source_ids": ["mof", "chinatax"]}
    assert not tmp_path.joinpath("database.sqlite3").exists()


def test_three_source_rotation_resumes_after_persisted_cursor(tmp_path, monkeypatch):
    import ftr.scheduler as scheduling
    from ftr import management

    sources = ["mof", "chinatax", "fictional_province"]
    config = RuntimeSettings(data_dir=tmp_path, scheduler={"enabled": True})
    now = datetime(2026, 11, 6, 1, tzinfo=UTC)
    with closing(Repository(tmp_path)) as repo:
        repo.db.execute(
            "INSERT INTO scheduler_meta VALUES(1,?,?,NULL)",
            (config.scheduler.model_dump_json(), now.isoformat()),
        )
        for source in sources:
            state = initial_state()
            state["pending_window"] = {
                "date_from": "2026-11-01",
                "date_to": "2026-11-06",
                "slot": now.isoformat(),
            }
            repo.db.execute(
                "INSERT INTO scheduler_sources VALUES(?,?)", (source, json.dumps(state))
            )
        repo.db.commit()
    monkeypatch.setattr(scheduling, "SOURCES", tuple(sources))
    monkeypatch.setattr(
        management,
        "effective_plan",
        lambda *a: {
            "revision": 1,
            "settings": config.scheduler.model_dump(mode="json"),
            "scope": {"source_ids": sources},
            "source_snapshot": {},
        },
    )
    calls = []
    monkeypatch.setattr(
        Scheduler, "_batch", lambda self, repo, source, state, when: calls.append(source)
    )
    for index in range(6):
        # New instance each tick proves the cursor survives process replacement.
        Scheduler(config).tick(now + timedelta(seconds=index))
    assert calls == sources * 2


def test_new_plan_save_requires_and_accepts_explicit_scope(tmp_path):
    with closing(Repository(tmp_path)):
        pass
    settings = SchedulerSettings(times=["10:00"]).model_dump(mode="json")
    command = Command(
        id="a" * 32,
        action="plan.save",
        payload={"schema_version": "2", "settings": settings, "scope": {"source_ids": ["mof"]}},
    )
    enqueue(tmp_path, command)
    process_commands(RuntimeSettings(data_dir=tmp_path))
    assert operations(tmp_path)[0]["state"] == "APPLIED"
    assert effective_plan(tmp_path)["scope"]["source_ids"] == ["mof"]
    assert effective_plan(tmp_path)["settings"]["enabled"] is False


def test_legacy_settings_edit_preserves_selected_scope(tmp_path):
    with closing(Repository(tmp_path)) as repo:
        if "scope_json" not in {r[1] for r in repo.db.execute("PRAGMA table_info(managed_plan)")}:
            repo.db.execute("ALTER TABLE managed_plan ADD COLUMN scope_json TEXT")
        repo.db.execute(
            "INSERT INTO managed_plan(id,revision,applied_revision,settings_json,scope_json) VALUES(1,1,1,?,?)",
            (SchedulerSettings().model_dump_json(), json.dumps({"source_ids": ["mof"]})),
        )
        repo.db.commit()
    command = Command(
        id="b" * 32,
        action="plan.save",
        expected_revision=1,
        payload=SchedulerSettings(times=["11:00"]).model_dump(mode="json"),
    )
    enqueue(tmp_path, command)
    process_commands(RuntimeSettings(data_dir=tmp_path))
    assert operations(tmp_path)[0]["state"] == "APPLIED"
    assert effective_plan(tmp_path)["scope"]["source_ids"] == ["mof"]


def test_task_preparation_failure_pauses_only_affected_source(tmp_path, monkeypatch):
    from test_scheduler import ControlledCollector

    original = Repository.create_task

    def revoked(repo, request, **kwargs):
        if request.source_ids == ["mof"]:
            raise ValueError("来源信任已收紧")
        return original(repo, request, **kwargs)

    monkeypatch.setattr(Repository, "create_task", revoked)
    config = RuntimeSettings(data_dir=tmp_path, scheduler={"enabled": True})
    now = datetime(2026, 11, 6, 1, tzinfo=UTC)
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    assert scheduler.tick(now)["source_id"] == "mof"
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert read_states(repo.db)["mof"]["paused_reason"] == "EXECUTION_ERROR"
    assert scheduler.tick(now)["source_id"] == "chinatax"


def test_removed_source_stops_and_retains_checkpoint(tmp_path):
    from test_scheduler import ControlledCollector

    ControlledCollector.calls = []
    ControlledCollector.outcomes = {"mof": "budget"}
    config = RuntimeSettings(data_dir=tmp_path, scheduler={"enabled": True})
    scheduler = Scheduler(config, collector_factory=ControlledCollector)
    scheduler.tick(datetime.now(UTC))
    with closing(Repository(tmp_path, readonly=True)) as repo:
        original = read_states(repo.db)["mof"]
        original_request = repo.task(original["active_task_id"])["request_json"]
    command = Command(
        id="c" * 32,
        action="plan.save",
        payload={
            "schema_version": "2",
            "settings": config.scheduler.model_dump(mode="json"),
            "scope": {"source_ids": ["chinatax"]},
        },
    )
    enqueue(tmp_path, command)
    process_commands(config)
    assert operations(tmp_path)[0]["state"] == "APPLIED"
    ControlledCollector.calls = []
    scheduler.tick(datetime.now(UTC) + timedelta(minutes=2))
    assert ControlledCollector.calls == ["chinatax"]
    with closing(Repository(tmp_path, readonly=True)) as repo:
        state = read_states(repo.db)["mof"]
        assert state["scope_removed"] is True
        assert state["active_task_id"] == original["active_task_id"]
        assert state["prepared_request"] == original["prepared_request"]
        assert repo.task(state["active_task_id"])["request_json"] == original_request
    ControlledCollector.outcomes = {}


def test_one_off_freezes_snapshot_without_changing_daily_scope(tmp_path, monkeypatch):
    from test_scheduler import ControlledCollector

    from ftr import sources
    from ftr.scheduler import window

    with closing(Repository(tmp_path)):
        pass
    config = RuntimeSettings(data_dir=tmp_path)
    saved = Command(
        id="d" * 32,
        action="plan.save",
        payload={
            "schema_version": "2",
            "settings": config.scheduler.model_dump(mode="json"),
            "scope": {"source_ids": ["mof"]},
        },
    )
    enqueue(tmp_path, saved)
    process_commands(config)
    frozen = sources.source_snapshot(["chinatax"])
    one_off = Command(
        id="e" * 32,
        action="plan.now",
        expected_revision=1,
        payload={**window(datetime.now(UTC), config.scheduler), "sources": ["chinatax"]},
    )
    enqueue(tmp_path, one_off)
    # Applying the accepted command must never re-fetch a changed source manifest.
    monkeypatch.setattr(sources, "source_snapshot", lambda ids: pytest.fail("snapshot re-frozen"))
    process_commands(config)
    assert effective_plan(tmp_path)["scope"]["source_ids"] == ["mof"]
    assert effective_plan(tmp_path)["settings"]["enabled"] is False
    with closing(Repository(tmp_path, readonly=True)) as repo:
        assert read_states(repo.db)["chinatax"]["pending_window"]["source_snapshot"] == frozen
    ControlledCollector.outcomes = {}
    Scheduler(config, collector_factory=ControlledCollector).tick()
    with closing(Repository(tmp_path, readonly=True)) as repo:
        raw = repo.db.execute("SELECT request_json FROM tasks").fetchone()[0]
        assert json.loads(raw)["source_snapshot"] == frozen


def test_public_command_rejects_client_source_snapshot():
    with pytest.raises(ValueError):
        Command(
            id="f" * 32,
            action="plan.save",
            payload={
                "schema_version": "2",
                "settings": {},
                "scope": {"source_ids": ["mof"], "source_snapshot": {"mof": {}}},
            },
        )


def test_registered_new_source_resume_uses_persisted_state(tmp_path, monkeypatch):
    from ftr import sources
    from ftr.scheduler import resume_source

    monkeypatch.setattr(sources, "validate_source_ids", lambda ids: ids)
    with closing(Repository(tmp_path)) as repo:
        state = initial_state()
        state["paused_reason"] = "ACCESS_RESTRICTED"
        repo.db.execute(
            "INSERT INTO scheduler_sources VALUES(?,?)", ("fictional_province", json.dumps(state))
        )
        repo.db.commit()
    assert resume_source(tmp_path, "fictional_province")["paused_reason"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "sources": ["mof"],
            "date_from": 123,
            "date_to": "2026-11-06",
            "slot": "2026-11-06T00:00:00+00:00",
        },
        {"sources": ["mof"], "date_from": "2026-11-01", "date_to": "2026-11-06", "slot": None},
    ],
)
def test_invalid_one_off_window_is_validation_error(payload):
    with pytest.raises(ValueError):
        Command(id="0" * 32, action="plan.now", payload=payload)
