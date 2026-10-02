"""Local workbench process ownership through a private authenticated control channel."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx
import portalocker

from ftr.rules import atomic_json


def _control(state, action="status"):
    if action == "stop":
        _control(state)  # Verify before sending any mutation to the private endpoint.
    port = state["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("工作台端口状态无效")
    with httpx.Client(trust_env=False, timeout=1) as client:
        result = client.request(
            "POST" if action == "stop" else "GET",
            f"http://127.0.0.1:{port}/_control",
            headers={"X-FTR-Token": state["token"]},
        )
        result.raise_for_status()
        identity = result.json()
    if identity["pid"] != state["pid"] or identity["directory"] != state["directory"]:
        raise ValueError("服务身份不匹配")
    return identity


def _state(root):
    path = root / ".workbench.json"
    if path.is_symlink():
        raise ValueError("工作台状态不得为符号链接")
    return json.loads(path.read_text()) if path.exists() else None


def status(root):
    state = _state(root)
    if state:
        try:
            _control(state)
            if state["directory"] != str(root.resolve()):
                raise ValueError("资料目录不匹配")
            return {
                "state": "RUNNING",
                "pid": state["pid"],
                "url": f"http://127.0.0.1:{state['port']}",
            }
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    return {"state": "STOPPED"}


def manage(root, action, *, open_browser=False, web_settings=None):
    root = root.resolve()
    if not (root / "database.sqlite3").is_file():
        raise ValueError("尚无资料库，请先明确范围并采集；不会自动创建资料库")
    if action == "status":
        return status(root)
    lock_path = root / ".workbench.lock"
    if lock_path.is_symlink():
        raise ValueError("工作台锁路径无效")
    with portalocker.Lock(str(lock_path), timeout=0):
        current = status(root)
        if action == "stop":
            if current["state"] == "RUNNING":
                _control(_state(root), "stop")
                for _ in range(50):
                    if status(root)["state"] == "STOPPED":
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("已请求停止，进程仍在退出中")
            return {"state": "STOPPED"}
        if action != "start":
            raise ValueError("未知工作台动作")
        if current["state"] != "RUNNING":
            # Fail before spawning if the optional extra is absent.
            import uvicorn  # noqa: F401

            from ftr.web.app import create_app

            create_app(root)
            for port in range(8765, 8775):
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError:
                        continue
                token = secrets.token_hex(32)
                env = dict(os.environ, FTR_WORKBENCH_TOKEN=token)
                command = [
                    sys.executable,
                    "-m",
                    "ftr.workbench",
                    "--directory",
                    str(root),
                    "--port",
                    str(port),
                ]
                if web_settings:
                    command += [
                        "--poll",
                        str(web_settings.poll_interval_ms),
                        "--timeout",
                        str(web_settings.request_timeout_ms),
                    ]
                options = (
                    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if sys.platform == "win32"
                    else {"start_new_session": True}
                )
                with (root / ".workbench.log").open("ab") as log:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        env=env,
                        **options,
                    )
                state = {"pid": process.pid, "port": port, "token": token, "directory": str(root)}
                healthy = False
                for _ in range(100):
                    if process.poll() is not None:
                        break
                    try:
                        _control(state)
                        with httpx.Client(trust_env=False, timeout=1) as client:
                            client.get(f"http://127.0.0.1:{port}/api/overview").raise_for_status()
                        healthy = True
                        break
                    except (httpx.HTTPError, ValueError, KeyError):
                        time.sleep(0.1)
                if healthy:
                    atomic_json(root / ".workbench.json", state)
                    os.chmod(root / ".workbench.json", 0o600)
                    current = status(root)
                    break
                if process.poll() is None:
                    process.terminate()  # Only the process handle created by this invocation.
                process.wait(timeout=10)
            else:
                raise RuntimeError("本地十个候选端口均不可用或启动失败；查看 .workbench.log")
        if open_browser:
            try:
                current["browser_opened"] = bool(webbrowser.open(current["url"]))
            except Exception:  # noqa: BLE001
                current["browser_opened"] = False
        return current


def child():
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from ftr.config import WebSettings
    from ftr.web.app import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--poll", type=int, default=5000)
    parser.add_argument("--timeout", type=int, default=10000)
    args = parser.parse_args()
    token = os.environ.pop("FTR_WORKBENCH_TOKEN")
    app = create_app(
        args.directory, WebSettings(poll_interval_ms=args.poll, request_timeout_ms=args.timeout)
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    )

    # Request annotation must be a runtime type (function-local import + postponed annotations).
    def control(request):
        supplied = request.headers.get("X-FTR-Token", "")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        identity = {"pid": os.getpid(), "directory": str(args.directory.resolve())}
        if request.method == "POST":
            server.should_exit = True
        return identity

    control.__annotations__["request"] = Request
    app.add_api_route("/_control", control, methods=["GET", "POST"], include_in_schema=False)
    server.run()


if __name__ == "__main__":
    child()
