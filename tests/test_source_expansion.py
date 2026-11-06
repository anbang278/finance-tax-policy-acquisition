import json
from datetime import date

import pytest

from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.sources import source_snapshot


def request(**changes):
    return TaskRequest(date_from=date(2026, 1, 1), date_to=date(2026, 11, 6), **changes)


def test_new_tasks_freeze_channels_and_legacy_idempotency(tmp_path):
    repo = Repository(tmp_path)
    value = request(idempotency_key="same")
    task_id = repo.create_task(value)
    stored = json.loads(repo.task(task_id)["request_json"])
    assert set(stored["source_snapshot"]) == {"mof", "chinatax"}
    assert repo.create_task(value) == task_id
    legacy = dict(stored)
    legacy.pop("source_snapshot")
    legacy.pop("official_urls")
    repo.db.execute(
        "UPDATE tasks SET request_json=?,request_digest=? WHERE id=?",
        (json.dumps(legacy), "old-digest", task_id),
    )
    repo.db.commit()
    assert repo.create_task(value) == task_id
    assert "source_snapshot" not in json.loads(repo.task(task_id)["request_json"])
    repo.close()


def test_user_snapshot_cannot_change_frozen_entry(tmp_path):
    from ftr.sources import _digest

    snapshot = source_snapshot(["mof"])
    snapshot["mof"]["config"]["entry"] = "https://www.mof.gov.cn/other/"
    snapshot["mof"]["digest"] = _digest(snapshot["mof"]["config"])
    repo = Repository(tmp_path)
    value = request(source_ids=["mof"], source_snapshot=snapshot)
    with pytest.raises(ValueError, match="快照"):
        repo.create_task(value)
    internal_id = repo.create_task(value, trusted_snapshot=True)
    assert json.loads(repo.task(internal_id)["request_json"])["source_snapshot"] == snapshot
    repo.close()


def test_unknown_source_rejected_but_historical_task_readable():
    with pytest.raises(ValueError, match="未登记"):
        request(source_ids=["unknown"])
    saved = request().model_dump(mode="json")
    saved["source_ids"] = ["retired_source"]
    assert TaskRequest.from_saved(json.dumps(saved)).source_ids == ["retired_source"]


def test_schema_seven_migration_preserves_legacy_scope(tmp_path):
    repo = Repository(tmp_path)
    assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert "scope_json" in {r[1] for r in repo.db.execute("PRAGMA table_info(managed_plan)")}
    repo.close()


def test_snapshot_keeps_old_entry_and_current_host_revocation(monkeypatch):
    from ftr.config import load_sources
    from ftr.sources import resolve_source

    frozen = source_snapshot(["mof"])
    registry = load_sources().model_copy(deep=True)
    registry.sources["mof"].entry = "https://www.mof.gov.cn/new-column/"
    monkeypatch.setattr("ftr.sources.load_sources", lambda: registry)
    assert resolve_source("mof", frozen).entry == frozen["mof"]["config"]["entry"]
    registry.sources["mof"].allowed_hosts = ["kjs.mof.gov.cn"]
    with pytest.raises(ValueError, match="信任"):
        resolve_source("mof", frozen)


def test_source_preview_readonly_and_rejects_modified_snapshot(tmp_path):
    from fastapi.testclient import TestClient

    from ftr.web.app import create_app

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        result = client.post(
            "/api/sources/preview", json=request(source_ids=["mof"]).model_dump(mode="json")
        )
        assert result.status_code == 200
        assert result.json()["request"]["source_snapshot"]["mof"]["config"]["adapter"] == "mof"
        bad = result.json()["request"]
        bad["source_snapshot"]["mof"]["config"]["entry"] = "https://example.com/"
        assert client.post("/api/sources/preview", json=bad).status_code == 422
    assert not (tmp_path / "database.sqlite3").exists()


def test_shared_host_limit_survives_client_replacement(monkeypatch):
    from ftr.config import NetworkSettings
    from ftr.network import BoundedClient

    times = {"current": 100.0}
    monkeypatch.setattr("ftr.network.time.monotonic", lambda: times["current"])
    waits = []

    def wait(client, duration):
        waits.append(duration)
        times["current"] += duration

    monkeypatch.setattr(BoundedClient, "_wait", wait)
    shared = {}
    for path in ("one", "two"):
        client = BoundedClient(["www.mof.gov.cn"], settings=NetworkSettings(interval_seconds=2))
        client.host_requests = shared
        client._before("https://www.mof.gov.cn/" + path)
        client.close()
    assert waits == [2.0]


def test_bad_unused_rules_do_not_block_other_source(tmp_path):
    from ftr.runtime import Collector

    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "chinatax.json").write_text("not json")
    collector = Collector(tmp_path)
    try:
        assert collector.rules == {}
    finally:
        collector.close()


def test_v6_backup_and_restore_keep_original_task_bytes(tmp_path):
    from ftr.backup import restore_backup, verify_backup

    root = tmp_path / "v6-data"
    repo = Repository(root)
    task_id = repo.create_task(request(source_ids=["mof"]))
    original_request = repo.task(task_id)["request_json"]
    repo.db.execute("ALTER TABLE managed_plan DROP COLUMN scope_json")
    repo.db.execute("PRAGMA user_version=6")
    repo.db.commit()
    repo.close()
    with __import__("sqlite3").connect(root / "database.sqlite3") as database:
        old_dump = list(database.iterdump())
    migrated = Repository(root)
    assert migrated.task(task_id)["request_json"] == original_request
    assert migrated.db.execute("PRAGMA user_version").fetchone()[0] == 7
    migrated.close()
    backups = list(tmp_path.glob("v6-data-migration-*"))
    assert len(backups) == 1
    verify_backup(backups[0])
    restored = tmp_path / "restored-v6"
    restore_backup(backups[0], restored)
    with __import__("sqlite3").connect(restored / "database.sqlite3") as database:
        assert list(database.iterdump()) == old_dump
        assert database.execute("PRAGMA user_version").fetchone()[0] == 6
    reread = Repository(restored)
    assert reread.task(task_id)["request_json"] == original_request
    assert reread.db.execute("PRAGMA user_version").fetchone()[0] == 7
    reread.close()


def test_successive_collectors_share_same_host_limiter(tmp_path):
    from ftr.runtime import Collector

    first = Collector(tmp_path)
    first._host_requests["www.mof.gov.cn"] = 123.0
    first.close()
    second = Collector(tmp_path)
    try:
        assert second._host_requests["www.mof.gov.cn"] == 123.0
    finally:
        second.close()


def test_managed_preview_digest_rejects_registry_change_before_queue(tmp_path):
    from ftr.config import SchedulerSettings
    from ftr.management import Command, Conflict, enqueue, read_queued
    from ftr.sources import snapshot_digest

    repo = Repository(tmp_path)
    repo.close()
    payload = {
        "schema_version": "2",
        "settings": SchedulerSettings().model_dump(mode="json"),
        "scope": {"source_ids": ["mof"]},
    }
    bad = Command(id="a" * 32, action="plan.save", payload=payload, scope_digest="0" * 64)
    with pytest.raises(Conflict, match="登记已变化"):
        enqueue(tmp_path, bad)
    assert not (tmp_path / "commands" / ("a" * 32 + ".json")).exists()
    current = source_snapshot(["mof"])
    good = bad.model_copy(update={"id": "b" * 32, "scope_digest": snapshot_digest(current)})
    enqueue(tmp_path, good)
    command, context = read_queued(tmp_path / "commands" / ("b" * 32 + ".json"))
    assert command.scope_digest == snapshot_digest(context["source_snapshot"])
    assert context["source_snapshot"] == current


def test_shipped_registry_profiles_and_all_36_bureau_catalog():
    from ftr.adapters.public import PublicProfile
    from ftr.config import load_sources
    from ftr.sources import catalog_entries

    registry = load_sources().sources
    assert registry["mof"].entry == "https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/"
    assert registry["chinatax"].entry == "https://fgk.chinatax.gov.cn/zcfgk/c100006/listflfg.html"
    for item in registry.values():
        if item.profile:
            PublicProfile.model_validate(item.profile)
        assert all(
            "://" not in host and "*" not in host and "/" not in host for host in item.allowed_hosts
        )
    catalog = catalog_entries()
    bureaus = {item["authority_id"] for item in catalog if item["authority_id"].startswith("tax_")}
    assert len(bureaus) == 36
    assert len({item["source_id"] for item in catalog}) == len(catalog)
    assert all(not item["selectable"] for item in catalog if item["source_id"] not in registry)
