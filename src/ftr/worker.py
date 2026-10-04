"""Owned, detached local writer with instance-authenticated control and scheduler lock."""

from __future__ import annotations

import hmac
import json
import os
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import portalocker

from ftr.config import RuntimeSettings
from ftr.management import effective_plan, operations, process_commands
from ftr.rules import atomic_json
from ftr.scheduler import Scheduler


class LoopbackControlServer(ThreadingHTTPServer):
    """Bind the fixed numeric loopback address without reverse DNS on startup."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def identity(root: Path) -> dict | None:
    path = root / ".worker.json"
    if path.is_symlink():
        raise ValueError("后台状态路径无效")
    try:
        state = json.loads(path.read_text())
        if (
            not isinstance(state, dict)
            or type(state.get("port")) is not int
            or not 1 <= state["port"] <= 65535
            or state.get("directory") != str(root.resolve())
            or not isinstance(state.get("token"), str)
        ):
            return None
        with httpx.Client(trust_env=False, timeout=1) as client:
            response = client.get(
                f"http://127.0.0.1:{state['port']}/control", headers={"X-FTR-Token": state["token"]}
            )
            response.raise_for_status()
            result = response.json()
        if any(result.get(key) != state.get(key) for key in ("instance_id", "directory", "pid")):
            return None
        if result["directory"] != str(root.resolve()):
            return None
        return {**state, **result}
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None


def public_status(root: Path) -> dict:
    state = identity(root)
    if state:
        return {k: v for k, v in state.items() if k not in ("token", "port")}
    return {"state": "STOPPED", "next_step": "本机管理操作会按需启动后台；电脑重启后须重新启动。"}


def ensure_worker(settings: RuntimeSettings) -> dict:
    root = settings.data_dir.resolve()
    if not (root / "database.sqlite3").is_file():
        raise ValueError("资料库不存在，不会自动初始化")
    with portalocker.Lock(str(root / ".worker-start.lock"), timeout=1):
        state = identity(root)
        if state:
            return public_status(root)
        lock_path = root / ".scheduler.lock"
        if lock_path.is_symlink():
            raise ValueError("调度锁路径无效")
        try:
            with portalocker.Lock(str(lock_path), timeout=0):
                pass
        except portalocker.exceptions.LockException as exc:
            raise ValueError(
                "已有独立调度器占用；请先明确停止原调度器后再接管，不修改 systemd"
            ) from exc
        token, instance = secrets.token_hex(32), secrets.token_hex(16)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("PYTHONHOME", "PYTHONPATH") and not key.startswith("FTR_")
        }
        env.update(
            FTR_WORKER_TOKEN=token,
            FTR_WORKER_INSTANCE=instance,
            FTR_WORKER_SETTINGS=settings.model_copy(update={"data_dir": root}).model_dump_json(),
        )
        log_path = root / ".worker.log"
        if log_path.is_symlink():
            raise ValueError("后台日志路径无效")
        options = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "ftr.worker"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **options,
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = identity(root)
            if state and state["instance_id"] == instance:
                return public_status(root)
            if process.poll() is not None:
                raise ValueError("后台启动失败；检查后台日志和独立调度器占用")
            time.sleep(0.05)
        # Stop only the spawned handle, never an unverified recorded PID.
        process.terminate()
        raise ValueError("后台握手超时")


def stop_owned(root: Path) -> bool:
    state = identity(root)
    if not state:
        return False
    with httpx.Client(trust_env=False, timeout=2) as client:
        response = client.post(
            f"http://127.0.0.1:{state['port']}/control", headers={"X-FTR-Token": state["token"]}
        )
        response.raise_for_status()
    return True


def recover_record(repo, settings, record_id, stop):
    from ftr.runtime import Collector

    collector = Collector(settings.data_dir, settings, stop_event=stop)
    collector.repo.close()
    collector.repo = repo
    try:
        return collector.recover_record(record_id)
    finally:
        # Shared transaction belongs to command processor.
        pass


def run(settings: RuntimeSettings, token: str, instance: str):
    root = settings.data_dir.resolve()
    stop, checkpoint = threading.Event(), threading.Event()
    scheduler = Scheduler(settings, stop_event=checkpoint)
    activity = {"state": "STARTING"}

    class Control(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path != "/control" or not hmac.compare_digest(
                self.headers.get("X-FTR-Token", ""), token
            ):
                self.send_error(403)
                return
            value = {
                "pid": os.getpid(),
                "directory": str(root),
                "instance_id": instance,
                "state": "RUNNING",
                "activity": activity,
                "at": datetime.now(UTC).isoformat(),
            }
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if self.path != "/control" or not hmac.compare_digest(
                self.headers.get("X-FTR-Token", ""), token
            ):
                self.send_error(403)
                return
            stop.set()
            checkpoint.set()
            self.do_GET()

    if (root / ".scheduler.lock").is_symlink():
        raise ValueError("调度锁路径无效")
    with portalocker.Lock(str(root / ".scheduler.lock"), timeout=0):
        server = LoopbackControlServer(("127.0.0.1", 0), Control)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        atomic_json(
            root / ".worker.json",
            {
                "token": token,
                "instance_id": instance,
                "pid": os.getpid(),
                "directory": str(root),
                "port": server.server_port,
            },
        )
        os.chmod(root / ".worker.json", 0o600)

        def watch():
            while not stop.wait(0.2):
                if activity.get("state") in ("COLLECTING", "RECOVERING") and any(
                    op["state"] == "QUEUED" and op["id"] != activity.get("operation_id")
                    for op in operations(root, limit=None)
                ):
                    checkpoint.set()
                atomic_json(
                    root / ".scheduler-heartbeat.json",
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "running": True,
                        "pid": os.getpid(),
                        "instance_id": instance,
                        "activity": activity,
                        "settings": effective_plan(root, settings.scheduler)["settings"],
                    },
                )

        threading.Thread(target=watch, daemon=True).start()

        def shutdown(*_args):
            stop.set()
            checkpoint.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, shutdown)
        # Collectors run outside main thread so they cannot replace worker shutdown handlers.
        try:
            while not stop.is_set():
                checkpoint.clear()
                try:
                    activity.update(state="APPLYING_COMMANDS")
                    process_commands(
                        settings,
                        on_operation=lambda operation_id, action: activity.update(
                            operation_id=operation_id,
                            state="RECOVERING"
                            if action == "review.recover"
                            else "APPLYING_COMMANDS",
                        ),
                        recover=lambda repo, rid: recover_record(repo, settings, rid, checkpoint),
                    )
                    if stop.is_set():
                        break
                    activity.update(state="IDLE")
                    result: dict = {}

                    def batch(result=result):
                        try:
                            result.update(scheduler.tick())
                        except Exception:  # noqa: BLE001
                            result.update(
                                state="FAILED", message="后台批次异常；检查任务诊断后处理来源"
                            )

                    thread = threading.Thread(target=batch)
                    thread.start()
                    while thread.is_alive():
                        activity.update(scheduler.activity)
                        thread.join(0.2)
                    activity.update(result)
                except RuntimeError as exc:
                    if "数据目录正由另一宿主使用" not in str(exc):
                        raise
                    activity.update(
                        state="WAITING_LOCK", next_step="等待其他 CLI 释放写锁；不会删除锁文件"
                    )
                    stop.wait(1)
                    continue
                stop.wait(0.5)
        finally:
            checkpoint.set()
            server.shutdown()
            server.server_close()
            atomic_json(
                root / ".scheduler-heartbeat.json",
                {
                    "at": datetime.now(UTC).isoformat(),
                    "running": False,
                    "pid": os.getpid(),
                    "instance_id": instance,
                    "activity": {"state": "STOPPED"},
                },
            )


if __name__ == "__main__":
    config = RuntimeSettings.model_validate_json(os.environ.pop("FTR_WORKER_SETTINGS"))
    run(config, os.environ.pop("FTR_WORKER_TOKEN"), os.environ.pop("FTR_WORKER_INSTANCE"))
