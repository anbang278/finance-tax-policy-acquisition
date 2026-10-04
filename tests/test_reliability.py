"""Failure injection for transient retries, ownership, recovery and read-only explanations."""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import date
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ftr import browser, network, workbench
from ftr.cli import parser, run
from ftr.config import BrowserSettings, NetworkSettings, RuntimeSettings, WebSettings
from ftr.diagnostics import limitation_reasons, recover_interrupted, writer_activity
from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.rules import Rules, extract_listing
from ftr.runtime import BudgetReached, Collector, data_lock
from ftr.web.query import ReadQueries


@pytest.mark.parametrize("kind", ["httpx", "requests"])
@pytest.mark.parametrize("status", [502, 503, 504, 403, 429, 404])
def test_status_retries_are_bounded_and_restricted_errors_stop(monkeypatch, kind, status):
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    client = (network.BoundedClient if kind == "httpx" else network.BrowserSessionClient)(
        ["www.mof.gov.cn"], settings=NetworkSettings(interval_seconds=0), proxy=None
    )
    calls, delays = [], []
    client._wait = delays.append
    client.before_request = calls.append
    if kind == "httpx":
        client._client.close()
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(status, content=b"bad"))
        )
    else:
        import requests

        def respond(*args, **kwargs):
            response = requests.Response()
            response.status_code, response.url = status, "https://www.mof.gov.cn/"
            response._content, response._content_consumed = b"bad", True
            return response

        client._session.request = respond
    try:
        error = (
            network.TransientFailure
            if status in (502, 503, 504)
            else network.AccessBlocked
            if status in (403, 429)
            else RuntimeError
        )
        with pytest.raises(error):
            client.get("https://www.mof.gov.cn/")
        assert len(calls) == (3 if status in (502, 503, 504) else 1)
        if status in (502, 503, 504):
            assert 1 <= delays[0] <= 1.25 and 2 <= delays[1] <= 2.25
        else:
            assert delays == []
    finally:
        client.close()


def test_retry_success_and_checkpoint_prevents_next_request(monkeypatch):
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    client = network.BoundedClient(["www.mof.gov.cn"], interval=0, proxy=None)
    client._client.close()
    statuses = iter([502, 200])
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(next(statuses), content=b"ok"))
    )
    client._wait = lambda _: None
    try:
        assert client.get("https://www.mof.gov.cn/")[0] == b"ok"
        client.checkpoint = lambda: (_ for _ in ()).throw(BudgetReached("暂停", "PAUSE_REQUESTED"))
        with pytest.raises(BudgetReached):
            client.get("https://www.mof.gov.cn/")
    finally:
        client.close()


def test_wait_can_be_interrupted(monkeypatch):
    client = network.BoundedClient(["www.mof.gov.cn"], proxy=None)
    client.checkpoint = lambda: (_ for _ in ()).throw(BudgetReached("取消", "CANCEL_REQUESTED"))
    try:
        with pytest.raises(BudgetReached):
            client._wait(100)
    finally:
        client.close()


def prepare_queue(tmp_path, count=4, **settings):
    sample = next(
        x
        for x in json.loads((files("ftr") / "data/rule-fixtures.json").read_text())
        if x["source_id"] == "mof"
    )
    collector = Collector(tmp_path, RuntimeSettings(data_dir=tmp_path, **settings), proxy=None)
    task = collector.repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    refs, _ = extract_listing(Rules(source_id="mof"), sample["listing"].encode(), sample["url"], 1)
    refs = [
        refs[0].model_copy(update={"url": refs[0].url.replace("_123.htm", f"_{n}.htm")})
        for n in range(count)
    ]
    collector.repo.save_page(task, "mof", refs, None)
    return collector, task, refs, sample


@pytest.mark.parametrize(
    "failures,expected_saved,expected_calls",
    [([True, False, True, False], 2, 4), ([True, True, True, False], 0, 3)],
)
def test_transient_item_streak_continue_and_resume(
    tmp_path, monkeypatch, failures, expected_saved, expected_calls
):
    collector, task, _refs, sample = prepare_queue(tmp_path)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    calls = []

    def get(client, url):
        calls.append(url)
        if failures[len(calls) - 1]:
            raise network.TransientFailure("502")
        return sample["detail"].encode(), url, "text/html"

    monkeypatch.setattr(network.BoundedClient, "get", get)
    try:
        result = collector.resume(task)
        assert result.status == "PARTIAL"
        assert len(calls) == expected_calls
        assert result.data["remaining_queue"].get("SAVED", 0) == expected_saved
        assert result.data["resume_argv"][-1] == task
        monkeypatch.setattr(
            network.BoundedClient,
            "get",
            lambda _, url: (sample["detail"].encode(), url, "text/html"),
        )
        collector.resume(task)
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 4
        collector.resume(task)
        assert collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 4
    finally:
        collector.close()


@pytest.mark.parametrize("error", [network.AccessBlocked("403"), RuntimeError("未知")])
def test_unknown_or_access_item_stops_immediately(tmp_path, monkeypatch, error):
    collector, task, _, _ = prepare_queue(tmp_path)
    calls = []

    def get(_, url):
        calls.append(url)
        raise error

    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    monkeypatch.setattr(network.BoundedClient, "get", get)
    try:
        assert collector.resume(task).status == "PARTIAL"
        assert len(calls) == 1
    finally:
        collector.close()


def test_repair_retry_attempts_count_actual_requests(tmp_path, monkeypatch):
    collector, task, _, _ = prepare_queue(tmp_path, network={"interval_seconds": 0})
    collector.detail_limit = 2
    calls = []
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    original = network.BoundedClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._client.close()
        self._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: calls.append(req.url) or httpx.Response(502))
        )
        self._wait = lambda _: self._check()

    monkeypatch.setattr(network.BoundedClient, "__init__", init)
    try:
        assert collector.resume(task).status == "PARTIAL"
        assert len(calls) == 2
        assert collector.detail_attempts == 2
    finally:
        collector.close()


def test_interrupted_signal_and_stale_record_recovery_readonly(tmp_path, monkeypatch):
    collector, task, _, _ = prepare_queue(tmp_path)

    def source(*args):
        collector._interrupt(None, None)
        collector._checkpoint(task, "mof", time.monotonic())

    monkeypatch.setattr(collector, "_run_source", source)
    try:
        assert collector.resume(task).data["stop_reasons"] == ["INTERRUPTED"]
        collector.repo.set_task_state(task, "RUNNING")
        before = collector.repo.path.read_bytes()
        query = ReadQueries(tmp_path).task(task)["item"]
        assert query["state"] == "RUNNING" and query["writer_activity"] == "NO_WRITER_OBSERVED"
        assert collector.repo.path.read_bytes() == before
        with data_lock(tmp_path):
            recover_interrupted(collector.repo)
        assert collector.repo.task(task)["state"] == "PARTIAL"
        assert ReadQueries(tmp_path).task(task)["item"]["last_batch"]["reasons"] == [
            "INTERRUPTED_PREVIOUS_WRITER"
        ]
        assert writer_activity(tmp_path) == "NO_WRITER_OBSERVED"
    finally:
        collector.close()


def test_periodic_progress_uses_stderr_and_leaves_json_stdout(tmp_path, monkeypatch, capsys):
    collector, task, _, _ = prepare_queue(tmp_path, collection={"progress_interval_seconds": 0.01})

    reported = threading.Event()
    real_print = print

    def observe(*args, **kwargs):
        real_print(*args, **kwargs)
        if args and str(args[0]).startswith("ftr progress "):
            reported.set()

    monkeypatch.setattr("ftr.runtime.print", observe, raising=False)

    def source(*args):
        collector._checkpoint(task, "mof", time.monotonic())
        assert reported.wait(2), "periodic reporter did not emit progress"
        raise BudgetReached("预算")

    monkeypatch.setattr(collector, "_run_source", source)
    try:
        collector.resume(task)
        output = capsys.readouterr()
        assert output.out == "" and '"last_checkpoint"' in output.err
        assert "ftr progress" in output.err and '"saved"' in output.err
    finally:
        collector.close()


def test_browser_fallback_and_explicit_failure_never_falls_back(tmp_path, monkeypatch):
    paths = [tmp_path / name for name in ("chromium", "edge", "chrome")]
    for path in paths:
        path.touch()
    driver = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(paths[0])))
    calls = []

    def launch(**kwargs):
        calls.append(kwargs["executable_path"])
        if len(calls) == 1:
            raise RuntimeError("policy")
        return SimpleNamespace(close=lambda: None)

    driver.chromium.launch = launch
    original = browser.candidates
    monkeypatch.setattr(
        browser, "candidates", lambda *_: list(zip(("chromium", "edge", "chrome"), paths))
    )
    _, selected = browser.launch_browser(driver, BrowserSettings())
    assert selected["name"] == "edge" and calls == [str(paths[0]), str(paths[1])]
    monkeypatch.setattr(browser, "candidates", original)
    with pytest.raises(RuntimeError, match="显式"):
        browser.launch_browser(driver, BrowserSettings(executable_path=tmp_path / "missing"))
    assert len(calls) == 2


def test_workbench_launcher_pid_is_separate_and_legacy_state_still_works(tmp_path, monkeypatch):
    Repository(tmp_path).close()
    original = workbench.subprocess.Popen

    class Wrapped:
        def __init__(self, *args, **kwargs):
            self.child = original(*args, **kwargs)
            self.pid = self.child.pid + 100000

        def __getattr__(self, name):
            return getattr(self.child, name)

    monkeypatch.setattr(workbench.subprocess, "Popen", Wrapped)
    try:
        result = workbench.manage(tmp_path, "start")
        state_path = tmp_path / ".workbench.json"
        state = json.loads(state_path.read_text())
        assert state["launcher_pid"] != state["pid"] == result["pid"]
        assert "token" not in result
        # The old four-field state still identifies the same service securely.
        legacy = {key: state[key] for key in ("pid", "port", "token", "directory")}
        state_path.write_text(json.dumps(legacy))
        assert workbench.manage(tmp_path, "status")["state"] == "RUNNING"
    finally:
        workbench.manage(tmp_path, "stop")


def test_workbench_failed_interpreter_does_not_try_ten_ports(tmp_path, monkeypatch):
    Repository(tmp_path).close()
    process = SimpleNamespace(pid=123, poll=lambda: 1, wait=lambda **_: 1)
    spawn = Mock(return_value=process)
    monkeypatch.setattr(workbench.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        workbench, "_identity", lambda *_: (_ for _ in ()).throw(httpx.ConnectError("no server"))
    )
    with pytest.raises(workbench.WorkbenchError) as exc:
        workbench.manage(tmp_path, "start")
    assert exc.value.details["code"] == "PROCESS_EXITED"
    assert exc.value.details["exit_code"] == 1 and spawn.call_count == 1
    assert not (tmp_path / ".workbench.json").exists()


def test_quarantine_reasons_derived_without_mutating_historical_records(tmp_path):
    collector, task, refs, sample = prepare_queue(tmp_path, count=1)
    from ftr.adapters.common import parse_detail

    record = parse_detail(refs[0], sample["detail"].encode(), "evidence")
    record.quality_state = "quarantined"
    record.limitations = ["筛选日期缺失，范围待确认", "部分附件未保存", "某历史限制"]
    record_id, _ = collector.repo.add_document(task, record)
    collector.close()
    before = (tmp_path / "database.sqlite3").read_bytes()
    query = ReadQueries(tmp_path)
    assert len(query.policy(record_id)["item"]["limitation_reasons"]) == 3
    assert len(query.overview()["quarantine_reasons"]) == 3
    assert limitation_reasons(["某历史限制"])[0]["code"] == "other_historical"
    assert (tmp_path / "database.sqlite3").read_bytes() == before


def test_workbench_slow_start_and_instance_mismatch(tmp_path, monkeypatch):
    Repository(tmp_path).close()
    original = workbench._identity
    attempts = []

    def slow(state, timeout=1):
        attempts.append(state)
        if len(attempts) <= 3:
            raise httpx.ConnectError("starting")
        return original(state, timeout)

    monkeypatch.setattr(workbench, "_identity", slow)
    try:
        workbench.manage(tmp_path, "start", web_settings=WebSettings(startup_timeout_seconds=5))
        assert len(attempts) >= 4
        path = tmp_path / ".workbench.json"
        original_state = path.read_text()
        state = json.loads(original_state)
        state["instance_id"] = "different"
        path.write_text(json.dumps(state))
        assert workbench.manage(tmp_path, "stop")["state"] == "STOPPED"
        path.write_text(original_state)
        assert workbench.manage(tmp_path, "status")["state"] == "RUNNING"
    finally:
        workbench.manage(tmp_path, "stop")


def test_pdf_warning_is_preserved_and_prevents_quality_promotion(tmp_path, monkeypatch):
    from ftr.adapters.common import parse_detail
    from ftr.models import Attachment

    collector, task, refs, sample = prepare_queue(tmp_path, count=1)
    collector._active_task = task
    record = parse_detail(refs[0], sample["detail"].encode(), "evidence")
    record.attachments = [
        Attachment(url="https://www.mof.gov.cn/form.pdf", label="附件", content_role="primary")
    ]

    def reader(_):
        logging.getLogger("pypdf").warning("Impossible to decode XFormObject")
        return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda: "partial text")])

    monkeypatch.setattr("ftr.runtime.PdfReader", reader)
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    client = SimpleNamespace(get=lambda url: (b"pdf", url, "application/pdf"))
    try:
        warnings = []
        collector._attachments(
            record, collector.sources["mof"], client, warnings, 100, time.monotonic()
        )
        assert "PDF 解析产生警告，内容完整性待复核" in record.limitations
        row = collector.repo.db.execute(
            "SELECT details_json FROM audit WHERE event='pdf_warnings'"
        ).fetchone()
        assert json.loads(row[0])["count"] == 1
        assert warnings == ["PDF 解析警告 1 条；内容完整性待复核"]
    finally:
        collector.close()


def test_hard_exit_record_only_repaired_after_exclusive_lock(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    task = repo.create_task(
        TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    repo.close()
    code = "from pathlib import Path; from ftr.repository import Repository; import os; r=Repository(Path(os.environ['FTR_DATA_DIR'])); r.set_task_state(os.environ['TASK_ID'],'RUNNING'); os._exit(9)"
    env = {key: value for key, value in os.environ.items() if not key.startswith("FTR_")}
    env.update(FTR_DATA_DIR=str(tmp_path), TASK_ID=task)
    result = subprocess.run([sys.executable, "-c", code], env=env, check=False)
    assert result.returncode == 9
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path))
    status = run(parser().parse_args(["task", "status", "--task", task]))
    assert status.status == "RUNNING"
    # A write operation is refused while a writer exists; it cannot repair the stale record.
    with data_lock(tmp_path), pytest.raises(RuntimeError, match="另一宿主"):
        run(parser().parse_args(["task", "resume", "--task", task]))
    # Acquire lock and recover locally, without any source request.
    repo = Repository(tmp_path)
    with data_lock(tmp_path):
        recover_interrupted(repo)
    assert repo.task(task)["state"] == "PARTIAL"
    repo.close()


def test_listing_failure_keeps_same_checkpoint(tmp_path, monkeypatch):
    collector = Collector(tmp_path, RuntimeSettings(network={"interval_seconds": 0}), proxy=None)
    calls = []
    original = network.BoundedClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._client.close()
        self._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: calls.append(req.url) or httpx.Response(502))
        )
        self._wait = lambda _: self._check()

    monkeypatch.setattr(network.BoundedClient, "__init__", init)
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    try:
        result = collector.collect(
            TaskRequest(source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
        )
        assert result.status == "PARTIAL" and len(calls) == 3
        row = collector.repo.source_run(result.task_id, "mof")
        assert row["next_page"] == 1 and row["pages_count"] == 0
        assert result.data["stop_reasons"] == ["TRANSIENT_NETWORK"]
    finally:
        collector.close()


def test_pause_during_retry_wait_prevents_second_request(tmp_path, monkeypatch):
    collector, task, _, _ = prepare_queue(tmp_path, network={"interval_seconds": 0})
    calls = []
    original = network.BoundedClient.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._client.close()
        self._client = httpx.Client(
            transport=httpx.MockTransport(lambda req: calls.append(req.url) or httpx.Response(502))
        )

        def wait(duration):
            collector.repo.request_stop(task, False)
            self._check()

        self._wait = wait

    monkeypatch.setattr(network.BoundedClient, "__init__", init)
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    monkeypatch.setattr("ftr.runtime.check_url", lambda *_: "www.mof.gov.cn")
    try:
        result = collector.resume(task)
        assert len(calls) == 1 and result.status == "PARTIAL"
        assert result.data["stop_reasons"] == ["PAUSE_REQUESTED"]
        assert (
            collector.repo.db.execute(
                "SELECT COUNT(*) FROM discovered WHERE state='FAILED'"
            ).fetchone()[0]
            == 0
        )
    finally:
        collector.close()


def test_workbench_timeout_stops_only_created_handle_and_redacts_logs(tmp_path, monkeypatch):
    Repository(tmp_path).close()
    process = SimpleNamespace(pid=1, poll=lambda: None, terminate=Mock(), wait=Mock())
    spawn = Mock(return_value=process)
    monkeypatch.setattr(workbench.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        workbench, "_identity", lambda *_: (_ for _ in ()).throw(httpx.ConnectError("pending"))
    )
    started = time.monotonic()
    with pytest.raises(workbench.WorkbenchError) as exc:
        workbench.manage(tmp_path, "start", web_settings=WebSettings(startup_timeout_seconds=0.02))
    assert exc.value.details["code"] == "STARTUP_TIMEOUT"
    assert spawn.call_count == 1 and time.monotonic() - started < 1
    process.terminate.assert_called_once()
    log = tmp_path / ".workbench.log"
    log.write_text("https://alice:secret@proxy/ token-value", encoding="utf-8")
    summary = workbench._log_summary(log, 0, "token-value")
    assert "alice" not in summary and "secret" not in summary and "token-value" not in summary


def test_workbench_missing_web_dependency_is_classified_before_spawn(tmp_path, monkeypatch):
    Repository(tmp_path).close()
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    spawn = Mock()
    monkeypatch.setattr(workbench.subprocess, "Popen", spawn)
    with pytest.raises(workbench.WorkbenchError) as exc:
        workbench.manage(tmp_path, "start")
    assert exc.value.details["code"] == "DEPENDENCIES_MISSING"
    spawn.assert_not_called()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX SIGTERM; Windows force termination is recovered on next writer",
)
def test_actual_sigterm_preserves_json_and_partial_checkpoint(tmp_path):
    import signal

    env = {key: value for key, value in os.environ.items() if not key.startswith("FTR_")}
    env["FTR_DATA_DIR"] = str(tmp_path)
    code = """
import os,sys,time
from pathlib import Path
from datetime import date
from ftr.config import RuntimeSettings
from ftr.runtime import Collector
from ftr.models import TaskRequest
root=Path(os.environ['FTR_DATA_DIR'])
c=Collector(root, RuntimeSettings(data_dir=root, collection={'progress_interval_seconds': .01}), proxy=None)
def source(task,request,source,*args):
    print('READY',flush=True)
    while True:
        c._checkpoint(task,source,time.monotonic())
        time.sleep(.02)
c._run_source=source
try:
    result=c.collect(TaskRequest(source_ids=['mof'],date_from=date(2026,9,1),date_to=date(2026,9,30)))
    print(result.model_dump_json())
finally:
    c.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        child.send_signal(signal.SIGTERM)
        output, errors = child.communicate(timeout=5)
        assert child.returncode == 0, errors
        result = json.loads(output)
        assert result["status"] == "PARTIAL" and result["data"]["stop_reasons"] == ["INTERRUPTED"]
        assert ReadQueries(tmp_path).task(result["task_id"])["item"]["state"] == "PARTIAL"
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
