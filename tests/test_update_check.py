from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import httpx
import portalocker
import pytest

from ftr import __version__
from ftr import update_check as uc
from ftr.cli import parser, run
from ftr.config import RuntimeSettings, UpdateCheckSettings, load_settings

SHA = "a" * 40
REMOTE = "b" * 40


def identity(commit=SHA, dirty=False, kind="git"):
    return {"commit": commit, "dirty": dirty, "version": __version__, "install_type": kind}


def settings(tmp_path, **kwargs):
    return RuntimeSettings(
        data_dir=tmp_path / "not-created",
        network={"proxy_mode": "direct"},
        update_check=UpdateCheckSettings(**kwargs),
    )


def transport(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        uc.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(handler), **kw)
    )


def compare(monkeypatch, config, local, handler, root=None):
    transport(monkeypatch, handler)
    # conftest isolates the automatic checker; this exercises the real comparison.
    return REAL_COMPARE(config, local, root, time.monotonic() + 3)


REAL_COMPARE = uc._compare


@pytest.mark.parametrize(
    "status,state",
    [("ahead", "UPDATE_AVAILABLE"), ("behind", "LOCAL_AHEAD"), ("diverged", "DIVERGED")],
)
def test_github_relationships(tmp_path, monkeypatch, status, state):
    def handler(request):
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={"status": status, "ahead_by": 2, "behind_by": 1})
        return httpx.Response(200, json={"sha": REMOTE})

    value = compare(monkeypatch, settings(tmp_path), identity(), handler)
    assert value["state"] == state
    assert value["local"]["version"] == __version__
    assert value["basis"] == "github_compare"


def test_same_sha_dirty_and_archive_unknown(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"sha": SHA})

    value = compare(monkeypatch, settings(tmp_path), identity(dirty=True), handler)
    assert value["state"] == "UP_TO_DATE"
    assert "本地有修改" in uc.notice(value)
    assert len(calls) == 1
    value = REAL_COMPARE(
        settings(tmp_path), identity(None, None, "archive"), None, time.monotonic() + 3
    )
    assert value["state"] == "UNKNOWN"
    assert value["basis"] == "missing_local_identity"


@pytest.mark.parametrize("ancestor,state", [(SHA, "UPDATE_AVAILABLE"), (REMOTE, "LOCAL_AHEAD")])
def test_local_ancestry(tmp_path, monkeypatch, ancestor, state):
    monkeypatch.setattr(
        uc,
        "_git",
        lambda _root, *args, **kw: SimpleNamespace(returncode=0 if args[2] == ancestor else 1),
    )
    value = compare(
        monkeypatch,
        settings(tmp_path),
        identity(),
        lambda _: httpx.Response(200, json={"sha": REMOTE}),
        tmp_path,
    )
    assert value["state"] == state
    assert value["basis"] == "local_ancestry"


def test_shallow_or_unpublished_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "_git", lambda *_a, **_k: SimpleNamespace(returncode=128))
    value = compare(
        monkeypatch,
        settings(tmp_path),
        identity(),
        lambda request: (
            httpx.Response(404)
            if "/compare/" in request.url.path
            else httpx.Response(200, json={"sha": REMOTE})
        ),
        tmp_path,
    )
    assert value["state"] == "UNKNOWN"
    assert value["basis"] == "unrecognized_local_commit"


def test_actual_git_readonly_and_containing_repo(tmp_path):
    root = tmp_path / "中文 source"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    index = root / ".git/index"
    before = index.read_bytes() if index.exists() else None
    value = uc.local_identity(root)
    assert value["install_type"] == "git" and value["dirty"] is False
    (root / "draft.txt").write_text("draft")
    assert uc.local_identity(root)["dirty"] is True
    assert (index.read_bytes() if index.exists() else None) == before
    child = root / "archive"
    child.mkdir()
    assert uc.local_identity(child)["commit"] is None


def test_cache_failure_retry_and_local_change(tmp_path, monkeypatch):
    config = settings(tmp_path)
    path = tmp_path / "cache/status.json"
    calls = []

    def good(_s, local, *_a):
        calls.append(local)
        return uc._result(
            local, "UPDATE_AVAILABLE", checked_at=time.time(), remote={"commit": REMOTE}
        )

    monkeypatch.setattr(uc, "_compare", good)
    first = uc.check_for_updates(config, local=identity(), path=path)
    assert first["state"] == "UPDATE_AVAILABLE"
    assert uc.check_for_updates(config, local=identity(), path=path)["cached"]
    assert len(calls) == 1
    uc.check_for_updates(config, local=identity(REMOTE), path=path)
    assert len(calls) == 2

    def failed(*_a):
        raise uc._CheckError("RATE_LIMITED", time.time() + 8000)

    monkeypatch.setattr(uc, "_compare", failed)
    value = uc.check_for_updates(config, force=True, local=identity(REMOTE), path=path)
    assert value["state"] == "CHECK_FAILED" and value["stale"]
    assert value["last_success"]["state"] == "UPDATE_AVAILABLE"
    monkeypatch.setattr(uc, "_compare", lambda *_a: pytest.fail("服务端限流不应被 force 绕过"))
    assert uc.check_for_updates(config, force=True, local=identity(REMOTE), path=path)["cached"]
    assert not config.data_dir.exists()


def test_corrupt_cache_permissions_and_lock(tmp_path, monkeypatch):
    config = settings(tmp_path)
    path = tmp_path / "cache/status.json"
    path.parent.mkdir()
    path.write_text("not json")
    monkeypatch.setattr(
        uc,
        "_compare",
        lambda _s, local, *_a: uc._result(local, "UP_TO_DATE", checked_at=time.time()),
    )
    assert uc.check_for_updates(config, local=identity(), path=path)["state"] == "UP_TO_DATE"
    with portalocker.Lock(str(path.with_suffix(".lock")), timeout=0):
        assert (
            uc.check_for_updates(config, force=True, local=identity(), path=path)["error_code"]
            == "CHECK_IN_PROGRESS"
        )
    monkeypatch.setattr(uc, "_write", lambda *_a: (_ for _ in ()).throw(PermissionError()))
    assert uc.check_for_updates(config, force=True, local=identity(), path=path)["cache_error"]


def test_deadline_and_sanitized_failure(tmp_path, monkeypatch):
    release, completed = threading.Event(), threading.Event()

    def slow(*_a):
        try:
            release.wait(5)
            raise RuntimeError("http://password:secret@example.invalid")
        finally:
            completed.set()

    monkeypatch.setattr(uc, "_compare", slow)
    start = time.monotonic()
    try:
        value = uc.check_for_updates(settings(tmp_path, timeout_seconds=0.05), local=identity())
        # Prove return precedes completion of blocked I/O, without a 150ms scheduling gate.
        assert not completed.is_set()
        assert time.monotonic() - start < 2
        assert value["error_code"] == "TIMEOUT"
        assert "secret" not in json.dumps(value)
    finally:
        release.set()
        assert completed.wait(2)


@pytest.mark.parametrize("payload", [{"sha": "bad"}, [], {"sha": None}])
def test_invalid_responses(tmp_path, monkeypatch, payload):
    with pytest.raises(uc._CheckError):
        compare(
            monkeypatch, settings(tmp_path), identity(), lambda _: httpx.Response(200, json=payload)
        )


def test_proxy_and_no_cookies(tmp_path, monkeypatch):
    config = settings(tmp_path)
    config.network.proxy_mode = "explicit"
    config.network.proxy_url = "http://user:secret@127.0.0.1:7890"
    observed = {}
    real = httpx.Client

    def client(**kw):
        observed.update(kw)
        return real(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"sha": SHA}))
        )

    monkeypatch.setattr(uc.httpx, "Client", client)
    value = REAL_COMPARE(config, identity(), None, time.monotonic() + 3)
    assert observed["proxy"] == config.network.proxy_url
    assert observed["trust_env"] is False
    assert "cookies" not in observed
    assert "secret" not in json.dumps(value)


def test_configuration_and_explicit_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FTR_UPDATE_CHECK__ENABLED", "true")
    monkeypatch.setenv("FTR_UPDATE_CHECK__CACHE_SECONDS", "12")
    config = load_settings(overrides={"data_dir": tmp_path / "data"})
    assert config.update_check.enabled and config.update_check.cache_seconds == 12
    monkeypatch.setattr(uc, "_compare", lambda *_a: (_ for _ in ()).throw(RuntimeError("secret")))
    response = run(
        parser().parse_args(["--data-dir", str(config.data_dir), "update", "check", "--force"])
    )
    assert response.status == "COMPLETED" and response.data["state"] == "CHECK_FAILED"
    assert "本次继续运行" in capsys.readouterr().err
    assert not config.data_dir.exists()


def test_nonautomatic_commands_never_check(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "check_for_updates", lambda *_a, **_k: pytest.fail("只读配置不应检查"))
    for command in (
        ["config", "validate"],
        ["config", "show"],
        ["schema"],
        ["scheduler", "status"],
        ["scheduler", "preview"],
    ):
        run(parser().parse_args(["--data-dir", str(tmp_path / "absent"), *command]))
    assert not (tmp_path / "absent").exists()


def test_monitor_frozen_identity_restart_and_api_readonly(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from ftr.web.app import create_app

    current = identity()
    monkeypatch.setattr(uc, "local_identity", lambda *_a: dict(current))
    monkeypatch.setattr(uc, "source_fingerprint", lambda: current["commit"])
    monkeypatch.setattr(
        uc,
        "_compare",
        lambda _s, local, *_a: uc._result(
            local, "UPDATE_AVAILABLE", checked_at=time.time(), remote={"commit": REMOTE}
        ),
    )
    monitor = uc.UpdateMonitor(settings(tmp_path))
    monitor.refresh()
    current["commit"] = REMOTE
    monitor.refresh()
    value = monitor.read()
    assert value["restart_required"] and value["local"]["commit"] == SHA
    monkeypatch.setattr(
        uc, "check_for_updates", lambda *_a, **_k: pytest.fail("GET 不能检查或写缓存")
    )
    client = TestClient(
        create_app(tmp_path / "absent", runtime_settings=settings(tmp_path, enabled=False)),
        base_url="http://127.0.0.1",
    )
    assert client.get("/api/update-status").json()["state"] == "DISABLED"
    assert not (tmp_path / "absent").exists()


def test_expired_cache_force_and_failure_backoff(tmp_path, monkeypatch):
    config = settings(tmp_path)
    path = tmp_path / "cache/status.json"
    path.parent.mkdir()
    old = uc._result(
        identity(), "UPDATE_AVAILABLE", checked_at=time.time() - 90000, remote={"commit": REMOTE}
    )
    old["last_success"] = dict(old)
    path.write_text(json.dumps(old))
    calls = []

    def failed(*_args):
        calls.append(1)
        raise OSError("offline")

    monkeypatch.setattr(uc, "_compare", failed)
    value = uc.check_for_updates(config, local=identity(), path=path)
    assert value["state"] == "CHECK_FAILED"
    assert value["last_success"]["remote"]["commit"] == REMOTE
    assert uc.check_for_updates(config, local=identity(), path=path)["cached"]
    assert len(calls) == 1
    uc.check_for_updates(config, force=True, local=identity(), path=path)
    assert len(calls) == 2


def test_cache_directory_unwritable_still_checks(tmp_path, monkeypatch):
    path = tmp_path / "file/status.json"
    path.parent.write_text("not a directory")
    monkeypatch.setattr(
        uc,
        "_compare",
        lambda _s, local, *_a: uc._result(local, "UP_TO_DATE", checked_at=time.time()),
    )
    assert (
        uc.check_for_updates(settings(tmp_path), local=identity(), path=path)["state"]
        == "UP_TO_DATE"
    )


@pytest.mark.parametrize(
    "code,headers",
    [(429, {"Retry-After": "5000"}), (403, {"X-RateLimit-Reset": str(int(time.time() + 8000))})],
)
def test_http_rate_limit(tmp_path, monkeypatch, code, headers):
    with pytest.raises(uc._CheckError) as caught:
        compare(
            monkeypatch,
            settings(tmp_path),
            identity(),
            lambda _: httpx.Response(code, headers=headers),
        )
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.retry_at > time.time() + 4500


def test_startup_failure_preserves_business_result(tmp_path, monkeypatch, capsys):
    import ftr.workbench
    from ftr import cli
    from ftr.models import OperationResponse

    monkeypatch.setenv("FTR_UPDATE_CHECK__ENABLED", "true")
    monkeypatch.setattr(uc, "_compare", lambda *_a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        ftr.workbench,
        "manage",
        lambda *_a, **_kw: {"state": "RUNNING", "url": "http://127.0.0.1:1"},
    )
    result = run(
        parser().parse_args(["--data-dir", str(tmp_path / "absent"), "workbench", "start"])
    )
    assert result.status == "RUNNING"
    assert "本次继续运行" in capsys.readouterr().err
    monkeypatch.setattr(
        cli, "run", lambda _args: OperationResponse(operation="collect", status="PARTIAL")
    )
    monkeypatch.setattr("sys.argv", ["ftr", "collect"])
    with pytest.raises(SystemExit) as exit_code:
        cli.main()
    assert exit_code.value.code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PARTIAL"


@pytest.mark.parametrize(
    "arguments",
    [["collect"], ["task", "resume", "--task", "missing"], ["workbench", "start"], ["serve"]],
)
def test_automatic_entrypoints(tmp_path, monkeypatch, arguments):
    import ftr.web.app
    import ftr.workbench

    calls = []
    monkeypatch.setattr(
        uc,
        "check_for_updates",
        lambda *_a, **_k: calls.append(1) or uc._result(identity(), "CHECK_FAILED"),
    )
    monkeypatch.setattr(ftr.workbench, "manage", lambda *_a, **_k: {"state": "RUNNING"})
    monkeypatch.setattr(ftr.web.app, "serve", lambda *_a, **_k: None)
    try:
        run(parser().parse_args(["--data-dir", str(tmp_path / "absent"), *arguments]))
    except (ValueError, KeyError):
        pass
    assert calls == [1]


def test_disabled_scheduler_no_auto_check(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "check_for_updates", lambda *_a, **_kw: pytest.fail("停用计划不检查"))
    value = run(parser().parse_args(["--data-dir", str(tmp_path / "absent"), "scheduler", "run"]))
    assert value.status == "DISABLED"


def test_monitor_lifespan_query_does_not_wait_for_network(tmp_path, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from ftr.web.app import create_app

    started = threading.Event()
    release = threading.Event()

    def slow(*_a, **_kw):
        started.set()
        release.wait(0.3)
        return uc._result(identity(), "CHECK_FAILED")

    monkeypatch.setattr(uc, "check_for_updates", slow)
    app = create_app(tmp_path / "absent", runtime_settings=settings(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert started.wait(1)
        start = time.monotonic()
        assert client.get("/api/update-status").status_code == 200
        assert time.monotonic() - start < 0.2
        release.set()
    assert app.state.update_monitor.thread is not None
    assert not app.state.update_monitor.thread.is_alive()


@pytest.mark.skipif(os.environ.get("FTR_WEB_BROWSER") != "1", reason="按需真实 Chromium")
def test_browser_update_notice_desktop_mobile_and_refresh(tmp_path, monkeypatch):
    import socket
    import threading

    import uvicorn
    from playwright.sync_api import expect, sync_playwright

    from ftr.repository import Repository
    from ftr.web.app import create_app

    repo = Repository(tmp_path / "db")
    repo.close()
    app = create_app(tmp_path / "db", runtime_settings=settings(tmp_path, enabled=False))
    value = uc._result(
        identity(), "UPDATE_AVAILABLE", remote={"commit": REMOTE}, checked_at=time.time()
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.clock.install()
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.02)
            app.state.update_monitor.snapshot = value
            page.goto(url)
            expect(page.locator("#update-notice")).to_contain_text("有新版本")
            assert page.locator("#update-notice a").get_attribute("href") == uc.PROJECT_URL
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            app.state.update_monitor.snapshot = {
                **value,
                "state": "CHECK_FAILED",
                "stale": True,
                "last_success": value,
                "restart_required": True,
            }
            page.clock.fast_forward(61000)
            expect(page.locator("#update-notice")).to_contain_text("请重启")
            assert "检查未完成" in page.locator("#update-notice").inner_text()
            assert "已过期" in page.locator("#update-notice").inner_text()
            app.state.update_monitor.snapshot = {**value, "state": "UNKNOWN"}
            page.locator("#refresh").click()
            expect(page.locator("#update-notice")).to_contain_text("无法确认")
            assert not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()


@pytest.mark.parametrize("dirty", [False, True])
def test_packaged_identity_and_invalid_metadata(tmp_path, monkeypatch, dirty):
    package = tmp_path / "site-packages/ftr"
    (package / "data").mkdir(parents=True)
    monkeypatch.setattr(uc, "__file__", str(package / "update_check.py"))
    path = package / "data/build-identity.json"
    path.write_text(
        json.dumps(
            {"repository": uc.REPOSITORY, "version": __version__, "commit": SHA, "dirty": dirty}
        )
    )
    value = uc.local_identity()
    assert value["install_type"] == "build" and value["commit"] == SHA and value["dirty"] == dirty
    path.write_text(
        json.dumps(
            {"repository": "other/repo", "version": __version__, "commit": SHA, "dirty": dirty}
        )
    )
    assert uc.local_identity()["commit"] is None
    path.write_text("not JSON")
    assert uc.local_identity()["commit"] is None


def test_redirect_and_cookies_not_forwarded(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.github.com"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(302, headers={"location": "https://other.invalid/"})

    with pytest.raises(uc._CheckError) as caught:
        compare(monkeypatch, settings(tmp_path), identity(), handler)
    assert caught.value.code == "HTTP_ERROR" and len(calls) == 1


def test_monitor_detects_content_change_while_already_dirty(tmp_path, monkeypatch):
    content = ["original"]
    monkeypatch.setattr(uc, "local_identity", lambda *_a: identity(dirty=True))
    monkeypatch.setattr(uc, "source_fingerprint", lambda: content[0])
    monitor = uc.UpdateMonitor(settings(tmp_path))
    monitor.freeze()
    content[0] = "changed while still dirty"
    monitor.refresh()
    assert monitor.read()["restart_required"]


def test_app_factory_does_not_spawn_git(tmp_path, monkeypatch):
    from ftr.web.app import create_app

    monkeypatch.setattr(uc, "_git", lambda *_a, **_kw: pytest.fail("构造 app 不执行 Git"))
    app = create_app(tmp_path / "absent", runtime_settings=settings(tmp_path))
    assert app.state.update_monitor.local["commit"] is None
    assert not (tmp_path / "absent").exists()


def test_identity_failure_does_not_block_check_or_monitor_start(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise OSError("unreadable source")

    monkeypatch.setattr(uc, "local_identity", unavailable)
    value = uc.check_for_updates(settings(tmp_path))
    assert value["state"] == "CHECK_FAILED" and value["error_code"] == "CHECK_UNAVAILABLE"
    monitor = uc.UpdateMonitor(settings(tmp_path, enabled=False))
    monitor.start()
    assert monitor.read()["state"] == "DISABLED"
    monitor.stop()


def test_monitor_identity_read_failure_does_not_kill_worker(tmp_path, monkeypatch):
    def unavailable(*_args):
        raise OSError("unreadable source")

    monkeypatch.setattr(uc, "local_identity", unavailable)
    monitor = uc.UpdateMonitor(settings(tmp_path))
    try:
        monitor.start()
        for _ in range(100):
            if monitor.read()["state"] == "CHECK_FAILED":
                break
            time.sleep(0.01)
        assert monitor.read()["state"] == "CHECK_FAILED"
        assert monitor.thread.is_alive()
    finally:
        monitor.stop()
    assert not monitor.thread.is_alive()
