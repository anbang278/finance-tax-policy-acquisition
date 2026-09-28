from __future__ import annotations

import fcntl
import io
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from ftr.adapters.chinatax import ChinataxAdapter
from ftr.adapters.common import canonical_url, parse_detail
from ftr.adapters.mof import MofAdapter
from ftr.config import SourceConfig, load_sources
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
    check_url,
    local_https_proxy,
)
from ftr.repository import Repository


class BudgetReached(RuntimeError):
    pass


@contextmanager
def data_lock(data_dir: Path) -> Iterator[None]:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / ".runtime.lock").open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("数据目录正由另一宿主使用") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Collector:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.repo = Repository(data_dir)
        self.evidence = EvidenceStore(data_dir)
        self.sources = load_sources().sources

    def close(self) -> None:
        self.repo.close()

    def collect(self, request: TaskRequest) -> OperationResponse:
        task_id = self.repo.create_task(request)
        return self.resume(task_id)

    def resume(self, task_id: str) -> OperationResponse:
        task = self.repo.task(task_id)
        request = TaskRequest.model_validate_json(task["request_json"])
        if task["cancel_requested"]:
            return OperationResponse(operation="collect", task_id=task_id, status="CANCELLED")
        self.repo.set_task_state(task_id, "RUNNING")
        started = time.monotonic()
        self._budget_baseline: dict[str, sqlite3.Row] = {
            source_id: self.repo.source_run(task_id, source_id) for source_id in request.source_ids
        }
        warnings: list[str] = []
        with sync_playwright() as playwright:
            browser = None
            try:
                for source_id in request.source_ids:
                    config = self.sources[source_id]
                    client: BoundedClient | BrowserSessionClient
                    client = (
                        BrowserSessionClient(config.allowed_hosts)
                        if source_id == "chinatax"
                        else BoundedClient(config.allowed_hosts)
                    )
                    try:
                        adapter: MofAdapter | ChinataxAdapter
                        if source_id == "mof":
                            assert isinstance(client, BoundedClient)
                            adapter = MofAdapter(config, client)
                            page = None
                        else:
                            if browser is None:
                                try:
                                    proxy = local_https_proxy()
                                    browser = playwright.chromium.launch(
                                        headless=False,
                                        proxy={"server": proxy} if proxy else None,
                                    )
                                except PlaywrightError as exc:
                                    raise RuntimeError("无法启动有界面浏览器访问税务站") from exc
                            page = browser.new_page()
                            assert isinstance(client, BrowserSessionClient)
                            adapter = ChinataxAdapter(config, page, client)
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
                            warnings.append(f"{source_id}: {exc}")
                            continue
                        category = (
                            "ACCESS_RESTRICTED"
                            if isinstance(exc, AccessBlocked)
                            else "BUDGET_REACHED"
                            if isinstance(exc, BudgetReached)
                            else "UNKNOWN"
                        )
                        failure_id = self.repo.add_failure(
                            task_id, source_id, category, {"error": str(exc)}
                        )
                        warnings.append(f"{source_id}: {exc}（failure_id={failure_id}）")
                    finally:
                        client.close()
            finally:
                if browser is not None:
                    browser.close()
        runs = [self.repo.source_run(task_id, source_id) for source_id in request.source_ids]
        if self.repo.task(task_id)["cancel_requested"]:
            self.repo.set_task_state(task_id, "CANCELLED")
            return OperationResponse(
                operation="collect", task_id=task_id, status="CANCELLED", warnings=warnings
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
        return OperationResponse(
            operation="collect",
            task_id=task_id,
            status=state,
            data={
                "pending_decisions": pending,
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

    def _budget(self, started: float, request: TaskRequest, row, check_pages: bool = True) -> None:
        base = self._budget_baseline[str(row["source_id"])]
        if (
            time.monotonic() - started >= 1800
            or (check_pages and row["pages_count"] - base["pages_count"] >= request.max_pages)
            or row["documents_count"] - base["documents_count"] >= request.max_documents
            or row["bytes_count"] - base["bytes_count"] >= 500_000_000
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
        if isinstance(adapter, ChinataxAdapter):
            adapter.ensure_session()
        self.repo.retry_failed_refs(task_id, source_id)
        run = self.repo.source_run(task_id, source_id)
        while not run["discovery_done"]:
            self._process_refs(task_id, request, source_id, config, client, page, started, warnings)
            run = self.repo.source_run(task_id, source_id)
            self._budget(started, request, run)
            stop = self.repo.task(task_id)
            if stop["cancel_requested"]:
                raise BudgetReached("收到取消请求")
            if stop["pause_requested"]:
                raise BudgetReached("收到暂停请求")
            page_number = int(run["next_page"])
            refs, next_page, raw, media_type = adapter.discover(page_number)
            page_hash = digest(raw)
            if page_hash in seen_pages:
                raise ValueError("列表页内容重复，停止分页")
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
            stop = self.repo.task(task_id)
            if stop["cancel_requested"]:
                raise BudgetReached("收到取消请求")
            if stop["pause_requested"]:
                raise BudgetReached("收到暂停请求")
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
            try:
                check_url(ref.url, config.allowed_hosts)
                raw, final_url, media_type = client.get(ref.url)
                check_url(final_url, config.allowed_hosts)
                evidence = self.evidence.save(raw, ref.url, final_url, media_type)
                self.repo.save_evidence(evidence)
                record = parse_detail(ref, raw, evidence.evidence_id)
                remaining_bytes = 500_000_000 - (
                    run["bytes_count"] - self._budget_baseline[source_id]["bytes_count"] + len(raw)
                )
                attachment_bytes = self._attachments(
                    record, config, client, warnings, remaining_bytes, started
                )
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
            except (AccessBlocked, ValueError, RuntimeError) as exc:
                self.repo.set_ref_state(task_id, source_id, ref.url, "FAILED", str(exc))
                failure_id = self.repo.add_failure(
                    task_id,
                    source_id,
                    "ACCESS_RESTRICTED" if isinstance(exc, AccessBlocked) else "UNKNOWN",
                    {"url": ref.url, "error": str(exc)},
                )
                warnings.append(f"{ref.url}: {exc}（failure_id={failure_id}）")

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
                if remaining_bytes <= 0 or time.monotonic() - started >= 1800:
                    attachment.download_state = "blocked"
                    attachment.extraction_state = "pending"
                    warnings.append(f"附件 {attachment.url}: 达到任务预算")
                    continue
                check_url(attachment.url, config.allowed_hosts)
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
                    reader = PdfReader(io.BytesIO(raw))
                    text = "\n".join(page.extract_text() or "" for page in reader.pages)
                    attachment.text = text or None
                    attachment.extraction_state = "text" if text else "scanned"
                else:
                    attachment.extraction_state = "unsupported"
            except (AccessBlocked, ValueError, RuntimeError, OSError) as exc:
                attachment.download_state = (
                    "blocked" if isinstance(exc, AccessBlocked) else "failed"
                )
                attachment.extraction_state = "failed"
                warnings.append(f"附件 {attachment.url}: {exc}")
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
