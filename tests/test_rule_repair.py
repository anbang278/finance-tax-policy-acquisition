import json
from datetime import date
from importlib.resources import files

import pytest

from ftr.backup import create_backup, restore_backup
from ftr.config import RuntimeSettings
from ftr.evidence import EvidenceStore
from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.rule_repair import activate, failure_context, recover, submit
from ftr.rule_repair import test_candidate as verify_candidate
from ftr.rules import Rules, active_rules, active_state, atomic_json, extract_listing
from ftr.runtime import Collector, data_lock


def fixtures(source):
    return next(
        x
        for x in json.loads((files("ftr") / "data" / "rule-fixtures.json").read_text())
        if x["source_id"] == source
    )


def fault(tmp_path, source="mof", stage="discover"):
    root = tmp_path / "data"
    repo = Repository(root)
    task = repo.create_task(
        TaskRequest(source_ids=[source], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    sample = fixtures(source)
    if source == "mof":
        raw = sample["listing"].replace("countPage", "newTotal").encode()
        rules = Rules(source_id=source, total_variable="newTotal")
    else:
        raw = sample["listing"].replace('"data":', '"newData":').encode()
        rules = Rules(source_id=source, data_path="results.newData")
    context = {"stage": stage, "page": 1, "rule_version": Rules(source_id=source).version}
    if stage == "detail":
        refs, _ = extract_listing(
            Rules(source_id=source), sample["listing"].encode(), sample["url"], 1
        )
        context["ref"] = refs[0].model_dump(mode="json")
        raw = (
            sample["detail"]
            .replace("TRS_Editor", "newBody")
            .replace("arc_cont", "newBody")
            .encode()
        )
        rules = Rules(
            source_id=source,
            bodies=[
                "//div[contains(@class,'newBody')]",
                "//div[contains(@class,'TRS_Editor')]",
                "//div[contains(@class,'arc_cont')]",
            ],
        )
    evidence = EvidenceStore(root).save(raw, sample["url"], sample["url"], "text/html")
    repo.save_evidence(evidence)
    context["evidence_id"] = evidence.evidence_id
    failure = repo.add_failure(task, source, "STRUCTURE_DRIFT", context)
    path = tmp_path / "rule.json"
    path.write_text(rules.model_dump_json())
    return root, repo, task, failure, path, sample


@pytest.mark.parametrize("source", ["mof", "chinatax"])
@pytest.mark.parametrize("stage", ["discover", "detail"])
def test_offline_gates_keep_history_and_recover_failure(tmp_path, source, stage):
    root, repo, _task, failure, path, _ = fault(tmp_path, source, stage)
    candidate = submit(root, repo, failure, path)["candidate_id"]
    report = verify_candidate(root, repo, candidate, RuntimeSettings(data_dir=root))
    assert report["state"] == "RULE_OFFLINE_VALIDATED"
    assert report["live_verified"] is False
    with pytest.raises(ValueError, match="真实来源"):
        activate(root, repo, candidate, RuntimeSettings(data_dir=root))
    assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    repo.close()


@pytest.mark.parametrize(
    "change",
    [
        {"bodies": ["//*[local-name()='div']"]},
        {"page_template": "../{index}"},
        {"data_path": "__import__(os)"},
        {"total_variable": "a+"},
        {"date_format": "%s"},
        {"extra": "code"},
        {"source_id": "evil"},
    ],
)
def test_rule_language_rejects_executable_or_unbounded_inputs(change):
    with pytest.raises(ValueError):
        Rules.model_validate({"source_id": "mof"} | change)


def test_budget_tamper_and_unsafe_links(tmp_path):
    root, repo, _task, failure, path, _sample = fault(tmp_path)
    candidate = submit(root, repo, failure, path)["candidate_id"]
    submit(root, repo, failure, path)
    with pytest.raises(ValueError, match="预算"):
        submit(root, repo, failure, path)
    folder = root / "rules" / "candidates" / candidate
    (folder / "rules.json").write_text(Rules(source_id="mof").model_dump_json())
    with pytest.raises(ValueError, match="篡改"):
        verify_candidate(root, repo, candidate, RuntimeSettings(data_dir=root))
    repo.close()


def test_full_mof_release_resume_and_rollback(tmp_path, monkeypatch):
    root, repo, _task, failure, path, sample = fault(tmp_path)
    candidate = submit(root, repo, failure, path)["candidate_id"]
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")

    def get(_self, url):
        return (
            (
                sample["listing"].replace("countPage", "newTotal")
                if url.endswith("/")
                else sample["detail"]
            ).encode(),
            url,
            "text/html",
        )

    monkeypatch.setattr("ftr.network.BoundedClient.get", get)
    settings = RuntimeSettings(data_dir=root)
    report = verify_candidate(root, repo, candidate, settings, live=True)
    assert report["live_records"] == 1
    with pytest.raises(ValueError, match="运行配置"):
        activate(
            root,
            repo,
            candidate,
            settings.model_copy(update={"web": settings.web.model_copy(update={"port": 8766})}),
        )
    assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    assert activate(root, repo, candidate, settings)["state"] == "RULE_ACTIVE"
    assert active_rules(root, "mof").version == report["version"]
    assert repo.db.execute("SELECT quality_state FROM documents").fetchone()[0] == "collected"
    backup = tmp_path / "backup"
    create_backup(root, backup)
    restore_backup(backup, tmp_path / "restored")
    assert active_rules(tmp_path / "restored", "mof").version == report["version"]
    repo.close()


def test_report_tampering_and_first_resume_failure(tmp_path, monkeypatch):
    root, repo, _task, failure, path, sample = fault(tmp_path)
    candidate = submit(root, repo, failure, path)["candidate_id"]
    settings = RuntimeSettings(data_dir=root)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    monkeypatch.setattr(
        "ftr.network.BoundedClient.get",
        lambda _, url: (
            (
                sample["listing"].replace("countPage", "newTotal")
                if url.endswith("/")
                else sample["detail"]
            ).encode(),
            url,
            "text/html",
        ),
    )
    verify_candidate(root, repo, candidate, settings, live=True)
    report_path = root / "rules" / "candidates" / candidate / "report.json"
    original = report_path.read_text()
    value = json.loads(original)
    value["created_at"] += 10
    report_path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="可信"):
        activate(root, repo, candidate, settings)
    report_path.write_text(original)
    monkeypatch.setattr(
        "ftr.network.BoundedClient.get", lambda _, url: (b"<p>bad</p>", url, "text/html")
    )
    result = activate(root, repo, candidate, settings)
    assert result["state"] == "RULE_ROLLED_BACK"
    assert active_rules(root, "mof").version == Rules(source_id="mof").version
    assert repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    repo.close()


def test_crash_recovery_and_lock_conflict(tmp_path):
    root, repo, *_ = fault(tmp_path)
    original = Rules(source_id="mof")
    changed = Rules(source_id="mof", total_variable="newTotal")
    atomic_json(
        root / "rules" / "mof.json",
        {
            "rules": changed.model_dump(),
            "version": changed.version,
            "probation": True,
            "previous": {"rules": original.model_dump(), "version": original.version},
        },
    )
    recover(root, repo)
    assert active_state(root, "mof")["version"] == original.version
    with data_lock(root), pytest.raises(RuntimeError, match="另一宿主"), data_lock(root):
        pass
    repo.close()


def test_actual_collector_records_failure_evidence(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(
        "ftr.network.BoundedClient.get", lambda _, url: (b"<p>changed</p>", url, "text/html")
    )
    collector = Collector(root)
    result = collector.collect(
        TaskRequest(
            source_ids=["mof"],
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            max_pages=1,
            max_documents=2,
        )
    )
    assert result.status == "PARTIAL"
    context = failure_context(collector.repo, result.data["failure_ids"][0])
    assert context["category"] == "STRUCTURE_DRIFT"
    assert context["context"]["evidence_id"]
    collector.close()


def test_full_tax_rule_closed_loop(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    root, repo, _task, failure, path, sample = fault(tmp_path, "chinatax")
    candidate = submit(root, repo, failure, path)["candidate_id"]
    monkeypatch.setattr("ftr.adapters.chinatax.check_url", lambda *_: "fgk.chinatax.gov.cn")
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "fgk.chinatax.gov.cn")
    response = SimpleNamespace(
        status=200,
        request=SimpleNamespace(
            url="https://fgk.chinatax.gov.cn/getFileListByCodeId",
            post_data="channelId=abc123&size=10",
            headers={},
        ),
    )
    page = SimpleNamespace(
        expect_response=lambda *_args, **_kwargs: nullcontext(SimpleNamespace(value=response)),
        goto=lambda *_args, **_kwargs: SimpleNamespace(status=200),
        context=SimpleNamespace(cookies=list),
        close=lambda: None,
    )
    browser = SimpleNamespace(new_page=lambda: page, close=lambda: None)
    monkeypatch.setattr(
        "ftr.runtime.sync_playwright",
        lambda: nullcontext(SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_: browser))),
    )
    monkeypatch.setattr(
        "ftr.network.BrowserSessionClient.post_form",
        lambda _, url, data: (
            sample["listing"].replace('"data":', '"newData":').encode(),
            url,
            "application/json",
        ),
    )
    monkeypatch.setattr(
        "ftr.network.BrowserSessionClient.get",
        lambda _, url: (sample["detail"].encode(), url, "text/html"),
    )
    settings = RuntimeSettings(data_dir=root)
    assert verify_candidate(root, repo, candidate, settings, live=True)["state"] == "RULE_VERIFIED"
    assert activate(root, repo, candidate, settings)["state"] == "RULE_ACTIVE"
    assert repo.db.execute("SELECT quality_state FROM documents").fetchone()[0] == "collected"
    repo.close()


def test_reject_changed_history_and_cross_host_failure(tmp_path):
    root, repo, _task, failure, path, sample = fault(tmp_path)
    rules = Rules(source_id="mof", total_variable="newTotal", bodies=["//p"])
    path.write_text(rules.model_dump_json())
    candidate = submit(root, repo, failure, path)["candidate_id"]
    # Body remains identical for this simple fixture, but title corruption must be rejected.
    corrupt = Rules(source_id="mof", total_variable="newTotal", titles=["//p"])
    path.write_text(corrupt.model_dump_json())
    candidate = submit(root, repo, failure, path)["candidate_id"]
    with pytest.raises(ValueError, match="历史正文"):
        verify_candidate(root, repo, candidate, RuntimeSettings(data_dir=root))
    from ftr.rule_repair import _links

    refs, _ = extract_listing(
        Rules(source_id="mof"),
        sample["listing"].replace("kjs.mof.gov.cn", "evil.example").encode(),
        sample["url"],
        1,
    )
    with pytest.raises(ValueError, match="不可信链接"):
        _links(refs, "mof")
    with pytest.raises(ValueError):
        extract_listing(
            Rules(source_id="mof"),
            sample["listing"].replace("= 2", "= 999999").encode(),
            sample["url"],
            1,
        )
    repo.close()


def test_network_retry_is_bounded_and_access_denied_not_retried(monkeypatch):
    import httpx

    from ftr.network import AccessBlocked, BoundedClient

    client = BoundedClient(["www.mof.gov.cn"], interval=0)
    count = []

    def failed(url):
        count.append(url)
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(client, "_get", failed)
    with pytest.raises(RuntimeError):
        client.get("https://www.mof.gov.cn/a")
    assert len(count) == 3
    count.clear()

    def blocked(url):
        count.append(url)
        raise AccessBlocked("403")

    monkeypatch.setattr(client, "_get", blocked)
    with pytest.raises(AccessBlocked):
        client.get("https://www.mof.gov.cn/a")
    assert len(count) == 1
    client.close()


def test_validation_respects_paused_task(tmp_path):
    root, repo, task, failure, path, _sample = fault(tmp_path)
    candidate = submit(root, repo, failure, path)["candidate_id"]
    repo.request_stop(task, False)
    with pytest.raises(ValueError, match="暂停"):
        verify_candidate(root, repo, candidate, RuntimeSettings(data_dir=root), live=True)
    report = json.loads((root / "rules/candidates" / candidate / "report.json").read_text())
    assert report["state"] == "RULE_VALIDATION_FAILED"
    repo.close()


def test_detail_limit_counts_duplicate_versions(tmp_path, monkeypatch):
    from ftr.adapters.common import parse_detail

    sample = fixtures("mof")
    collector = Collector(tmp_path, detail_limit=2)
    task = collector.repo.create_task(
        TaskRequest(
            source_ids=["mof"],
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
            max_documents=1,
        )
    )
    refs, _ = extract_listing(Rules(source_id="mof"), sample["listing"].encode(), sample["url"], 1)
    refs = [
        refs[0].model_copy(update={"url": refs[0].url.replace("_123.htm", f"_{n}.htm")})
        for n in range(3)
    ]
    collector.repo.save_page(task, "mof", refs, None)
    evidence = collector.evidence.save(
        sample["detail"].encode(), refs[0].url, refs[0].url, "text/html"
    )
    collector.repo.save_evidence(evidence)
    for ref in refs:
        collector.repo.add_document(
            task, parse_detail(ref, sample["detail"].encode(), evidence.evidence_id)
        )
    calls = []

    def get(_client, url):
        calls.append(url)
        return sample["detail"].encode(), url, "text/html"

    monkeypatch.setattr("ftr.network.BoundedClient.get", get)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    result = collector.resume(task)
    assert result.status == "PARTIAL"
    assert len(calls) == 2
    assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 3
    collector.close()
