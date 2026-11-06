"""Bounded real-source samples. Run only with explicit collection authorization.

Two list pages and three selected details per registered channel. Each source has
its own data directory; no review state, scheduler or live workbench is changed.
"""

from __future__ import annotations

import argparse
import json
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from ftr.browser import launch_browser
from ftr.config import RuntimeSettings, load_sources
from ftr.diagnostics import safe_message
from ftr.models import OfficialTarget, TaskRequest, digest, new_id
from ftr.network import BoundedClient, BrowserSessionClient, browser_proxy
from ftr.runtime import BudgetReached, Collector, data_lock, drain_private_context


def sample(
    source_id: str,
    output: Path,
    proxy: str | None,
    *,
    retry: bool = False,
    max_bytes: int = 50_000_000,
    max_file_bytes: int = 50_000_000,
    max_duration: float = 100,
) -> dict:
    if min(max_bytes, max_file_bytes, max_duration) <= 0:
        raise ValueError("样本预算必须为正数")
    directory = output / "data" / source_id
    result_path = output / "results" / (source_id + ".json")
    if result_path.exists():
        if not retry:
            return json.loads(result_path.read_text())
        history = result_path.parent / "history"
        history.mkdir(exist_ok=True)
        previous = history / (
            source_id + "-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".json"
        )
        previous.write_bytes(result_path.read_bytes())
    if retry:
        # This also isolates an interrupted attempt that never wrote a result.
        # Preserve its database/checkpoint; test idempotency within the new attempt.
        directory = directory / ("attempt-" + new_id())
    config = load_sources().sources[source_id]
    settings = RuntimeSettings(
        data_dir=directory,
        update_check={"enabled": False},
        network={
            "proxy_mode": "explicit" if proxy else "direct",
            "proxy_url": proxy,
            "timeout_seconds": 15,
            "interval_seconds": 1,
            "retry_attempts": 1,
            "max_file_bytes": max_file_bytes,
        },
        browser={"headless": True, "timeout_seconds": 20},
        collection={
            "max_duration_seconds": max_duration,
            "max_pages": 2,
            "max_documents": 3,
            "max_bytes_per_source": max_bytes,
            "max_bytes_per_batch": max_bytes,
        },
    )
    result = {
        "source_id": source_id,
        "entry": config.entry,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "BLOCKED",
        "pages": [],
        "selected_urls": [],
        "details": [],
        "data_dir": str(directory.resolve()),
    }
    with data_lock(directory):
        collector = Collector(directory, settings, proxy=proxy)
        sample_started = time.monotonic()
        sample_byte_limit = settings.collection.max_bytes_per_batch
        listing_bytes = 0
        listing_hashes = set()

        def save_listing(raw, url, final, media):
            nonlocal listing_bytes
            size = 0 if digest(raw) in listing_hashes else len(raw)
            if size > sample_byte_limit - listing_bytes:
                raise BudgetReached("样本列表及浏览器原件超过整批剩余预算")
            evidence = collector.evidence.save(raw, url, final, media)
            collector.repo.save_evidence(evidence)
            listing_bytes += size
            listing_hashes.add(evidence.evidence_id)
            return evidence

        def checkpoint():
            if time.monotonic() - sample_started >= settings.collection.max_duration_seconds:
                raise BudgetReached("样本达到整批时间预算")

        adapter = None
        try:
            with ExitStack() as stack:
                browser_required = config.adapter in ("chinatax", "public_browser")
                client = (BrowserSessionClient if browser_required else BoundedClient)(
                    config.allowed_hosts, settings=settings.network, proxy=proxy
                )
                stack.callback(client.close)
                client.host_requests = collector._host_requests
                client.checkpoint = checkpoint

                def before_request(_url):
                    checkpoint()
                    if listing_bytes >= sample_byte_limit:
                        raise BudgetReached("样本达到整批字节预算")
                    client.max_bytes = min(
                        settings.network.max_file_bytes, sample_byte_limit - listing_bytes
                    )

                client.before_request = before_request
                page = None
                if browser_required:
                    playwright = stack.enter_context(sync_playwright())
                    browser, _ = launch_browser(
                        playwright, settings.browser, proxy=browser_proxy(proxy)
                    )
                    stack.callback(browser.close)
                    context = browser.new_context(service_workers="block")
                    stack.callback(context.close)
                    page = context.new_page()
                    client.public_browser_service_workers_blocked = True
                adapter = collector.make_adapter(source_id, config, client, page)
                if hasattr(adapter, "close"):
                    stack.callback(adapter.close)
                if page is not None:
                    stack.callback(page.close)
                    stack.callback(drain_private_context, context)
                if hasattr(adapter, "ensure_session"):
                    adapter.ensure_session()
                refs = []
                page_number = 1
                for _ in range(2):
                    found, next_page, raw, media = adapter.discover(page_number)
                    final = adapter.last_response[1]
                    evidence = save_listing(
                        raw, getattr(adapter, "last_request_url", config.entry), final, media
                    )
                    originals = []
                    for content, url, original_media, status in getattr(
                        adapter, "last_original_responses", []
                    ):
                        original = save_listing(content, url, url, original_media)
                        originals.append({"evidence_id": original.evidence_id, "status": status})
                    for ref in found:
                        ref.discovered_from = evidence.evidence_id
                        if ref.url not in {item.url for item in refs}:
                            refs.append(ref)
                    result["pages"].append(
                        {
                            "page": page_number,
                            "evidence_id": evidence.evidence_id,
                            "final_url": final,
                            "next_page": next_page,
                            "found": len(found),
                            "capture": getattr(adapter, "last_capture", None),
                            "original_responses": originals,
                        }
                    )
                    if next_page is None:
                        break
                    page_number = next_page
            selected = refs[:3]
            if not selected:
                result.update(status="PARTIAL", reason="列表正常空结果，尚无代表正文样本")
            else:
                dates = [ref.listing_date for ref in selected if ref.listing_date]
                # Missing listing dates remain explicit. This fallback is a capture date,
                # never a fabricated publication/issue date.
                result["sample_date_basis"] = (
                    "observed_listing_dates" if dates else "capture_date_dates_unknown"
                )
                start, end = (
                    (min(dates), max(dates))
                    if dates
                    else (datetime.now(UTC).date(), datetime.now(UTC).date())
                )
                targets = [
                    OfficialTarget(
                        source_id=source_id,
                        url=ref.url,
                        title=ref.listing_title,
                        topic_id="source-expansion-sample",
                        reason="从已存证公开列表选取的有界链路样本",
                    )
                    for ref in selected
                ]
                request = TaskRequest(
                    source_ids=[source_id],
                    date_from=start,
                    date_to=end,
                    mode="targeted",
                    official_urls=targets,
                    max_pages=2,
                    max_documents=3,
                    idempotency_key="source-expansion-v1-" + source_id,
                )
                task_id = collector.repo.create_task(request)
                result.update(
                    task_id=task_id,
                    selected_urls=[r.url for r in selected],
                    request=json.loads(collector.repo.task(task_id)["request_json"]),
                )
                # The sample's exact URLs originate in these preserved list pages.
                collector.repo.audit(
                    task_id,
                    "sample_listing_scope",
                    {
                        "pages": result["pages"],
                        "selected_refs": [ref.model_dump(mode="json") for ref in selected],
                    },
                )
                checkpoint()
                remaining = sample_byte_limit - listing_bytes
                if remaining <= 0:
                    raise BudgetReached("列表已耗尽本次样本预算")
                collector.settings = settings.model_copy(
                    update={
                        "collection": settings.collection.model_copy(
                            update={
                                "max_bytes_per_batch": remaining,
                                "max_bytes_per_source": remaining,
                                "max_duration_seconds": max(
                                    0.001,
                                    settings.collection.max_duration_seconds
                                    - (time.monotonic() - sample_started),
                                ),
                            }
                        )
                    }
                )
                first = collector.resume(task_id)
                result["first_run"] = first.model_dump(mode="json")
                before = collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                repeated_id = collector.repo.create_task(request)
                after = collector.repo.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                result["repeat_run"] = {
                    "operation": "create_task",
                    "task_id": repeated_id,
                    "status": collector.repo.task(task_id)["state"],
                    "network_requests": 0,
                }
                result["repeat_kind"] = "same_key_idempotent_submission_without_refetch"
                result["idempotent_same_task"] = repeated_id == task_id
                result["repeat_new_versions"] = after - before
                records = collector.repo.db.execute(
                    "SELECT record_json FROM documents ORDER BY rowid"
                ).fetchall()
                result["details"] = [json.loads(row[0]) for row in records]
                saved = collector.repo.db.execute(
                    "SELECT COUNT(*) FROM discovered WHERE task_id=? AND state='SAVED'", (task_id,)
                ).fetchone()[0]
                attachments = [a for r in result["details"] for a in r.get("attachments", [])]
                result["parse_gaps"] = [
                    {"url": r["source_url"], "limitations": r.get("limitations", [])}
                    for r in result["details"]
                    if r.get("limitations")
                ]
                attachment_gaps = [
                    gap
                    for r in result["details"]
                    for gap in r.get("limitations", [])
                    if gap.startswith(("附件", "可见附件", "正文明确列示附件"))
                ]
                result["attachment_gaps"] = attachment_gaps
                complete = (
                    saved == len(selected)
                    and all(a["download_state"] == "saved" for a in attachments)
                    and not attachment_gaps
                )
                result["status"] = (
                    "PASS" if complete and dates else "PARTIAL" if saved else "BLOCKED"
                )
                result["reason"] = (
                    "有界列表、选定正文与所见附件已存证；资料仍待使用者复核"
                    if complete
                    else "部分正文或关联附件未完成，见运行失败与恢复记录"
                )
        except Exception as exc:  # noqa: BLE001 -- each independently bounded source must leave evidence.
            snapshot = getattr(adapter, "last_response", None)
            if snapshot:
                raw, url, media, _page_number = snapshot
                try:
                    evidence = save_listing(raw, url, url, media)
                    result["failure_evidence_id"] = evidence.evidence_id
                except BudgetReached:
                    result["failure_evidence_gap"] = "原始响应超过剩余样本存证预算"
            result["reason"] = safe_message(exc)
            result["error_type"] = type(exc).__name__
            result["status"] = "PARTIAL" if result["pages"] else "BLOCKED"
        finally:
            result["sample_budget_bytes"] = sample_byte_limit
            result["sample_file_budget_bytes"] = settings.network.max_file_bytes
            result["sample_duration_budget_seconds"] = settings.collection.max_duration_seconds
            result["sample_saved_bytes"] = sum(
                path.stat().st_size
                for path in (directory / "evidence").rglob("*")
                if path.is_file()
            )
            collector.close()
    result["finished_at"] = datetime.now(UTC).isoformat()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--retry", action="store_true", help="保存前次结果后复测相同有界样本")
    parser.add_argument("--sources", nargs="+", choices=list(load_sources().sources))
    parser.add_argument("--max-bytes", type=int, default=50_000_000, help="本轮每源总字节预算")
    parser.add_argument("--max-file-bytes", type=int, default=50_000_000, help="单个原件字节预算")
    parser.add_argument("--max-duration", type=float, default=100, help="本轮每源时间预算，秒")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for source_id in args.sources or load_sources().sources:
        result = sample(
            source_id,
            args.output,
            args.proxy,
            retry=args.retry,
            max_bytes=args.max_bytes,
            max_file_bytes=args.max_file_bytes,
            max_duration=args.max_duration,
        )
        print(
            json.dumps(
                {key: result.get(key) for key in ("source_id", "status", "reason", "task_id")},
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
