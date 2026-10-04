"""Non-blocking, advisory checks against the fixed official GitHub repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import portalocker

from ftr.config import RuntimeSettings
from ftr.network import resolve_proxy

REPOSITORY = "anbang278/finance-tax-policy-acquisition"
BRANCH = "main"
PROJECT_URL = f"https://github.com/{REPOSITORY}"
API = f"https://api.github.com/repos/{REPOSITORY}"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
STATES = {
    "UP_TO_DATE",
    "UPDATE_AVAILABLE",
    "LOCAL_AHEAD",
    "DIVERGED",
    "UNKNOWN",
    "CHECK_FAILED",
    "DISABLED",
}


def _git(root: Path, *args: str, timeout: float = 0.5) -> subprocess.CompletedProcess:
    # Git optional locks are disabled: even status must not refresh the index.
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def project_root() -> Path | None:
    # Locate the code being executed, never the caller's unrelated working directory.
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src/ftr").is_dir():
        return root
    return None


def local_identity(root: Path | None = None) -> dict[str, Any]:
    from ftr import __version__

    root = root or project_root()
    identity: dict[str, Any] = {
        "version": __version__,
        "commit": None,
        "dirty": None,
        "install_type": "unknown",
    }
    if root is not None:
        identity["install_type"] = "archive"
        # Do not inherit a containing vault/repository's commit identity.
        if (root / ".git").exists():
            try:
                commit = _git(root, "rev-parse", "HEAD")
                status = _git(root, "status", "--porcelain", "--untracked-files=normal")
                if commit.returncode == 0 and _SHA.fullmatch(commit.stdout.strip()):
                    identity.update(
                        install_type="git",
                        commit=commit.stdout.strip(),
                        dirty=bool(status.stdout) if status.returncode == 0 else None,
                    )
            except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, AttributeError):
                pass
    if identity["commit"] is None:
        path = Path(__file__).parent / "data/build-identity.json"
        try:
            built = json.loads(path.read_text(encoding="utf-8"))
            if (
                built.get("repository") == REPOSITORY
                and built.get("version") == __version__
                and isinstance(built.get("dirty"), bool)
                and isinstance(built.get("commit"), str)
                and _SHA.fullmatch(built["commit"])
            ):
                identity.update(
                    install_type="build",
                    commit=built["commit"],
                    dirty=built["dirty"],
                )
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    return identity


def cache_path(root: Path | None = None) -> Path:
    if sys.platform == "win32":
        default = Path.home() / "AppData/Local"
        base = Path(os.environ.get("LOCALAPPDATA") or default).expanduser()
        if not base.is_absolute():
            base = default
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        default = Path.home() / ".cache"
        base = Path(os.environ.get("XDG_CACHE_HOME") or default).expanduser()
        if not base.is_absolute():
            base = default
    installation = str((root or project_root() or Path(__file__).parent).resolve())
    key = hashlib.sha256(installation.encode()).hexdigest()[:24]
    return base / "ftr/update-check" / key / "status.json"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("repository") == REPOSITORY:
            return value
    except (OSError, ValueError):
        pass
    return {}


def _write(path: Path, value: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _result(local: dict, state: str = "UNKNOWN", **extra: Any) -> dict:
    return {
        "state": state,
        "repository": REPOSITORY,
        "branch": BRANCH,
        "project_url": PROJECT_URL,
        "local": local,
        "remote": None,
        "basis": None,
        "checked_at": None,
        "cached": False,
        "stale": False,
        **extra,
    }


class _CheckError(Exception):
    def __init__(self, code: str, retry_at: float | None = None):
        self.code = code
        self.retry_at = retry_at


def _request(client: httpx.Client, path: str, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _CheckError("TIMEOUT")
    # Streaming bounds both wall time and response size, including large compare responses.
    with client.stream("GET", API + path, timeout=remaining) as response:
        if response.status_code in (403, 429):
            retry_at = None
            retry = response.headers.get("Retry-After")
            reset = response.headers.get("X-RateLimit-Reset")
            try:
                if retry:
                    try:
                        retry_at = time.time() + float(retry)
                    except ValueError:
                        from email.utils import parsedate_to_datetime

                        retry_at = parsedate_to_datetime(retry).timestamp()
                elif reset:
                    retry_at = float(reset)
            except (ValueError, TypeError, OverflowError):
                pass
            raise _CheckError("RATE_LIMITED", retry_at)
        if response.status_code == 404:
            raise _CheckError("COMMIT_NOT_FOUND")
        if response.status_code != 200:
            raise _CheckError("HTTP_ERROR")
        content = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise _CheckError("TIMEOUT")
            content.extend(chunk)
            if len(content) > 2_000_000:
                raise _CheckError("RESPONSE_TOO_LARGE")
        try:
            data = json.loads(content)
        except ValueError as exc:
            raise _CheckError("INVALID_RESPONSE") from exc
        if not isinstance(data, dict):
            raise _CheckError("INVALID_RESPONSE")
        return data


def _compare(settings: RuntimeSettings, local: dict, root: Path | None, deadline: float) -> dict:
    with httpx.Client(
        proxy=resolve_proxy(settings.network),
        trust_env=False,
        follow_redirects=False,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ftr-update-check"},
    ) as client:
        remote = _request(client, f"/commits/{BRANCH}", deadline)
        sha = remote.get("sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise _CheckError("INVALID_RESPONSE")
        result = _result(local, remote={"commit": sha}, checked_at=time.time())
        if not local.get("commit"):
            return {**result, "basis": "missing_local_identity"}
        if local["commit"] == sha:
            return {**result, "state": "UP_TO_DATE", "basis": "commit_identity"}
        if local["install_type"] == "git" and root:
            for base, head, state in (
                (local["commit"], sha, "UPDATE_AVAILABLE"),
                (sha, local["commit"], "LOCAL_AHEAD"),
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _CheckError("TIMEOUT")
                try:
                    ancestor = _git(
                        root, "merge-base", "--is-ancestor", base, head, timeout=min(0.5, remaining)
                    )
                    if ancestor.returncode == 0:
                        return {**result, "state": state, "basis": "local_ancestry"}
                except (OSError, subprocess.TimeoutExpired):
                    pass
        try:
            comparison = _request(
                client, f"/compare/{local['commit']}...{sha}?per_page=1", deadline
            )
        except _CheckError as exc:
            if exc.code == "COMMIT_NOT_FOUND":
                return {**result, "basis": "unrecognized_local_commit"}
            raise
        status = comparison.get("status")
        states = {
            "ahead": "UPDATE_AVAILABLE",
            "behind": "LOCAL_AHEAD",
            "diverged": "DIVERGED",
            "identical": "UP_TO_DATE",
        }
        if status not in states:
            raise _CheckError("INVALID_RESPONSE")
        return {
            **result,
            "state": states[status],
            "basis": "github_compare",
            "behind_commits": comparison.get("ahead_by"),
            "ahead_commits": comparison.get("behind_by"),
        }


def check_for_updates(
    settings: RuntimeSettings,
    *,
    force: bool = False,
    local: dict | None = None,
    root: Path | None = None,
    path: Path | None = None,
) -> dict:
    """Never propagate a check failure into the user's business operation."""
    try:
        local = local or local_identity(root)
        if not settings.update_check.enabled:
            return _result(local, "DISABLED")
        path = path or cache_path(root)
        return _check(settings, local, root or project_root(), path, force)
    except Exception:  # noqa: BLE001 - advisory boundary; never include credentials/errors
        return _result(local or {}, "CHECK_FAILED", error_code="CHECK_UNAVAILABLE")


def _check(
    settings: RuntimeSettings, local: dict, root: Path | None, path: Path, force: bool
) -> dict:
    now = time.time()
    policy = settings.update_check
    cached = _read(path)
    same = cached.get("local") == local
    retry_at = cached.get("retry_at", 0)
    checked = cached.get("checked_at")
    try:
        fresh = isinstance(checked, (float, int)) and 0 <= now - checked < policy.cache_seconds
        retrying = isinstance(retry_at, (float, int)) and now < retry_at
    except (TypeError, OverflowError):
        fresh = retrying = False
    server_retry = cached.get("server_retry_at", 0)
    if isinstance(server_retry, (int, float)) and now < server_retry:
        return _result(
            local,
            "CHECK_FAILED",
            error_code="RATE_LIMITED",
            cached=True,
            retry_at=server_retry,
            checked_at=cached.get("checked_at"),
            last_success=cached.get("last_success"),
            stale=True,
        )
    if (
        not force
        and same
        and cached.get("state") in STATES
        and ((fresh and cached.get("state") != "CHECK_FAILED") or retrying)
    ):
        return {**cached, "cached": True}
    lock = None
    writable = True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(
            str(path.with_suffix(".lock")),
            timeout=0,
            flags=portalocker.LOCK_EX | portalocker.LOCK_NB,
        )
        lock.acquire()
    except portalocker.exceptions.LockException:
        return _result(
            local,
            "CHECK_FAILED",
            error_code="CHECK_IN_PROGRESS",
            cached=False,
            last_success=cached.get("last_success"),
            stale=True,
        )
    except OSError:
        writable = False
    deadline = time.monotonic() + policy.timeout_seconds
    try:
        # A worker bounds DNS, proxy connection and slow-stream total wall time. It never writes.
        box: list = []
        done = threading.Event()

        def perform():
            try:
                box.append(_compare(settings, local, root, deadline))
            except Exception as exc:  # noqa: BLE001
                box.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=perform, name="ftr-update-request", daemon=True)
        thread.start()
        done.wait(policy.timeout_seconds)
        if not done.is_set():
            outcome: Any = _CheckError("TIMEOUT")
        else:
            outcome = box[0]
        if isinstance(outcome, dict):
            result = outcome
            result["last_success"] = {
                key: value for key, value in result.items() if key != "last_success"
            }
        else:
            code = outcome.code if isinstance(outcome, _CheckError) else "NETWORK_ERROR"
            retry = outcome.retry_at if isinstance(outcome, _CheckError) else None
            result = _result(
                local,
                "CHECK_FAILED",
                error_code=code,
                checked_at=now,
                server_retry_at=retry or 0,
                retry_at=max(now + policy.failure_retry_seconds, retry or 0),
                last_success=cached.get("last_success"),
                stale=True,
            )
        if writable:
            try:
                _write(path, result)
            except OSError:
                result["cache_error"] = True
        return result
    finally:
        if lock is not None:
            lock.release()


def notice(result: dict) -> str | None:
    state = result.get("state")
    local = result.get("local", {})
    if state == "DISABLED":
        return None
    if state == "UP_TO_DATE":
        return "基准提交与远端一致，本地有修改。" if local.get("dirty") else None
    messages = {
        "UPDATE_AVAILABLE": "检测到项目有新版本，建议任务结束后更新；本次继续运行。",
        "LOCAL_AHEAD": "本地提交领先官方 main。",
        "DIVERGED": "本地与官方 main 已分叉，更新前需人工核对。",
        "UNKNOWN": "缺少可验证的提交关系，无法确认是否最新。",
        "CHECK_FAILED": "GitHub 更新检查未完成，本次继续运行。",
    }
    message = messages.get(str(state))
    if message and state == "UPDATE_AVAILABLE":
        message += f" 本地 {str(local.get('commit') or '未知')[:7]} → main {str((result.get('remote') or {}).get('commit') or '未知')[:7]}。"
    if message and local.get("dirty"):
        message += " 本地有修改，更新前须保留。"
    return message


def emit_notice(result: dict) -> None:
    message = notice(result)
    if message:
        print(f"[FTR] {message}", file=sys.stderr)


def source_fingerprint() -> str | None:
    root = project_root()
    if root is None:
        return None
    fingerprint = hashlib.sha256()
    try:
        for path in sorted((root / "src/ftr").rglob("*")):
            if path.is_file() and path.suffix in {".py", ".js", ".css", ".html"}:
                fingerprint.update(str(path.relative_to(root)).encode())
                fingerprint.update(path.read_bytes())
        return fingerprint.hexdigest()
    except OSError:
        return "unavailable"


class UpdateMonitor:
    """Frozen running identity; API reads memory only, worker refreshes asynchronously."""

    def __init__(self, settings: RuntimeSettings):
        from ftr import __version__

        self.settings = settings
        self.local = {
            "version": __version__,
            "commit": None,
            "dirty": None,
            "install_type": "unknown",
        }
        self.fingerprint: str | None = None
        self.frozen = False
        self.snapshot = _result(
            self.local, "DISABLED" if not settings.update_check.enabled else "UNKNOWN"
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def freeze(self) -> None:
        if not self.frozen:
            try:
                self.local = local_identity()
                self.fingerprint = source_fingerprint()
            except Exception:  # noqa: BLE001 - missing advisory identity cannot stop the server
                self.fingerprint = "unavailable"
            self.frozen = True
            self.snapshot = _result(
                self.local, "DISABLED" if not self.settings.update_check.enabled else "UNKNOWN"
            )

    def start(self) -> None:
        self.freeze()
        if self.settings.update_check.enabled:
            self.thread = threading.Thread(target=self._run, name="ftr-update-monitor", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - keep the advisory worker alive on disk failures
                self.snapshot = _result(self.local, "CHECK_FAILED", error_code="CHECK_UNAVAILABLE")
            # Local drift is checked periodically; cached checks do not access GitHub.
            self.stop_event.wait(60)

    def refresh(self) -> None:
        self.freeze()
        value = check_for_updates(self.settings, local=self.local)
        if self.stop_event.is_set():
            return
        value["restart_required"] = (
            local_identity() != self.local or source_fingerprint() != self.fingerprint
        )
        self.snapshot = value

    def read(self) -> dict:
        return dict(self.snapshot)

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.settings.update_check.timeout_seconds + 0.2)
