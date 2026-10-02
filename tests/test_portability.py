import json
import os
import subprocess
import sys
from datetime import date
from pathlib import PureWindowsPath
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from ftr.backup import _relative_path, create_backup, restore_backup, verify_backup
from ftr.cli import parser, run
from ftr.config import RuntimeSettings, WebSettings
from ftr.evidence import EvidenceStore
from ftr.models import TaskRequest
from ftr.network import AccessBlocked, BoundedClient, BrowserSessionClient, browser_proxy, check_url
from ftr.repository import Repository
from ftr.runtime import Collector, data_lock


def test_lock_is_nonblocking_across_processes_and_released_on_exit(tmp_path):
    child = """
import os, sys
from pathlib import Path
from ftr.runtime import data_lock
with data_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.readline()
    os._exit(0)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(RuntimeError, match="另一宿主"), data_lock(tmp_path):
            pytest.fail("不应获得已占用锁")
        process.communicate("exit\n", timeout=10)
        assert process.returncode == 0
        with data_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_backup_moves_between_separator_conventions_and_special_paths(tmp_path):
    suffix = " #" if os.name == "nt" else " #?"
    root = tmp_path / ("原始 资料" + suffix)
    repo = Repository(root)
    evidence = EvidenceStore(root).save(
        b"evidence", "https://example.org/a", "https://example.org/a", "text/plain"
    )
    repo.save_evidence(evidence)
    repo.close()
    backup = tmp_path / ("备份 空间" + suffix)
    manifest = create_backup(root, backup)
    assert all("\\" not in name for name in manifest["files"])
    # 模拟历史 Windows 清单，不能只验证新版本本机生成的文件。
    manifest["files"] = {
        name.replace("/", "\\"): value for name, value in manifest["files"].items()
    }
    (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert verify_backup(backup)["verified"]
    restored = tmp_path / ("恢复 资料" + suffix)
    assert restore_backup(backup, restored)["verified"]
    assert (restored / "evidence" / evidence.relative_path).read_bytes() == b"evidence"
    with pytest.raises(FileExistsError):
        restore_backup(backup, restored)
    assert PureWindowsPath("C:/资料 空间/database.sqlite3").as_uri().startswith("file:///C:/")


def test_backup_cli_verify_and_restore_do_not_initialize_unrelated_data(tmp_path, monkeypatch):
    root = tmp_path / "original"
    repo = Repository(root)
    repo.close()
    backup = tmp_path / "backup"
    create_backup(root, backup)
    unrelated = tmp_path / "unrelated"
    monkeypatch.setenv("FTR_DATA_DIR", str(unrelated))
    assert (
        run(parser().parse_args(["backup", "verify", "--path", str(backup)])).status == "COMPLETED"
    )
    assert (
        run(
            parser().parse_args(
                ["backup", "restore", "--path", str(backup), "--target", str(tmp_path / "restored")]
            )
        ).status
        == "COMPLETED"
    )
    assert not unrelated.exists()


@pytest.mark.parametrize(
    "relative", ["C:\\secret", "C:secret", "\\\\server\\share\\a", "../x", "a\\..\\x", "/x"]
)
def test_foreign_platform_escape_is_rejected(relative):
    with pytest.raises(ValueError):
        _relative_path(relative)


@pytest.mark.parametrize(
    "mode,proxy", [("direct", None), ("explicit", "http://user:password@proxy.example:8080")]
)
def test_http_clients_share_proxy_and_apply_limits(monkeypatch, mode, proxy):
    settings = RuntimeSettings(
        network={
            "proxy_mode": mode,
            "proxy_url": proxy,
            "timeout_seconds": 7,
            "interval_seconds": 0,
            "max_file_bytes": 3,
        }
    )
    client = BoundedClient(["www.mof.gov.cn"], settings=settings.network)
    tax = BrowserSessionClient(["www.mof.gov.cn"], settings=settings.network)
    try:
        assert client.proxy == tax.proxy == proxy
        assert client.timeout == tax.timeout == 7
        assert not tax._session.trust_env
        assert tax._session.proxies == ({"https": proxy} if proxy else {})
        client._client.close()
        client._client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"1234"))
        )
        monkeypatch.setattr("ftr.network.check_url", lambda *_: "www.mof.gov.cn")
        with pytest.raises(AccessBlocked, match="单文件"):
            client.get("https://www.mof.gov.cn/")
    finally:
        client.close()
        tax.close()


def test_explicit_direct_proxy_does_not_borrow_system_dns_exception(monkeypatch):
    monkeypatch.setattr("ftr.network.getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    monkeypatch.setattr(
        "ftr.network.socket.getaddrinfo", lambda *_: [(None, None, None, None, ("198.18.0.1", 443))]
    )
    with pytest.raises(AccessBlocked):
        check_url("https://www.mof.gov.cn/", ["www.mof.gov.cn"], None)
    assert check_url("https://www.mof.gov.cn/", ["www.mof.gov.cn"], "http://127.0.0.1:7890")


def test_mof_collection_has_no_browser_dependency_and_resume_audits_config(tmp_path, monkeypatch):
    settings = RuntimeSettings(data_dir=tmp_path, network={"proxy_mode": "direct"})
    collector = Collector(tmp_path, settings)

    def fake_source(task_id, request, source_id, *args):
        collector.repo.save_page(task_id, source_id, [], None)

    monkeypatch.setattr(collector, "_run_source", fake_source)
    monkeypatch.setattr(
        "ftr.runtime.sync_playwright", lambda: pytest.fail("财政部不应启动 Playwright")
    )
    try:
        request = TaskRequest(
            source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 2), max_pages=2
        )
        result = collector.collect(request)
        assert result.status == "COMPLETED_EMPTY"
        collector.settings.collection.max_duration_seconds = 12
        assert collector.resume(result.task_id).status == "COMPLETED_EMPTY"
        audit = collector.repo.db.execute(
            "SELECT details_json FROM audit WHERE event='runtime_config' ORDER BY id DESC"
        ).fetchone()[0]
        assert json.loads(audit)["settings"]["collection"]["max_duration_seconds"] == 12
        assert json.loads(collector.repo.task(result.task_id)["request_json"])["max_pages"] == 2
    finally:
        collector.close()


def test_browser_launch_uses_same_proxy_and_timeout_and_audit_is_redacted(tmp_path, monkeypatch):
    settings = RuntimeSettings(
        data_dir=tmp_path,
        network={"proxy_mode": "explicit", "proxy_url": "http://alice:secret@127.0.0.1:7890"},
        browser={"headless": True, "timeout_seconds": 9, "executable_path": tmp_path / "chromium"},
    )
    (tmp_path / "chromium").touch()
    launch = Mock(return_value=Mock())
    driver = Mock()
    driver.__enter__ = Mock(return_value=SimpleNamespace(chromium=SimpleNamespace(launch=launch)))
    driver.__exit__ = Mock(return_value=False)
    monkeypatch.setattr("ftr.runtime.sync_playwright", lambda: driver)
    collector = Collector(tmp_path, settings)
    adapter = Mock()
    monkeypatch.setattr("ftr.runtime.ChinataxAdapter", adapter)
    monkeypatch.setattr(
        collector, "_run_source", lambda *args: collector.repo.save_page(args[0], args[2], [], None)
    )
    try:
        result = collector.collect(
            TaskRequest(
                source_ids=["chinatax"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 2)
            )
        )
        assert result.status == "COMPLETED_EMPTY"
        kwargs = launch.call_args.kwargs
        assert kwargs["headless"] is True and kwargs["timeout"] == 9000
        assert kwargs["executable_path"] == str(tmp_path / "chromium")
        assert kwargs["proxy"] == browser_proxy(settings.network.proxy_url)
        assert adapter.call_args.kwargs["timeout_seconds"] == 9
        audit = collector.repo.db.execute(
            "SELECT details_json FROM audit WHERE event='runtime_config'"
        ).fetchone()[0]
        assert "alice" not in audit and "secret" not in audit
    finally:
        collector.close()


def test_linux_without_display_records_partial_instead_of_launching(tmp_path, monkeypatch):
    monkeypatch.setattr("ftr.runtime.sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        "ftr.runtime.sync_playwright", lambda: pytest.fail("不能在缺显示环境时启动浏览器")
    )
    collector = Collector(tmp_path, RuntimeSettings(network={"proxy_mode": "direct"}))
    try:
        result = collector.collect(
            TaskRequest(
                source_ids=["chinatax"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 2)
            )
        )
        assert result.status == "PARTIAL" and "xvfb-run" in result.warnings[0]
    finally:
        collector.close()


def test_new_task_freezes_config_budgets_and_system_proxy_is_resolved_once(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FTR_NETWORK__PROXY_MODE", "system")
    monkeypatch.setenv("FTR_COLLECTION__MAX_PAGES", "2")
    proxy = Mock(
        side_effect=[{"https": "http://127.0.0.1:7890"}, {"https": "http://127.0.0.1:7891"}]
    )
    monkeypatch.setattr("ftr.network.getproxies", proxy)
    monkeypatch.setattr(Collector, "resume", lambda self, task: SimpleNamespace(task_id=task))
    result = run(
        parser().parse_args(
            [
                "collect",
                "--sources",
                "mof",
                "--date-from",
                "2026-09-01",
                "--date-to",
                "2026-09-02",
                "--max-documents",
                "3",
            ]
        )
    )
    assert proxy.call_count == 1
    repo = Repository(tmp_path)
    try:
        request = json.loads(repo.task(result.task_id)["request_json"])
        assert request["max_pages"] == 2 and request["max_documents"] == 3
    finally:
        repo.close()


def test_lan_host_and_ui_config_are_readonly_and_do_not_expose_runtime(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ftr.web.app import create_app

    settings = WebSettings(host="0.0.0.0", poll_interval_ms=750, request_timeout_ms=1200)
    client = TestClient(
        create_app(tmp_path / "not-created", settings), base_url="http://192.168.1.20"
    )
    result = client.get("/api/ui-config")
    assert result.status_code == 200
    assert set(result.json()) == {"queried_at", "poll_interval_ms", "request_timeout_ms"}
    assert result.json()["poll_interval_ms"] == 750
    assert client.get("/api/overview").status_code == 503
    assert client.post("/api/policies").status_code == 405
    assert not (tmp_path / "not-created").exists()
