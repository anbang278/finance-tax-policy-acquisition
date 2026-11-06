"""Independent final-review probes; never contact an official site."""

import importlib.util
import json
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_source_integration import source_world

from ftr.config import RuntimeSettings
from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.sources import _digest, source_snapshot
from ftr.web.app import create_app

__all__ = ["source_world"]


def test_business_receipt_cannot_replace_selector_even_with_recomputed_digest(
    tmp_path, source_world
):
    snapshot = source_snapshot(["public_a"])
    snapshot["public_a"]["config"]["profile"]["detail"]["body"] = "//script"
    snapshot["public_a"]["digest"] = _digest(snapshot["public_a"]["config"])
    request = TaskRequest(
        source_ids=["public_a"],
        source_snapshot=snapshot,
        date_from=date(2026, 11, 1),
        date_to=date(2026, 11, 6),
    )
    with closing(Repository(tmp_path)) as repo:
        with pytest.raises(ValueError, match="快照"):
            repo.create_task(request)
        assert repo.db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        assert (
            client.post("/api/sources/preview", json=request.model_dump(mode="json")).status_code
            == 422
        )
    assert source_world[2] == []


def test_retry_sample_must_recheck_previously_saved_detail(tmp_path, source_world, monkeypatch):
    registry, pages, calls, _clients = source_world
    script = Path(__file__).parents[1] / "scripts" / "source_expansion_samples.py"
    spec = importlib.util.spec_from_file_location("source_samples_review", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_sources", lambda: registry)
    monkeypatch.setattr(
        module,
        "RuntimeSettings",
        lambda **values: RuntimeSettings(
            **{
                **values,
                "network": {**values["network"], "interval_seconds": 0},
            }
        ),
    )
    output = tmp_path / "samples"
    initial = module.sample("public_a", output, None)
    assert initial["status"] == "PASS"
    original_request = json.loads(json.dumps(initial["request"]))
    detail = initial["selected_urls"][0]
    original_html = pages[detail]
    calls.clear()
    pages[detail] = "<html><p>页面不存在或已删除</p></html>".encode()
    repeated = module.sample("public_a", output, None, retry=True)
    assert detail in calls, f"retry status={repeated['status']}; HTTP={calls}; 未重查已保存正文"
    assert repeated["status"] != "PASS"
    assert initial["request"] == original_request
    pages[detail] = original_html.replace(
        b"</main>", '<button onclick="downloadAttachment()">附件下载</button></main>'.encode()
    )
    missing_attachment = module.sample("public_a", output, None, retry=True)
    assert missing_attachment["details"], "正文保存不应掩盖没有URL的附件"
    assert missing_attachment["status"] == "PARTIAL"
    assert missing_attachment["attachment_gaps"]
    (output / "results" / "public_a.json").unlink()
    calls.clear()
    interrupted_retry = module.sample("public_a", output, None, retry=True)
    assert detail in calls
    assert interrupted_retry["data_dir"] != initial["data_dir"]


def test_sample_listing_originals_share_the_source_sample_byte_budget(
    tmp_path, source_world, monkeypatch
):
    from ftr.adapters.public import PublicAdapter

    registry, _pages, _calls, _clients = source_world
    script = Path(__file__).parents[1] / "scripts" / "source_expansion_samples.py"
    spec = importlib.util.spec_from_file_location("source_samples_budget_review", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_sources", lambda: registry)
    monkeypatch.setattr(
        module,
        "RuntimeSettings",
        lambda **values: RuntimeSettings(
            **{
                **values,
                "network": {**values["network"], "interval_seconds": 0},
            }
        ),
    )
    original = PublicAdapter._read

    def navigation(adapter, url, **kwargs):
        result = original(adapter, url, **kwargs)
        if kwargs.get("listing"):
            # Two permitted navigation responses are individually below 50MB.
            adapter.last_original_responses = [
                (b"a" * 30_000_000, url, "text/html", 200),
                (b"b" * 30_000_000, url, "text/html", 200),
            ]
            adapter.last_capture["capture_kind"] = "browser_dom"
        return result

    monkeypatch.setattr(PublicAdapter, "_read", navigation)
    output = tmp_path / "samples"
    result = module.sample("public_a", output, None)
    total = sum(
        p.stat().st_size for p in (Path(result["data_dir"]) / "evidence").rglob("*") if p.is_file()
    )
    assert total <= 50_000_000, (
        f"sample {result['status']} saved={total}; 样本列表阶段绕过50MB整批预算"
    )
