from __future__ import annotations

import os
import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from ftr.browser import candidates
from ftr.config import RuntimeSettings


def environment_report(settings: RuntimeSettings) -> dict[str, Any]:
    dependencies: dict[str, dict[str, Any]] = {}
    required = (
        "pydantic",
        "httpx",
        "lxml",
        "pypdf",
        "playwright",
        "PyYAML",
        "requests",
        "portalocker",
    )
    for name in (*required, "fastapi", "uvicorn"):
        try:
            dependencies[name] = {"installed": True, "version": version(name)}
        except PackageNotFoundError:
            dependencies[name] = {"installed": False, "version": None}
    browser_path = settings.browser.executable_path
    driver_error = False
    available_browsers = []
    if browser_path is None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                available_browsers = [
                    {"name": name, "path": str(path), "file_exists": path.is_file()}
                    for name, path in candidates(playwright, settings.browser)
                ]
                browser_path = next(
                    (Path(item["path"]) for item in available_browsers if item["file_exists"]),
                    Path(playwright.chromium.executable_path),
                )
        except Exception:  # noqa: BLE001
            driver_error = True
    display_available = (
        settings.browser.headless
        or not sys.platform.startswith("linux")
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    return {
        "configuration": {"valid": True},
        "python": {
            "version": platform.python_version(),
            "supported": sys.version_info[:2] == (3, 13),
        },
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "dependencies": dependencies,
        "core_dependencies_ready": all(dependencies[name]["installed"] for name in required),
        "web_dependencies_ready": all(
            dependencies[name]["installed"] for name in ("fastapi", "uvicorn")
        ),
        "browser": {
            "path": str(browser_path) if browser_path else None,
            "candidates": available_browsers,
            "file_exists": bool(browser_path and browser_path.is_file()),
            "driver_error": driver_error,
            "launch_tested": False,
        },
        "display": {
            "available": display_available,
            "headless": settings.browser.headless,
            "connection_tested": False,
        },
        "data": {
            "directory": str(settings.data_dir),
            "database_exists": (settings.data_dir / "database.sqlite3").is_file(),
        },
        "source_access_tested": False,
    }
