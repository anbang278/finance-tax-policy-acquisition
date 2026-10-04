"""在源码目录之外创建独立环境，验证 wheel、静态资源和只读 API。"""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from zipfile import ZipFile

project = Path(__file__).resolve().parents[1]
wheels = sorted((project / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
if not wheels:
    raise SystemExit("缺少 wheel，请先运行 uv build")
with ZipFile(wheels[-1]) as wheel:
    metadata_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
    metadata = wheel.read(metadata_name).decode("utf-8")
    assert "License-Expression: Apache-2.0" in metadata
    assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel.namelist())
    built_identity = json.loads(wheel.read("ftr/data/build-identity.json"))
    assert built_identity["repository"] == "anbang278/finance-tax-policy-acquisition"
    assert "Version: " + built_identity["version"] in metadata
sdists = sorted((project / "dist").glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
assert sdists, "缺少 sdist"
with tarfile.open(sdists[-1]) as source:
    name = next(
        name for name in source.getnames() if name.endswith("/src/ftr/data/build-identity.json")
    )
    handle = source.extractfile(name)
    assert handle is not None
    assert json.load(handle) == built_identity, "sdist → wheel 必须保留构建身份"
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
        if not key.startswith("FTR_") and key not in ("PYTHONPATH", "PYTHONHOME")
    }
    env.update(
        PYTHONUTF8="1",
        FTR_DATA_DIR=str(root / "资料库 #"),
        FTR_NETWORK__PROXY_MODE="direct",
        FTR_UPDATE_CHECK__ENABLED="false",
    )
    for arguments in (
        ["update", "check"],
        ["config", "validate"],
        ["doctor"],
        ["schema"],
        ["scheduler", "preview"],
        ["scheduler", "status"],
        ["scheduler", "run"],
    ):
        result = subprocess.run(
            [str(python), "-m", "ftr.cli", *arguments],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        assert json.loads(result.stdout)["status"] in (
            "COMPLETED",
            "LOCAL_DEMO_READY",
            "NOT_STARTED",
            "DISABLED",
        )
    assert not (root / "资料库 #").exists()
    code = """
import asyncio
from importlib.resources import files
import json
from ftr.update_check import local_identity
artifact_identity = json.loads(files("ftr").joinpath("data/build-identity.json").read_text())
assert local_identity()["commit"] == artifact_identity["commit"]
if artifact_identity["commit"]:
    assert local_identity()["install_type"] == "build"
import httpx
from ftr.config import load_settings, WebSettings
from ftr.repository import Repository
from ftr.web.app import create_app
settings = load_settings()
repo = Repository(settings.data_dir)
from datetime import date
from ftr.models import TaskRequest
from pydantic import ValidationError
try:
    TaskRequest.model_validate({"sources": ["mof"], "date_from":"2026-09-01", "date_to":"2026-09-30"})
except ValidationError:
    pass
else:
    raise AssertionError("未知范围字段没有拒绝")
task = repo.create_task(TaskRequest(source_ids=["mof"],date_from=date(2026,9,1),date_to=date(2026,9,30)))
assert repo.db.execute("PRAGMA user_version").fetchone()[0] == 4
repo.close()
from ftr.cli import parser,run
before = (settings.data_dir / "database.sqlite3").read_bytes()
for args in (["task","list"], ["task","report","--task",task], ["task","missing","--task",task], ["search","--query",""]):
    assert run(parser().parse_args(args)).status in ("CREATED","COMPLETED")
assert (settings.data_dir / "database.sqlite3").read_bytes() == before
assert (files("ftr") / "data" / "ftr.example.yaml").is_file()
from ftr.browser import candidates
from ftr.diagnostics import limitation_reasons
assert limitation_reasons(["历史限制"])[0]["code"] == "other_historical"
assert settings.network.retry_attempts == 3
assert settings.collection.progress_interval_seconds == 30
assert settings.web.startup_timeout_seconds == 15
assert (files("ftr") / "data" / "rule-fixtures.json").is_file()
from ftr.rules import Rules, extract_listing
import json
for sample in json.loads((files("ftr") / "data" / "rule-fixtures.json").read_text()):
    assert extract_listing(Rules(source_id=sample["source_id"]), sample["listing"].encode(), sample["url"], 1)[0]
async def verify():
    app = create_app(settings.data_dir, WebSettings(host="0.0.0.0"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://192.168.1.20") as client:
        for path in ("/", "/static/app.js", "/static/manage.js", "/static/style.css", "/static/favicon.svg", "/api/ui-config", "/api/overview", "/api/policies", "/api/tasks", "/api/sources", "/api/scheduler", "/api/update-status", "/api/manage/plan", "/api/manage/operations", "/api/reviews"):
            result = await client.get(path)
            assert result.status_code == 200, (path, result.status_code)
        assert not (await client.get("/api/manage/session")).json()["management_enabled"]
        assert (await client.post("/api/manage/session",json={"token":"untrusted"})).status_code == 403
        report = await client.get(f"/api/tasks/{task}/report")
        assert report.status_code == 200 and report.json()["report"]["unseen_matches"] is None
        missing = await client.get(f"/api/tasks/{task}/missing")
        assert missing.status_code == 200
asyncio.run(verify())
from ftr.workbench import manage
try:
    assert manage(settings.data_dir, "start")["state"] == "RUNNING"
    assert manage(settings.data_dir, "status")["state"] == "RUNNING"
    state = json.loads((settings.data_dir / ".workbench.json").read_text())
    assert state["instance_id"] and state["launcher_pid"] and state["pid"]
finally:
    manage(settings.data_dir, "stop")
from ftr.management import effective_plan
from ftr.worker import identity, stop_owned
import time
try:
    result = manage(settings.data_dir, "start", management=True, runtime_settings=settings)
    state = json.loads((settings.data_dir / ".workbench.json").read_text())
    with httpx.Client(base_url=result["url"],trust_env=False) as client:
        token = client.get("/_control/session",headers={"X-FTR-Token":state["token"]}).json()["token"]
        origin={"Origin":result["url"]}
        assert client.post("/api/manage/session",json={"token":token},headers=origin).status_code == 200
        assert client.post("/api/manage/commands",json={"id":"a"*32,"action":"plan.disable","expected_revision":0,"payload":{}},headers=origin).status_code == 202
        for _ in range(200):
            if effective_plan(settings.data_dir)["revision"] == 1:break
            time.sleep(.05)
        else:raise AssertionError("后台未应用命令")
    manage(settings.data_dir,"stop")
    assert identity(settings.data_dir)
    assert effective_plan(settings.data_dir)["config_source"] == "persistent_local_plan"
finally:
    manage(settings.data_dir,"stop")
    stop_owned(settings.data_dir)
    for _ in range(200):
        if identity(settings.data_dir) is None:break
        time.sleep(.05)
    assert identity(settings.data_dir) is None
print("独立 wheel、模板、静态资源、只读 API、本机管理会话和脱离页面的后台：PASS")
"""
    subprocess.run([str(python), "-c", code], cwd=root, env=env, check=True)
