import json
from datetime import date

import pytest

from ftr.cli import parser, run
from ftr.config import RuntimeSettings, load_settings
from ftr.models import TaskRequest
from ftr.network import resolve_proxy
from ftr.runtime import BudgetReached, Collector


def test_precedence_and_path_bases(tmp_path):
    config = tmp_path / "配置 目录" / "runtime.yaml"
    config.parent.mkdir()
    config.write_text("data_dir: ./资料\nweb:\n  port: 9001\n", encoding="utf-8")
    settings = load_settings(config, environ={})
    assert settings.data_dir == (config.parent / "资料").resolve()
    settings = load_settings(
        config,
        environ={"FTR_DATA_DIR": "./环境资料", "FTR_WEB__PORT": "9002"},
        overrides={"web": {"port": 9003}},
    )
    assert settings.data_dir.name == "环境资料"
    assert settings.data_dir.parent == __import__("pathlib").Path.cwd()
    assert settings.web.port == 9003
    assert settings.origins["web.port"] == "cli"


@pytest.mark.parametrize(
    "content",
    [
        "unknown: true",
        "web: {port: 0}",
        "browser: {headless: maybe}",
        "network: {proxy_mode: explicit}",
        "collection: {max_pages: 1001}",
        "network: {timeout_seconds: .nan}",
        "data_dir: ''",
        "- not-a-mapping",
        "web: {port: 8765, port: 9000}",
        "network: {timeout_seconds: true}",
        "browser: {timeout_seconds: '30'}",
    ],
)
def test_invalid_configuration_has_no_data_side_effect(tmp_path, content):
    config = tmp_path / "invalid.yaml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        run(
            parser().parse_args(
                [
                    "--config",
                    str(config),
                    "--data-dir",
                    str(tmp_path / "data"),
                    "collect",
                    "--date-from",
                    "2026-09-01",
                    "--date-to",
                    "2026-09-02",
                ]
            )
        )
    assert not (tmp_path / "data").exists()


def test_missing_config_and_unknown_environment_are_rejected(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml", environ={})
    with pytest.raises(ValueError, match="FTR_WEB__TYPO"):
        load_settings(environ={"FTR_WEB__TYPO": "1"})


def test_environment_can_clear_optional_proxy_when_switching_mode(tmp_path):
    config = tmp_path / "proxy.yaml"
    config.write_text(
        "network:\n  proxy_mode: explicit\n  proxy_url: http://127.0.0.1:7890\n", encoding="utf-8"
    )
    settings = load_settings(
        config, environ={"FTR_NETWORK__PROXY_MODE": "direct", "FTR_NETWORK__PROXY_URL": ""}
    )
    assert settings.network.proxy_mode == "direct" and settings.network.proxy_url is None


def test_doctor_reports_local_checks_without_creating_data(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path / "not-created"))
    monkeypatch.setenv("FTR_BROWSER__EXECUTABLE_PATH", str(tmp_path / "missing-browser"))
    result = run(parser().parse_args(["doctor"]))
    assert result.status == "LOCAL_DEMO_READY"
    checks = result.data["checks"]
    assert checks["source_access_tested"] is False
    assert checks["browser"]["file_exists"] is False
    assert checks["browser"]["launch_tested"] is False
    assert not (tmp_path / "not-created").exists()


def test_config_commands_are_readonly_and_redact_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path / "not-created"))
    monkeypatch.setenv("FTR_NETWORK__PROXY_MODE", "explicit")
    monkeypatch.setenv("FTR_NETWORK__PROXY_URL", "http://alice:secret@127.0.0.1:7890")
    for action in ("show", "validate"):
        result = run(parser().parse_args(["config", action]))
        rendered = result.model_dump_json()
        assert "secret" not in rendered and "alice" not in rendered
        assert result.status == "COMPLETED"
    assert not (tmp_path / "not-created").exists()


def test_proxy_modes_do_not_read_system_in_direct_or_explicit(monkeypatch):
    monkeypatch.setattr("ftr.network.getproxies", lambda: {"https": "http://127.0.0.1:7890"})
    assert resolve_proxy(RuntimeSettings().network) == "http://127.0.0.1:7890"
    direct = RuntimeSettings(network={"proxy_mode": "direct"})
    assert resolve_proxy(direct.network) is None
    explicit = RuntimeSettings(
        network={"proxy_mode": "explicit", "proxy_url": "http://proxy.example:8080"}
    )
    assert resolve_proxy(explicit.network) == "http://proxy.example:8080"


def test_budget_uses_current_runtime_settings_and_keeps_task_request(tmp_path):
    settings = RuntimeSettings(data_dir=tmp_path, collection={"max_bytes_per_source": 10})
    collector = Collector(tmp_path, settings)
    try:
        request = TaskRequest(
            source_ids=["mof"], date_from=date(2026, 9, 1), date_to=date(2026, 9, 2), max_pages=2
        )
        task = collector.repo.create_task(request)
        row = collector.repo.source_run(task, "mof")
        collector._budget_baseline = {"mof": row}
        updated = dict(row)
        updated["bytes_count"] = 10
        import time

        with pytest.raises(BudgetReached):
            collector._budget(time.monotonic(), request, updated)
        stored = json.loads(collector.repo.task(task)["request_json"])
        assert stored["max_pages"] == 2
    finally:
        collector.close()
