"""Local workbench process ownership through a private authenticated control channel."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
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


class WorkbenchError(RuntimeError):
    def __init__(self, code, *, port=None, exit_code=None, log_summary=""):
        self.details = {
            "code": code,
            "stage": "startup",
            "port": port,
            "exit_code": exit_code,
            "log_summary": log_summary,
            "next_step": "检查工作台依赖、日志及端口后重试；不要手工改状态文件",
        }
        super().__init__(f"工作台启动失败：{code}；{self.details['next_step']}")


def _identity(state, timeout=1):
    port = state["port"]
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("工作台端口状态无效")
    with httpx.Client(trust_env=False, timeout=timeout) as client:
        result = client.get(
            f"http://127.0.0.1:{port}/_control", headers={"X-FTR-Token": state["token"]}
        )
        result.raise_for_status()
        identity = result.json()
    if (
        type(identity.get("pid")) is not int
        or identity["pid"] <= 0
        or identity.get("directory") != state["directory"]
        or ("instance_id" in state and identity.get("instance_id") != state["instance_id"])
    ):
        raise ValueError("服务身份不匹配")
    return identity


def _control(state, action="status", timeout=1):
    identity = _identity(state, timeout)
    if identity["pid"] != state["pid"]:
        raise ValueError("服务身份不匹配")
    if action == "stop":
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            result = client.post(
                f"http://127.0.0.1:{state['port']}/_control",
                headers={"X-FTR-Token": state["token"]},
            )
            result.raise_for_status()
    return identity


def _log_summary(path, offset, token):
    try:
        with path.open("rb") as handle:
            handle.seek(max(offset, path.stat().st_size - 2048))
            value = handle.read(2048).decode("utf-8", errors="replace")
        value = value.replace(token, "***")
        return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1***:***@", value)
    except OSError:
        return "日志不可读"


def _cleanup(process, state):
    # Authenticated child first: a launcher may wrap or detach the actual service.
    try:
        identity = _identity(state)
        _control({**state, "pid": identity["pid"]}, "stop")
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    if process.poll() is None:
        process.terminate()  # Only the handle spawned by this invocation.
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _state(root):
    path = root / ".workbench.json"
    if path.is_symlink():
        raise ValueError("工作台状态不得为符号链接")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


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


def manage(
    root,
    action,
    *,
    open_browser=False,
    web_settings=None,
    scheduler_settings=None,
    management=False,
    runtime_settings=None,
):
    root = root.resolve()
    if runtime_settings is not None:
        runtime_settings = runtime_settings.model_copy(update={"data_dir": root})
    if not (root / "database.sqlite3").is_file():
        raise ValueError("尚无资料库，请先明确范围并采集；不会自动创建资料库")
    if action in ("background-status", "background-stop"):
        from ftr.worker import identity, public_status, stop_owned

        if action == "background-status":
            return public_status(root)
        stop_owned(root)
        for _ in range(100):
            if identity(root) is None:
                return {"state": "STOPPED"}
            time.sleep(0.1)
        return {"state": "STOPPING", "next_step": "后台在安全检查点停止；等待请求超时后重查状态"}
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
        if current["state"] == "RUNNING" and bool(_state(root).get("management")) != management:
            raise ValueError("工作台模式不同；请先 workbench stop，再用 --manage 重新启动")
        if current["state"] != "RUNNING":
            # Fail before spawning if the optional extra is absent.
            try:
                import uvicorn  # noqa: F401

                from ftr.web.app import create_app  # noqa: F401
            except ImportError as exc:
                raise WorkbenchError("DEPENDENCIES_MISSING") from exc

            # Construct the app only in the owned child, not during dependency probing.
            from ftr.config import WebSettings

            settings = web_settings or WebSettings()
            deadline = time.monotonic() + settings.startup_timeout_seconds
            log_path = root / ".workbench.log"
            if log_path.is_symlink():
                raise ValueError("工作台日志路径无效")
            for port in range(settings.port, min(settings.port + 10, 65536)):
                if time.monotonic() >= deadline:
                    raise WorkbenchError("STARTUP_TIMEOUT", port=port)
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError:
                        continue
                token = secrets.token_hex(32)
                instance_id = secrets.token_hex(16)
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if key not in ("PYTHONHOME", "PYTHONPATH")
                }
                env.update(FTR_WORKBENCH_TOKEN=token, FTR_WORKBENCH_INSTANCE=instance_id)
                if runtime_settings is not None:
                    env["FTR_WORKBENCH_SETTINGS"] = runtime_settings.model_dump_json()
                command = [
                    sys.executable,
                    "-m",
                    "ftr.workbench",
                    "--directory",
                    str(root),
                    "--port",
                    str(port),
                    "--poll",
                    str(settings.poll_interval_ms),
                    "--timeout",
                    str(settings.request_timeout_ms),
                ]
                if management:
                    command += ["--manage"]
                if scheduler_settings is not None:
                    command += ["--scheduler-settings", scheduler_settings.model_dump_json()]
                options = (
                    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                    if sys.platform == "win32"
                    else {"start_new_session": True}
                )
                offset = log_path.stat().st_size if log_path.exists() else 0
                with log_path.open("ab") as log:
                    try:
                        process = subprocess.Popen(
                            command,
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            env=env,
                            **options,
                        )
                    except OSError as exc:
                        raise WorkbenchError("SPAWN_FAILED", port=port) from exc
                state = {
                    "management": management,
                    "launcher_pid": process.pid,
                    "port": port,
                    "token": token,
                    "directory": str(root),
                    "instance_id": instance_id,
                }
                code = "STARTUP_TIMEOUT"
                healthy = False
                registered = False
                try:
                    while time.monotonic() < deadline:
                        try:
                            identity = _identity(
                                state, min(1, max(0.01, deadline - time.monotonic()))
                            )
                            state["pid"] = identity["pid"]
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            with httpx.Client(trust_env=False, timeout=min(1, remaining)) as client:
                                client.get(
                                    f"http://127.0.0.1:{port}/api/overview"
                                ).raise_for_status()
                            healthy = True
                            break
                        except ValueError:
                            code = "IDENTITY_MISMATCH"
                            break
                        except httpx.HTTPStatusError as exc:
                            code = (
                                "IDENTITY_MISMATCH"
                                if exc.response.status_code == 403
                                else "HEALTH_FAILED"
                            )
                            break
                        except httpx.HTTPError:
                            if process.poll() not in (None, 0):
                                code = "PROCESS_EXITED"
                                break
                            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                    if healthy:
                        atomic_json(root / ".workbench.json", state)
                        os.chmod(root / ".workbench.json", 0o600)
                        current = status(root)
                        registered = current["state"] == "RUNNING"
                        if not registered:
                            raise WorkbenchError("IDENTITY_MISMATCH", port=port)
                        break
                finally:
                    if not registered:
                        _cleanup(process, state)
                summary = _log_summary(log_path, offset, token)
                # Only an explicit address-in-use startup race is retried on another port.
                if code == "PROCESS_EXITED" and (
                    "address already in use" in summary.lower() or "10048" in summary
                ):
                    continue
                raise WorkbenchError(code, port=port, exit_code=process.poll(), log_summary=summary)
            else:
                raise WorkbenchError("PORTS_OCCUPIED")
        if management:
            from ftr.config import RuntimeSettings, SchedulerSettings
            from ftr.management import effective_plan, operations
            from ftr.worker import ensure_worker

            runtime = runtime_settings or RuntimeSettings(
                data_dir=root, scheduler=scheduler_settings or SchedulerSettings()
            )
            if effective_plan(root, runtime.scheduler)["settings"]["enabled"] or any(
                item["state"] in ("QUEUED", "RUNNING") for item in operations(root, limit=None)
            ):
                try:
                    current["background"] = ensure_worker(runtime)
                except ValueError as exc:
                    current["background"] = {"state": "BLOCKED", "next_step": str(exc)}
        if open_browser:
            try:
                url = current["url"]
                if management:
                    state = _state(root)
                    with httpx.Client(trust_env=False, timeout=2) as client:
                        bootstrap = client.get(
                            url + "/_control/session", headers={"X-FTR-Token": state["token"]}
                        )
                        bootstrap.raise_for_status()
                        url += "#manage-token=" + bootstrap.json()["token"]
                current["browser_opened"] = bool(webbrowser.open(url))
            except Exception:  # noqa: BLE001
                current["browser_opened"] = False
        return current


def child():
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from ftr.config import RuntimeSettings, SchedulerSettings, WebSettings
    from ftr.web.app import create_app
    from ftr.web.management import ManagementSession

    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--poll", type=int, default=5000)
    parser.add_argument("--timeout", type=int, default=10000)
    parser.add_argument("--scheduler-settings")
    parser.add_argument("--manage", action="store_true")
    args = parser.parse_args()
    token = os.environ.pop("FTR_WORKBENCH_TOKEN")
    instance_id = os.environ.pop("FTR_WORKBENCH_INSTANCE", None)
    session = ManagementSession() if args.manage else None
    runtime = os.environ.pop("FTR_WORKBENCH_SETTINGS", None)
    app = create_app(
        args.directory,
        WebSettings(poll_interval_ms=args.poll, request_timeout_ms=args.timeout),
        SchedulerSettings.model_validate_json(args.scheduler_settings)
        if args.scheduler_settings
        else None,
        management_session=session,
        runtime_settings=RuntimeSettings.model_validate_json(runtime) if runtime else None,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
        )
    )

    # Request annotation must be a runtime type (function-local import + postponed annotations).
    def control(request):
        supplied = request.headers.get("X-FTR-Token", "")
        if not hmac.compare_digest(supplied, token):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.url.path == "/_control/session":
            if session is None:
                return JSONResponse({"error": "readonly"}, status_code=403)
            return {"token": session.issue()}
        identity = {
            "pid": os.getpid(),
            "directory": str(args.directory.resolve()),
            "instance_id": instance_id,
        }
        if request.method == "POST":
            server.should_exit = True
        return identity

    control.__annotations__["request"] = Request
    app.add_api_route("/_control", control, methods=["GET", "POST"], include_in_schema=False)
    app.add_api_route("/_control/session", control, methods=["GET"], include_in_schema=False)
    server.run()


if __name__ == "__main__":
    child()
