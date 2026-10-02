"""Discover supported browsers without touching users' browser profiles."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ftr.config import BrowserSettings


def candidates(playwright, settings: BrowserSettings):
    if settings.executable_path is not None:
        return [("explicit", settings.executable_path)]
    result = [("chromium", Path(playwright.chromium.executable_path))]
    if sys.platform == "darwin":
        roots = [Path("/Applications"), Path.home() / "Applications"]
        for name, app, binary in (
            ("edge", "Microsoft Edge.app", "Microsoft Edge"),
            ("chrome", "Google Chrome.app", "Google Chrome"),
        ):
            result.extend((name, root / app / "Contents/MacOS" / binary) for root in roots)
    elif sys.platform == "win32":
        roots = [
            Path(os.environ[key])
            for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
            if os.environ.get(key)
        ]
        for name, relative in (
            ("edge", "Microsoft/Edge/Application/msedge.exe"),
            ("chrome", "Google/Chrome/Application/chrome.exe"),
        ):
            result.extend((name, root / relative) for root in roots)
    return result


def launch_browser(playwright, settings: BrowserSettings, **options):
    attempted = []
    for name, path in candidates(playwright, settings):
        if not path.is_file():
            attempted.append({"name": name, "error": "NOT_FOUND"})
            continue
        try:
            browser = playwright.chromium.launch(
                headless=settings.headless,
                executable_path=str(path),
                timeout=settings.timeout_seconds * 1000,
                **options,
            )
            return browser, {"name": name, "path": str(path), "attempts": attempted}
        except Exception:  # noqa: BLE001
            attempted.append({"name": name, "error": "LAUNCH_FAILED"})
    error = RuntimeError(
        "显式浏览器不可用，请检查配置"
        if settings.executable_path
        else "没有可启动的浏览器，请运行 setup；Linux 检查系统库与显示环境"
    )
    error.attempts = attempted  # type: ignore[attr-defined]
    raise error
