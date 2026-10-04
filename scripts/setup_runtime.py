"""Finish setup using the installed interpreter; never collect or initialize the DB."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from ftr.browser import launch_browser
from ftr.config import load_settings
from ftr.environment import environment_report


def project_version(project: Path) -> dict:
    from importlib.metadata import version

    hashes = {
        name: hashlib.sha256((project / name).read_bytes()).hexdigest()
        for name in (
            "pyproject.toml",
            "uv.lock",
            "scripts/setup_runtime.py",
            "scripts/run.sh",
            "scripts/run.ps1",
            "src/ftr/browser.py",
            "src/ftr/runtime.py",
            "src/ftr/network.py",
            "src/ftr/workbench.py",
            "src/ftr/config.py",
        )
        if (project / name).is_file()
    }
    commit = None
    if (project / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                commit = result.stdout.strip()
        except OSError:
            pass
    return {"package": version("finance-tax-research"), "commit": commit, "sha256": hashes}


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
                handle.write((project / "ftr.example.yaml").read_text(encoding="utf-8"))
        except FileExistsError:
            pass
    settings = load_settings(config)
    browser = {
        "required": capability in ("chinatax", "all"),
        "launch_tested": False,
        "launched": False,
    }
    error = None
    selected = None
    if browser["required"]:
        from playwright.sync_api import sync_playwright

        browser["launch_tested"] = True
        with sync_playwright() as playwright:
            try:
                instance, selected = launch_browser(playwright, settings.browser)
                instance.close()
                browser["launched"] = True
            except RuntimeError:
                if settings.browser.executable_path is not None:
                    error = "显式浏览器不可用；请检查配置，不自动替换"
                else:
                    result = subprocess.run(
                        [sys.executable, "-m", "playwright", "install", "chromium"],
                        capture_output=True,
                        check=False,
                    )
                    if result.returncode:
                        error = "Chromium 安装失败，请检查网络；系统依赖须由管理员处理"
                    else:
                        try:
                            instance, selected = launch_browser(playwright, settings.browser)
                            instance.close()
                            browser["launched"] = True
                        except RuntimeError:
                            error = "Chromium 无法启动；请检查系统库、显示环境或浏览器策略"
        browser["selected"] = selected
    report = environment_report(settings)
    if selected:
        report["browser"].update(path=selected["path"], file_exists=True, launch_tested=True)
    ready = report["core_dependencies_ready"] and report["python"]["supported"] and error is None
    if capability in ("workbench", "all"):
        ready = ready and report["web_dependencies_ready"]
    from ftr.update_check import check_for_updates, emit_notice

    update = check_for_updates(settings, root=project) if ready else None
    if update:
        emit_notice(update)
    return {
        "update_check": update,
        "state": "ENVIRONMENT_READY" if ready else "BLOCKED",
        "stage": "verification",
        "capability": capability,
        "error_code": "BROWSER_UNAVAILABLE"
        if error
        else (None if ready else "DEPENDENCIES_UNAVAILABLE"),
        "next_step": "使用运行入口执行原请求；采集须明确来源与日期"
        if ready
        else "检查网络、浏览器配置或系统依赖后重试 setup",
        "runner": str(project / "scripts" / ("run.ps1" if sys.platform == "win32" else "run.sh")),
        "version": project_version(project),
        "python": str(Path(sys.executable).absolute()),
        "config": str(config),
        "data_dir": str(settings.data_dir),
        "checks": report,
        "browser": browser,
        "source_access_tested": False,
        "error": error,
    }


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        result = prepare(sys.argv[1], Path(__file__).resolve().parent.parent)
    except Exception as exc:  # noqa: BLE001
        result = {
            "state": "BLOCKED",
            "stage": "configuration",
            "error_code": "CONFIGURATION_ERROR",
            "next_step": "检查配置及路径后重试，已有配置保留",
            "error_type": type(exc).__name__,
            "error": "环境配置失败；保留已有配置，检查路径与参数",
        }
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["state"] == "ENVIRONMENT_READY" else 3)
