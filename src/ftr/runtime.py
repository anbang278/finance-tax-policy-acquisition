from __future__ import annotations

import io
import json
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import portalocker
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from ftr.adapters.chinatax import ChinataxAdapter
from ftr.adapters.common import canonical_url, parse_detail
from ftr.adapters.mof import MofAdapter
from ftr.browser import launch_browser
from ftr.config import RuntimeSettings, SourceConfig, load_sources, redact_proxy
from ftr.evidence import EvidenceStore
from ftr.models import (
    DecisionRequest,
    DiscoveredRef,
    DocumentRecord,
    OperationResponse,
    TaskRequest,
    digest,
)
from ftr.network import (
    AccessBlocked,
    BoundedClient,
    BrowserSessionClient,
    TransientFailure,
    browser_proxy,
    check_url,
    resolve_proxy,
)
from ftr.repository import Repository
from ftr.rules import Rules, StructureDrift, active_rules


class BudgetReached(RuntimeError):
    def __init__(self, message, code="BUDGET_REACHED"):
        super().__init__(message)
        self.code = code


class PdfWarnings(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


_PROXY_UNSET = object()


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".runtime.lock").open("a+b") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.LockException as exc:
            raise RuntimeError("数据目录正由另一宿主使用") from exc
        try:
            yield
        finally:
            portalocker.unlock(handle)


class Collector:
    def __init__(
        self,
        data_dir: Path,
        settings: RuntimeSettings | None = None,
        *,
        proxy: object = _PROXY_UNSET,
        rule_overrides: dict[str, Rules] | None = None,
        detail_limit: int | None = None,
    ):
        self.settings = settings or RuntimeSettings(data_dir=data_dir)
        selected = resolve_proxy(self.settings.network) if proxy is _PROXY_UNSET else proxy
        assert selected is None or isinstance(selected, str)
        self.proxy = selected
        self.data_dir = data_dir
        self.repo = Repository(data_dir)
        self.evidence = EvidenceStore(data_dir)
        self.sources = load_sources().sources
        self.rules = {
            key: (rule_overrides or {}).get(key) or active_rules(data_dir, key)
            for key in self.sources
        }
        self.failure_ids: list[str] = []
        self.processed_documents = 0
        self.incomplete_documents = 0
        self.detail_limit = detail_limit
        self.detail_attempts = 0
        self._transient_streak: dict[str, int] = {}
        self._interrupted = False
        self._progress_snapshot: dict = {}
        self._stop_reasons: list[str] = []

    def close(self) -> None:
        self.repo.close()

    def collect(self, request: TaskRequest) -> OperationResponse:
        task_id = self.repo.create_task(request)
        return self.resume(task_id)

    def resume(self, task_id: str, *, bounded_source: str | None = None) -> OperationResponse:
        finished = threading.Event()
        self._interrupted = False
        self._stop_reasons = []
        self._transient_streak = {}
        self._progress_started = time.monotonic()
        self._progress_snapshot = {"task_id": task_id, "stage": "starting"}
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._interrupt)
        reporter = threading.Thread(target=self._report_progress, args=(finished,), daemon=True)
        reporter.start()
        try:
            return self._resume(task_id, bounded_source=bounded_source)
        except KeyboardInterrupt:
            self.repo.set_task_state(task_id, "PARTIAL")
            self.repo.audit(task_id, "batch_stopped", {"reasons": ["INTERRUPTED"]})
            return OperationResponse(
                operation="collect",
                task_id=task_id,
                status="PARTIAL",
                data={"stop_reasons": ["INTERRUPTED"], "resume_argv": self._resume_argv(task_id)},
            )
        finally:
            finished.set()
            reporter.join(timeout=1)
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def _interrupt(self, signum, frame):
        self._interrupted = True

    def _report_progress(self, finished):
        while not finished.wait(self.settings.collection.progress_interval_seconds):
            snapshot = {
                **self._progress_snapshot,
                "elapsed_seconds": round(time.monotonic() - self._progress_started, 1),
            }
            print(
                "ftr progress " + json.dumps(snapshot, ensure_ascii=False),
                file=sys.stderr,
                flush=True,
            )

    def _resume_argv(self, task_id):
        args = [sys.executable, "-m", "ftr.cli"]
        if self.settings._config_path:
            args += ["--config", str(self.settings._config_path)]
        return args + [
            "--data-dir",
            str(self.data_dir.resolve()),
            "task",
            "resume",
            "--task",
            task_id,
        ]

    def _checkpoint(self, task_id, source_id, started):
        row = self.repo.task(task_id)
        if self._interrupted:
            raise BudgetReached("收到中断信号，检查点已保留", "INTERRUPTED")
        if row["cancel_requested"]:
            raise BudgetReached("收到取消请求", "CANCEL_REQUESTED")
        if row["pause_requested"]:
            raise BudgetReached("收到暂停请求", "PAUSE_REQUESTED")
        if time.monotonic() - started >= self.settings.collection.max_duration_seconds:
            raise BudgetReached("达到本次采集时间预算，请续跑")
        counts = dict(
            self.repo.db.execute(
                "SELECT state,COUNT(*) FROM discovered WHERE task_id=? GROUP BY state", (task_id,)
            )
        )
        run = self.repo.source_run(task_id, source_id)
        self._progress_snapshot = {
            "task_id": task_id,
            "source_id": source_id,
            "discovered": sum(counts.values()),
            "saved": counts.get("SAVED", 0),
            "failed": counts.get("FAILED", 0),
            "pages": run["pages_count"],
            "last_checkpoint": {"next_page": run["next_page"], "at": datetime.now(UTC).isoformat()},
            "stage": getattr(self, "_request_stage", "checkpoint"),
        }

    def _network_request(self, task_id, source_id, started, url):
        self._checkpoint(task_id, source_id, started)
        if self.detail_limit is not None:
            if self._request_stage == "discover":
                if self._list_attempts >= 1:
                    raise BudgetReached("达到有界验证的列表请求上限")
                self._list_attempts += 1
            else:
                if self.detail_attempts >= self.detail_limit:
                    raise BudgetReached("达到有界验证的详情请求上限")
                self.detail_attempts += 1

    def _resume(self, task_id: str, *, bounded_source: str | None = None) -> OperationResponse:
        self._active_task = task_id
        task = self.repo.task(task_id)
        request = TaskRequest.model_validate_json(task["request_json"])
        if bounded_source:
            request = request.model_copy(
                update={"source_ids": [bounded_source], "max_pages": 1, "max_documents": 2}
            )
        if task["cancel_requested"]:
            return OperationResponse(
                operation="collect",
                task_id=task_id,
                status="CANCELLED",
                data={"stop_reasons": ["CANCEL_REQUESTED"]},
            )
        self.repo.set_task_state(task_id, "RUNNING")
        self.repo.audit(
            task_id,
            "runtime_config",
            {
                "settings": self.settings.public_dict(),
                "effective_proxy": redact_proxy(self.proxy),
            },
        )
        started = time.monotonic()
        self._budget_baseline: dict[str, sqlite3.Row] = {
            source_id: self.repo.source_run(task_id, source_id) for source_id in request.source_ids
        }
        warnings: list[str] = []
        with ExitStack() as stack:
            browser = None
            try:
                for source_id in request.source_ids:
                    config = self.sources[source_id]
                    client: BoundedClient | BrowserSessionClient
                    client = (
                        BrowserSessionClient(
                            config.allowed_hosts, settings=self.settings.network, proxy=self.proxy
                        )
                        if source_id == "chinatax"
                        else BoundedClient(
                            config.allowed_hosts, settings=self.settings.network, proxy=self.proxy
                        )
                    )
                    self._list_attempts = 0
                    self._request_stage = "discover"
                    client.checkpoint = lambda source_id=source_id: self._checkpoint(
                        task_id, source_id, started
                    )
                    client.before_request = lambda url, source_id=source_id: self._network_request(
                        task_id, source_id, started, url
                    )
                    adapter: MofAdapter | ChinataxAdapter | None = None
                    try:
                        self._checkpoint(task_id, source_id, started)
                        if source_id == "mof":
                            assert isinstance(client, BoundedClient)
                            adapter = MofAdapter(config, client)
                            page = None
                        else:
                            if browser is None:
                                try:
                                    if (
                                        sys.platform.startswith("linux")
                                        and not self.settings.browser.headless
                                        and not os.environ.get("DISPLAY")
                                        and not os.environ.get("WAYLAND_DISPLAY")
                                    ):
                                        raise RuntimeError(
                                            "缺少显示环境，请使用 xvfb-run 启动税务采集"
                                        )
                                    playwright = stack.enter_context(sync_playwright())
                                    browser, _selected = launch_browser(
                                        playwright,
                                        self.settings.browser,
                                        proxy=browser_proxy(self.proxy),
                                        args=["--no-proxy-server"] if self.proxy is None else [],
                                    )
                                except PlaywrightError as exc:
                                    raise RuntimeError(
                                        "无法启动浏览器访问税务站，请检查安装与显示环境"
                                    ) from exc
                            page = browser.new_page()
                            assert isinstance(client, BrowserSessionClient)
                            adapter = ChinataxAdapter(
                                config,
                                page,
                                client,
                                timeout_seconds=self.settings.browser.timeout_seconds,
                            )
                        adapter.rules = self.rules[source_id]
                        self.repo.audit(
                            task_id,
                            "rule_version",
                            {"source_id": source_id, "version": adapter.rules.version},
                        )
                        try:
                            self._run_source(
                                task_id,
                                request,
                                source_id,
                                config,
                                client,
                                adapter,
                                page,
                                started,
                                warnings,
                            )
                        finally:
                            if page is not None:
                                page.close()
                    except (AccessBlocked, BudgetReached, ValueError, RuntimeError) as exc:
                        if isinstance(exc, BudgetReached):
                            self._stop_reasons.append(exc.code)
                            warnings.append(f"{source_id}: {exc}")
                            continue
                        self._stop_reasons.append(
                            "TRANSIENT_NETWORK"
                            if isinstance(exc, TransientFailure)
                            else "SOURCE_FAILURE"
                        )
                        snapshot = getattr(adapter, "last_response", None)
                        evidence_id = None
                        if snapshot and isinstance(exc, StructureDrift):
                            raw, url, media, _page_number = snapshot
                            evidence = self.evidence.save(raw, url, url, media)
                            self.repo.save_evidence(evidence)
                            evidence_id = evidence.evidence_id
                        failure_id = self._failure(
                            task_id, source_id, exc, "discover", evidence_id=evidence_id
                        )
                        warnings.append(f"{source_id}: {exc}（failure_id={failure_id}）")
                    finally:
                        client.close()
            finally:
                if browser is not None:
                    browser.close()
        if self._interrupted:
            self._stop_reasons.append("INTERRUPTED")
            warnings.append("收到中断信号，检查点已保留")
        if self.repo.task(task_id)["pause_requested"]:
            self._stop_reasons.append("PAUSE_REQUESTED")
            warnings.append("收到暂停请求，检查点已保留")
        runs = [self.repo.source_run(task_id, source_id) for source_id in request.source_ids]
        if self.repo.task(task_id)["cancel_requested"]:
            self.repo.set_task_state(task_id, "CANCELLED")
            self.repo.audit(
                task_id, "batch_stopped", {"reasons": ["CANCEL_REQUESTED"], "state": "CANCELLED"}
            )
            return OperationResponse(
                operation="collect",
                task_id=task_id,
                status="CANCELLED",
                warnings=warnings,
                data={"stop_reasons": ["CANCEL_REQUESTED"]},
            )
        pending = len(self.repo.pending_decisions(task_id))
        failures = self.repo.db.execute(
            "SELECT COUNT(*) FROM discovered WHERE task_id=? AND state='FAILED'", (task_id,)
        ).fetchone()[0]
        complete = all(
            row["discovery_done"] and not self.repo.pending_refs(task_id, row["source_id"])
            for row in runs
        )
        if warnings or failures or not complete:
            state = "PARTIAL"
        elif pending:
            state = "WAITING_DECISION"
        else:
            count = self.repo.db.execute(
                "SELECT COUNT(*) FROM discovered WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            state = "COMPLETED_EMPTY" if count == 0 else "COMPLETED"
        self.repo.set_task_state(task_id, state)
        counts = dict(
            self.repo.db.execute(
                "SELECT state,COUNT(*) FROM discovered WHERE task_id=? GROUP BY state", (task_id,)
            )
        )
        if not self._stop_reasons and (failures or not complete):
            self._stop_reasons.append("INCOMPLETE_QUEUE")
        self.repo.audit(
            task_id,
            "batch_stopped",
            {
                "reasons": list(dict.fromkeys(self._stop_reasons)),
                "remaining_queue": counts,
                "state": state,
            },
        )
        return OperationResponse(
            operation="collect",
            task_id=task_id,
            status=state,
            data={
                "stop_reasons": list(dict.fromkeys(self._stop_reasons)),
                "remaining_queue": counts,
                "resume_argv": self._resume_argv(task_id) if state == "PARTIAL" else None,
                "pending_decisions": pending,
                "failure_ids": self.failure_ids,
                "processed_documents": self.processed_documents,
                "incomplete_documents": self.incomplete_documents,
                "sources": [
                    {
                        "source_id": row["source_id"],
                        "pages": row["pages_count"],
                        "documents": row["documents_count"],
                        "discovery_done": bool(row["discovery_done"]),
                    }
                    for row in runs
                ],
            },
            warnings=warnings,
        )

    def _failure(self, task_id, source_id, exc, stage, *, evidence_id=None, ref=None):
        run = self.repo.source_run(task_id, source_id)
        category = (
            "ACCESS_RESTRICTED"
            if isinstance(exc, AccessBlocked)
            else "STRUCTURE_DRIFT"
            if isinstance(exc, StructureDrift)
            else "TRANSIENT_NETWORK"
            if isinstance(exc, TransientFailure)
            else "UNKNOWN"
        )
        details = {
            "stage": stage,
            "page": run["next_page"],
            "evidence_id": evidence_id,
            "rule_version": self.rules[source_id].version,
            "response": {"sha256": evidence_id},
            "expected": "列表含标题/官方链接/日期出处且分页前进；正文与标题可靠定位",
            "error_type": type(exc).__name__,
            "ref": ref.model_dump(mode="json") if ref else None,
        }
        failure_id = self.repo.add_failure(task_id, source_id, category, details)
        self.failure_ids.append(failure_id)
        return failure_id

    def _budget(self, started: float, request: TaskRequest, row, check_pages: bool = True) -> None:
        base = self._budget_baseline[str(row["source_id"])]
        if (
            time.monotonic() - started >= self.settings.collection.max_duration_seconds
            or (check_pages and row["pages_count"] - base["pages_count"] >= request.max_pages)
            or row["documents_count"] - base["documents_count"] >= request.max_documents
            or row["bytes_count"] - base["bytes_count"]
            >= self.settings.collection.max_bytes_per_source
        ):
            raise BudgetReached("达到本次采集预算，请续跑")

    def _run_source(
        self,
        task_id: str,
        request: TaskRequest,
        source_id: str,
        config: SourceConfig,
        client: BoundedClient | BrowserSessionClient,
        adapter,
        page,
        started: float,
        warnings: list[str],
    ) -> None:
        seen_pages: set[str] = set()
        self._checkpoint(task_id, source_id, started)
        if isinstance(adapter, ChinataxAdapter):
            adapter.ensure_session()
        self.repo.retry_failed_refs(task_id, source_id)
        run = self.repo.source_run(task_id, source_id)
        while not run["discovery_done"]:
            self._process_refs(task_id, request, source_id, config, client, page, started, warnings)
            run = self.repo.source_run(task_id, source_id)
            self._budget(started, request, run)
            self._checkpoint(task_id, source_id, started)
            self._request_stage = "discover"
            self._checkpoint(task_id, source_id, started)
            page_number = int(run["next_page"])
            refs, next_page, raw, media_type = adapter.discover(page_number)
            page_hash = digest(raw)
            if next_page is not None and next_page != page_number + 1:
                raise StructureDrift("分页必须向下一页前进")
            if page_hash in seen_pages:
                raise StructureDrift("列表页内容重复，停止分页")
            seen_pages.add(page_hash)
            listing_url = adapter.list_url if isinstance(adapter, ChinataxAdapter) else config.entry
            assert listing_url is not None
            listing_evidence = self.evidence.save(raw, listing_url, listing_url, media_type)
            self.repo.save_evidence(listing_evidence)
            for ref in refs:
                ref.discovered_from = listing_evidence.evidence_id
            self.repo.save_page(task_id, source_id, refs, next_page)
            run = self.repo.source_run(task_id, source_id)
        self._process_refs(task_id, request, source_id, config, client, page, started, warnings)

    def _process_refs(
        self,
        task_id: str,
        request: TaskRequest,
        source_id: str,
        config: SourceConfig,
        client: BoundedClient | BrowserSessionClient,
        page,
        started: float,
        warnings: list[str],
    ) -> None:
        for row in self.repo.pending_refs(task_id, source_id):
            run = self.repo.source_run(task_id, source_id)
            self._budget(started, request, run, check_pages=False)
            self._checkpoint(task_id, source_id, started)
            ref = DiscoveredRef.model_validate_json(row["ref_json"])
            if (
                request.mode == "incremental"
                and ref.listing_date is not None
                and ref.listing_date < datetime.now(UTC).date() - timedelta(days=14)
                and self.repo.has_document(source_id, canonical_url(ref.url))
            ):
                self.repo.set_ref_state(task_id, source_id, ref.url, "UNCHANGED_SKIP")
                continue
            if (
                request.date_basis == "source_listing"
                and ref.listing_date
                and not request.date_from <= ref.listing_date <= request.date_to
            ):
                self.repo.set_ref_state(task_id, source_id, ref.url, "OUT_OF_SCOPE")
                continue
            evidence = None
            try:
                self._request_stage = "detail"
                if self.detail_limit is not None and self.detail_attempts >= self.detail_limit:
                    raise BudgetReached("达到有界验证的详情请求上限")
                check_url(ref.url, config.allowed_hosts, self.proxy)
                raw, final_url, media_type = client.get(ref.url)
                check_url(final_url, config.allowed_hosts, self.proxy)
                if len(raw) > self.settings.collection.max_bytes_per_source - (
                    run["bytes_count"] - self._budget_baseline[source_id]["bytes_count"]
                ):
                    raise BudgetReached("正文超过剩余下载预算，请续跑或调整运行预算")
                evidence = self.evidence.save(raw, ref.url, final_url, media_type)
                self.repo.save_evidence(evidence)
                record = parse_detail(ref, raw, evidence.evidence_id, self.rules[source_id])
                if self.rules[source_id].version != Rules(source_id=source_id).version:
                    record.parser_version += ":" + self.rules[source_id].version
                if not record.body_text or not record.title:
                    raise StructureDrift("正文或标题区域未可靠定位")
                remaining_bytes = self.settings.collection.max_bytes_per_source - (
                    run["bytes_count"] - self._budget_baseline[source_id]["bytes_count"] + len(raw)
                )
                attachment_bytes = self._attachments(
                    record, config, client, warnings, remaining_bytes, started
                )
                self._transient_streak[source_id] = 0
                if (
                    request.date_basis == "issued_date"
                    and record.issued_date
                    and not request.date_from <= record.issued_date <= request.date_to
                ):
                    self.repo.set_ref_state(task_id, source_id, ref.url, "OUT_OF_SCOPE")
                    continue
                if (
                    request.date_basis == "published_date"
                    and record.published_date
                    and not request.date_from <= record.published_date <= request.date_to
                ):
                    self.repo.set_ref_state(task_id, source_id, ref.url, "OUT_OF_SCOPE")
                    continue
                selected_date = (
                    record.listing_date
                    if request.date_basis == "source_listing"
                    else record.issued_date
                    if request.date_basis == "issued_date"
                    else record.published_date
                )
                if selected_date is None:
                    record.limitations.append("筛选日期缺失，范围待确认")
                if not record.title or not record.body_text:
                    record.limitations.append("标题或正文缺失")
                record.quality_state = "quarantined" if record.limitations else "collected"
                self.processed_documents += 1
                self.incomplete_documents += bool(record.limitations)
                record_id, created = self.repo.add_document(task_id, record)
                if created and not record.limitations:
                    input_digest = digest(record.model_dump_json().encode())
                    primary_text = "\n".join(
                        attachment.text or ""
                        for attachment in record.attachments
                        if attachment.content_role == "primary"
                        and attachment.extraction_state == "text"
                    )
                    self.repo.add_decision(
                        DecisionRequest(
                            task_id=task_id,
                            record_id=record_id,
                            input_digest=input_digest,
                            excerpt=(record.body_text + "\n" + primary_text)[:1500],
                            evidence_id=record.evidence_id,
                        )
                    )
                self.repo.set_ref_state(task_id, source_id, ref.url, "SAVED")
                self.repo.db.execute(
                    "UPDATE source_runs SET bytes_count=bytes_count+? WHERE task_id=? AND source_id=?",
                    (len(raw) + attachment_bytes, task_id, source_id),
                )
                self.repo.db.commit()
            except BudgetReached:
                raise
            except (AccessBlocked, ValueError, RuntimeError) as exc:
                self.repo.set_ref_state(task_id, source_id, ref.url, "FAILED", str(exc))
                failure_id = self._failure(
                    task_id,
                    source_id,
                    exc,
                    "detail",
                    evidence_id=evidence.evidence_id if evidence else None,
                    ref=ref,
                )
                warnings.append(f"条目失败，failure_id={failure_id}")
                if isinstance(exc, TransientFailure):
                    streak = self._transient_streak.get(source_id, 0) + 1
                    self._transient_streak[source_id] = streak
                    if streak < self.settings.collection.consecutive_failure_limit:
                        continue
                    raise BudgetReached(
                        "来源连续瞬态失败停止，请诊断后续跑", "TRANSIENT_FAILURE_LIMIT"
                    ) from exc
                raise BudgetReached("来源因条目失败停止，请先诊断", "SOURCE_FAILURE") from exc

    def _attachments(
        self,
        record: DocumentRecord,
        config: SourceConfig,
        client: BoundedClient | BrowserSessionClient,
        warnings: list[str],
        remaining_bytes: int,
        started: float,
    ) -> int:
        downloaded_bytes = 0
        for attachment in record.attachments:
            try:
                if (
                    remaining_bytes <= 0
                    or time.monotonic() - started >= self.settings.collection.max_duration_seconds
                ):
                    attachment.download_state = "blocked"
                    attachment.extraction_state = "pending"
                    warnings.append(f"附件 {attachment.url}: 达到任务预算")
                    continue
                check_url(attachment.url, config.allowed_hosts, self.proxy)
                raw, final_url, media_type = client.get(attachment.url)
                if len(raw) > remaining_bytes:
                    attachment.download_state = "blocked"
                    attachment.extraction_state = "pending"
                    warnings.append(f"附件 {attachment.url}: 达到任务下载预算")
                    continue
                evidence = self.evidence.save(raw, attachment.url, final_url, media_type)
                self.repo.save_evidence(evidence)
                remaining_bytes -= len(raw)
                downloaded_bytes += len(raw)
                attachment.evidence_id = evidence.evidence_id
                attachment.download_state = "saved"
                if urlsplit(attachment.url).path.lower().endswith(".pdf"):
                    handler = PdfWarnings()
                    logger = logging.getLogger("pypdf")
                    old_handlers, old_propagate = logger.handlers[:], logger.propagate
                    logger.handlers, logger.propagate = [handler], False
                    try:
                        reader = PdfReader(io.BytesIO(raw))
                        text = "\n".join(page.extract_text() or "" for page in reader.pages)
                    finally:
                        logger.handlers, logger.propagate = old_handlers, old_propagate
                        if handler.messages:
                            record.limitations.append("PDF 解析产生警告，内容完整性待复核")
                            warnings.append(
                                f"PDF 解析警告 {len(handler.messages)} 条；内容完整性待复核"
                            )
                            # Preserve full warnings as local evidence, not console noise.
                            warning_bytes = json.dumps(
                                handler.messages, ensure_ascii=False
                            ).encode()
                            warning_evidence = self.evidence.save(
                                warning_bytes, attachment.url, final_url, "application/json"
                            )
                            self.repo.save_evidence(warning_evidence)
                            self.repo.audit(
                                self._active_task,
                                "pdf_warnings",
                                {
                                    "evidence_id": warning_evidence.evidence_id,
                                    "attachment_evidence_id": attachment.evidence_id,
                                    "count": len(handler.messages),
                                },
                            )
                    attachment.text = text or None
                    attachment.extraction_state = "text" if text else "scanned"
                else:
                    attachment.extraction_state = "unsupported"
            except BudgetReached:
                raise
            except (AccessBlocked, ValueError, RuntimeError, OSError) as exc:
                attachment.download_state = (
                    "blocked" if isinstance(exc, AccessBlocked) else "failed"
                )
                attachment.extraction_state = "failed"
                warnings.append(f"附件 {attachment.url}: {exc}")
                if isinstance(exc, AccessBlocked):
                    raise
        if any(att.download_state != "saved" for att in record.attachments):
            record.limitations.append("部分附件未保存")
        if any(
            att.content_role == "primary" and att.extraction_state != "text"
            for att in record.attachments
        ):
            record.limitations.append("主附件原件已登记，但内容尚未解析")
        if not record.body_text and any(
            att.extraction_state != "text" for att in record.attachments
        ):
            record.limitations.append("主内容可能位于未解析附件")
        return downloaded_bytes
