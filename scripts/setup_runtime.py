"""Finish setup using the installed interpreter; never collect or initialize the DB."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from ftr.config import load_settings
from ftr.environment import environment_report


def prepare(capability: str, project: Path) -> dict:
    if capability not in ("mof", "chinatax", "workbench", "all"):
        raise ValueError("未知能力")
    config = (
        Path(os.environ["FTR_CONFIG"]).expanduser().resolve()
        if os.environ.get("FTR_CONFIG")
        else project / "ftr.local.yaml"
    )
    if not config.exists():
        if os.environ.get("FTR_CONFIG"):
            raise ValueError("指定配置不存在，不自动覆盖或替换")
        try:
            with config.open("x", encoding="utf-8") as handle:
                handle.write((project / "ftr.example.yaml").read_text())
        except FileExistsError:
            pass
    settings = load_settings(config)
    browser = {
        "required": capability in ("chinatax", "all"),
        "launch_tested": False,
        "launched": False,
    }
    error = None
    if browser["required"]:
        if not environment_report(settings)["browser"]["file_exists"]:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                check=False,
            )
            if result.returncode:
                error = "Chromium 安装失败，请检查网络；系统依赖须由管理员处理"
        if error is None:
            browser["launch_tested"] = True
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    instance = playwright.chromium.launch(
                        headless=settings.browser.headless,
                        executable_path=str(settings.browser.executable_path)
                        if settings.browser.executable_path
                        else None,
                        timeout=settings.browser.timeout_seconds * 1000,
                    )
                    instance.close()
                browser["launched"] = True
            except Exception:  # noqa: BLE001
                error = "Chromium 无法启动；Linux 请检查系统库和显示环境/Xvfb，桌面请检查浏览器配置"
    report = environment_report(settings)
    ready = report["core_dependencies_ready"] and report["python"]["supported"] and error is None
    if capability in ("workbench", "all"):
        ready = ready and report["web_dependencies_ready"]
    return {
        "state": "ENVIRONMENT_READY" if ready else "BLOCKED",
        "python": str(Path(sys.executable).absolute()),
        "config": str(config),
        "data_dir": str(settings.data_dir),
        "checks": report,
        "browser": browser,
        "source_access_tested": False,
        "error": error,
    }


if __name__ == "__main__":
    try:
        result = prepare(sys.argv[1], Path(__file__).resolve().parent.parent)
    except Exception as exc:  # noqa: BLE001
        result = {
            "state": "BLOCKED",
            "error_type": type(exc).__name__,
            "error": "环境配置失败；保留已有配置，检查路径与参数",
        }
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["state"] == "ENVIRONMENT_READY" else 3)
