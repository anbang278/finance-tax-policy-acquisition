"""在源码目录之外创建独立环境，验证 wheel、静态资源和只读 API。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
wheels = sorted((project / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
if not wheels:
    raise SystemExit("缺少 wheel，请先运行 uv build")
with tempfile.TemporaryDirectory(prefix="ftr-wheel-") as temporary:
    root = Path(temporary)
    environment = root / "venv"
    subprocess.run(["uv", "venv", "--python", "3.13", str(environment)], check=True)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), f"{wheels[-1]}[web]"],
        check=True,
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("FTR_") and key != "PYTHONPATH"
    }
    env.update(
        PYTHONUTF8="1",
        FTR_DATA_DIR=str(root / "资料库 #"),
        FTR_NETWORK__PROXY_MODE="direct",
    )
    for arguments in (["config", "validate"], ["doctor"]):
        result = subprocess.run(
            [str(python), "-m", "ftr.cli", *arguments],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert json.loads(result.stdout)["status"] in ("COMPLETED", "LOCAL_DEMO_READY")
    assert not (root / "资料库 #").exists()
    code = """
import asyncio
from importlib.resources import files
import httpx
from ftr.config import load_settings, WebSettings
from ftr.repository import Repository
from ftr.web.app import create_app
settings = load_settings()
repo = Repository(settings.data_dir)
repo.close()
assert (files("ftr") / "data" / "ftr.example.yaml").is_file()
async def verify():
    app = create_app(settings.data_dir, WebSettings(host="0.0.0.0"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.20") as client:
        for path in ("/", "/static/app.js", "/static/style.css", "/static/favicon.svg", "/api/ui-config", "/api/overview", "/api/policies", "/api/tasks", "/api/sources"):
            result = await client.get(path)
            assert result.status_code == 200, (path, result.status_code)
asyncio.run(verify())
print("独立 wheel、打包配置模板、静态资源和只读 API：PASS")
"""
    subprocess.run([str(python), "-c", code], cwd=root, env=env, check=True)
