import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("setup_runtime", PROJECT / "scripts/setup_runtime.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def project(tmp_path):
    root = tmp_path / "中文 project"
    root.mkdir()
    (root / "ftr.example.yaml").write_text("data_dir: ./isolated-data\n")
    return root


def test_prepare_repeated_mof_workbench_without_browser_or_database(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.delenv("FTR_CONFIG", raising=False)
    monkeypatch.delenv("FTR_DATA_DIR", raising=False)
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("财政部或工作台不应安装浏览器"),
    )
    assert setup.prepare("mof", root)["state"] == "ENVIRONMENT_READY"
    config = root / "ftr.local.yaml"
    config.write_text("data_dir: ./changed\n")
    assert setup.prepare("workbench", root)["config"] == str(config)
    assert "changed" in config.read_text()
    assert not (root / "changed").exists()


def test_config_absent_or_invalid_kept(tmp_path, monkeypatch):
    root = project(tmp_path)
    config = tmp_path / "explicit.yaml"
    monkeypatch.setenv("FTR_CONFIG", str(config))
    with pytest.raises(ValueError, match="指定配置不存在"):
        setup.prepare("mof", root)
    config.write_text("unknown: secret\n")
    with pytest.raises(ValueError):
        setup.prepare("mof", root)
    assert config.read_text() == "unknown: secret\n"


def test_missing_browser_install_failure_no_collection(tmp_path, monkeypatch):
    root = project(tmp_path)
    monkeypatch.delenv("FTR_CONFIG", raising=False)
    original = setup.environment_report

    def report(settings):
        value = original(settings)
        value["browser"]["file_exists"] = False
        return value

    monkeypatch.setattr(setup, "environment_report", report)
    monkeypatch.setattr(
        setup.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1)
    )
    result = setup.prepare("chinatax", root)
    assert result["state"] == "BLOCKED"
    assert result["source_access_tested"] is False
    assert not (root / "isolated-data").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX bootstrap execution; Windows has its own CI validation"
)
def test_shell_uses_absolute_environment_and_capability_extras(tmp_path):
    root = project(tmp_path)
    scripts = root / "scripts"
    shutil.copytree(PROJECT / "scripts", scripts)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$UV_PROJECT_ENVIRONMENT $*" >> "$SETUP_TEST_LOG"\n'
    )
    fake_uv.chmod(0o700)
    venv_bin = root / ".venv/bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    (venv_bin / "python").chmod(0o700)
    log = tmp_path / "commands"
    env = {
        key: value for key, value in os.environ.items() if key not in ("FTR_CONFIG", "FTR_DATA_DIR")
    }
    env.update(
        PATH=str(fake_bin) + os.pathsep + env["PATH"],
        SETUP_TEST_LOG=str(log),
        UV_PROJECT_ENVIRONMENT=str(tmp_path / "unrelated-env"),
    )
    for capability in ("mof", "workbench"):
        result = subprocess.run(
            ["sh", str(scripts / "setup.sh"), capability],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["source_access_tested"] is False
        assert report["config"] == str(root / "ftr.local.yaml")
    commands = log.read_text().splitlines()
    assert "--extra web" not in commands[0]
    assert "--extra web" in commands[1]
    assert "--frozen" in commands[0]
    assert str(root / ".venv") in commands[0]
    assert not (tmp_path / "unrelated-env").exists()
    assert not (root / "isolated-data").exists()


def test_browser_install_then_launch_blank_page(tmp_path, monkeypatch):
    from contextlib import nullcontext

    root = project(tmp_path)
    monkeypatch.delenv("FTR_CONFIG", raising=False)
    report = setup.environment_report
    calls = []

    def missing(settings):
        value = report(settings)
        value["browser"]["file_exists"] = False
        return value

    monkeypatch.setattr(setup, "environment_report", missing)
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    import playwright.sync_api

    monkeypatch.setattr(
        playwright.sync_api,
        "sync_playwright",
        lambda: nullcontext(
            SimpleNamespace(
                chromium=SimpleNamespace(launch=lambda **_: SimpleNamespace(close=lambda: None))
            )
        ),
    )
    result = setup.prepare("chinatax", root)
    assert result["state"] == "ENVIRONMENT_READY"
    assert result["browser"]["launch_tested"] and result["browser"]["launched"]
    assert calls[0][-2:] == ["install", "chromium"]
    assert result["source_access_tested"] is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS missing-uv bootstrap path")
def test_missing_uv_checksum_failure_never_executes_archive(tmp_path):
    root = project(tmp_path)
    scripts = root / "scripts"
    shutil.copytree(PROJECT / "scripts", scripts)
    fake = tmp_path / "bin"
    fake.mkdir()
    for name in ("dirname", "mkdir", "mktemp", "rm", "cut"):
        (fake / name).symlink_to(shutil.which(name))
    (fake / "uname").write_text(
        '#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else echo arm64; fi\n'
    )
    (fake / "curl").write_text('#!/bin/sh\nfor last do :; done\nprintf wrong > "$last"\n')
    (fake / "shasum").write_text("#!/bin/sh\necho mismatch\n")
    for name in ("uname", "curl", "shasum"):
        (fake / name).chmod(0o700)
    env = dict(os.environ, PATH=str(fake))
    # HOME is passed through unchanged; choose a shell-local test substitution of bootstrap directory.
    script = (
        (scripts / "setup.sh")
        .read_text()
        .replace("$HOME/.local/share/ftr/bootstrap/bin", str(tmp_path / "user-bin"))
    )
    (scripts / "setup.sh").write_text(script)
    result = subprocess.run(
        ["/bin/sh", str(scripts / "setup.sh"), "mof"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    assert "校验失败" in result.stderr
    assert not (root / ".venv").exists()
