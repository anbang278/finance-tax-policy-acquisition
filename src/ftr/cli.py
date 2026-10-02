from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from ftr.backup import create_backup, restore_backup, verify_backup
from ftr.config import load_settings, load_sources, redact_proxy
from ftr.environment import environment_report
from ftr.models import OperationResponse, SemanticDecision, TaskRequest
from ftr.network import resolve_proxy
from ftr.repair import candidate_report, prepare_candidate
from ftr.repository import Repository
from ftr.research import ResearchDraft, verify_and_render
from ftr.runtime import Collector, data_lock


def data_dir() -> Path:
    return load_settings().data_dir


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="ftr", description="财税公开资料采集（本地演示）")
    root.add_argument("--config", type=Path, help="运行配置 YAML；全局参数置于子命令之前")
    root.add_argument("--data-dir", type=Path, help="覆盖运行数据目录")
    commands = root.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config").add_subparsers(dest="action", required=True)
    config.add_parser("show")
    config.add_parser("validate")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--mode", choices=["local-demo", "governed"], default="local-demo")
    serve = commands.add_parser("serve", help="启动只读政策工作台")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    workbench = commands.add_parser("workbench").add_subparsers(dest="action", required=True)
    for action in ("start", "status", "stop"):
        command = workbench.add_parser(action)
        if action == "start":
            command.add_argument("--open", action="store_true")
    collect = commands.add_parser("collect")
    collect.add_argument("--request", type=Path)
    collect.add_argument("--sources", nargs="+", choices=["mof", "chinatax"])
    collect.add_argument("--date-from", type=date.fromisoformat)
    collect.add_argument("--date-to", type=date.fromisoformat)
    collect.add_argument(
        "--date-basis",
        choices=["source_listing", "issued_date", "published_date"],
        default=None,
    )
    collect.add_argument("--mode", choices=["initial", "incremental", "backfill", "rescan"])
    collect.add_argument("--max-pages", type=int)
    collect.add_argument("--max-documents", type=int)
    collect.add_argument("--idempotency-key")
    task = commands.add_parser("task").add_subparsers(dest="action", required=True)
    for action in ("status", "resume", "pause", "cancel"):
        item = task.add_parser(action)
        item.add_argument("--task", required=True)
    decision = commands.add_parser("decision").add_subparsers(dest="action", required=True)
    decision.add_parser("list").add_argument("--task", required=True)
    decision.add_parser("submit").add_argument("--file", required=True, type=Path)
    search = commands.add_parser("search")
    search.add_argument("--query", required=True)
    search.add_argument("--include-limited", action="store_true")
    for name in ("document", "evidence"):
        subcommands = commands.add_parser(name).add_subparsers(dest="action", required=True)
        subcommands.add_parser("show").add_argument("--id", required=True)
    repair = commands.add_parser("repair").add_subparsers(dest="action", required=True)
    item = repair.add_parser("prepare")
    item.add_argument("--failure", required=True)
    item.add_argument("--patch", required=True, type=Path)
    repair.add_parser("test").add_argument("--candidate", required=True)
    repair.add_parser("context").add_argument("--failure", required=True)
    rule = repair.add_parser("propose-rule")
    rule.add_argument("--failure", required=True)
    rule.add_argument("--file", type=Path, required=True)
    rule = repair.add_parser("test-rule")
    rule.add_argument("--candidate", required=True)
    rule.add_argument("--live", action="store_true", help="原任务范围内真实来源验证；需采集授权")
    repair.add_parser("activate-rule").add_argument("--candidate", required=True)
    repair.add_parser("rollback-rule").add_argument(
        "--source", choices=["mof", "chinatax"], required=True
    )
    candidate = commands.add_parser("candidate").add_subparsers(dest="action", required=True)
    candidate.add_parser("report").add_argument("--candidate", required=True)
    research = commands.add_parser("research").add_subparsers(dest="action", required=True)
    research.add_parser("prepare").add_argument("--query", required=True)
    research.add_parser("submit").add_argument("--file", required=True, type=Path)
    export = commands.add_parser("export")
    export.add_argument("--query", required=True)
    export.add_argument("--output", required=True, type=Path)
    backup = commands.add_parser("backup").add_subparsers(dest="action", required=True)
    backup.add_parser("create").add_argument("--output", required=True, type=Path)
    backup.add_parser("verify").add_argument("--path", required=True, type=Path)
    restore = backup.add_parser("restore")
    restore.add_argument("--path", required=True, type=Path)
    restore.add_argument("--target", required=True, type=Path)
    release = commands.add_parser("release").add_subparsers(dest="action", required=True)
    release.add_parser("status").add_argument(
        "--adapter", choices=["mof", "chinatax"], required=True
    )
    commands.add_parser("schema")
    return root


def run(args: argparse.Namespace) -> OperationResponse:
    overrides: dict = {}
    if getattr(args, "data_dir", None) is not None:
        overrides["data_dir"] = args.data_dir
    if args.command == "serve":
        if args.port is not None and not 1 <= args.port <= 65535:
            raise ValueError("端口必须在 1 到 65535 之间")
        overrides["web"] = {
            key: getattr(args, key) for key in ("host", "port") if getattr(args, key) is not None
        }
    if args.command == "collect":
        overrides["collection"] = {
            key: getattr(args, key)
            for key in ("max_pages", "max_documents")
            if getattr(args, key) is not None
        }
    settings = load_settings(getattr(args, "config", None), overrides=overrides)
    root = settings.data_dir
    effective_proxy = resolve_proxy(settings.network)
    if args.command == "config":
        return OperationResponse(
            operation=f"config {args.action}",
            status="COMPLETED",
            data={
                **settings.describe(),
                "valid": True,
                "effective_proxy": redact_proxy(effective_proxy),
            },
        )
    if args.command == "backup" and args.action in ("verify", "restore"):
        backup_result = (
            verify_backup(args.path)
            if args.action == "verify"
            else restore_backup(args.path, args.target)
        )
        return OperationResponse(
            operation=f"backup {args.action}", status="COMPLETED", data=backup_result
        )
    if args.command == "workbench":
        from ftr.workbench import manage

        result = manage(
            root, args.action, open_browser=getattr(args, "open", False), web_settings=settings.web
        )
        return OperationResponse(
            operation=f"workbench {args.action}", status=result["state"], data=result
        )
    if args.command == "serve":
        try:
            from ftr.web.app import serve
        except ImportError as exc:
            raise RuntimeError("请先安装 Web 依赖：uv sync --extra web") from exc
        serve(root, settings.web.port, settings.web)
        return OperationResponse(operation="serve", status="STOPPED")
    if (
        args.command == "collect"
        and not args.request
        and (args.date_from is None or args.date_to is None)
    ):
        raise ValueError("采集时间范围未明确：请同时提供 --date-from 和 --date-to")
    if args.command == "task" and args.action in ("pause", "cancel"):
        database = root / "database.sqlite3"
        if not database.is_file():
            raise KeyError("任务数据库不存在")
        with sqlite3.connect(database, timeout=5) as connection:
            field = "cancel_requested" if args.action == "cancel" else "pause_requested"
            cursor = connection.execute(f"UPDATE tasks SET {field}=1 WHERE id=?", (args.task,))
            if cursor.rowcount != 1:
                raise KeyError("任务不存在")
        return OperationResponse(
            operation=f"task {args.action}", task_id=args.task, status="STOP_REQUESTED"
        )
    if args.command == "doctor":
        sources = load_sources()
        report = environment_report(settings)
        if args.mode == "governed":
            return OperationResponse(
                operation="doctor",
                status="BLOCKED",
                data={"mode": args.mode, "checks": report},
                errors=[
                    {
                        "code": "GOVERNED_NOT_IMPLEMENTED",
                        "message": "正式权限隔离尚未实施",
                        "retryable": False,
                    }
                ],
            )
        return OperationResponse(
            operation="doctor",
            status="LOCAL_DEMO_READY"
            if report["core_dependencies_ready"] and report["python"]["supported"]
            else "BLOCKED",
            data={
                "mode": args.mode,
                "source_ids": list(sources.sources),
                "data_dir": str(root),
                "checks": report,
                "effective_proxy": redact_proxy(effective_proxy),
            },
            warnings=[
                "此检查只核对本地配置与依赖，不证明浏览器启动、显示连接、真实来源可访问或正式权限隔离"
            ]
            + (
                ["税务浏览器文件缺失，请安装 Chromium 或配置 executable_path"]
                if not report["browser"]["file_exists"]
                else []
            )
            + (
                ["税务有界面采集缺少显示环境，请使用 xvfb-run"]
                if not report["display"]["available"]
                else []
            ),
        )
    if args.command == "schema":
        return OperationResponse(
            operation="schema",
            status="COMPLETED",
            data={
                "task_request": TaskRequest.model_json_schema(),
                "semantic_decision": SemanticDecision.model_json_schema(),
                "research_draft": ResearchDraft.model_json_schema(),
                "extraction_rules": __import__(
                    "ftr.rules", fromlist=["Rules"]
                ).Rules.model_json_schema(),
            },
        )
    if args.command == "task" and args.action == "status":
        database = root / "database.sqlite3"
        if not database.is_file():
            raise KeyError("任务数据库不存在")
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            # 在同一只读快照内查询，不争用采集进程的独占锁。
            connection.execute("BEGIN")
            row = connection.execute("SELECT state FROM tasks WHERE id=?", (args.task,)).fetchone()
            if row is None:
                raise KeyError(f"任务不存在: {args.task}")
            source_rows = connection.execute(
                "SELECT source_id,state,next_page,discovery_done,pages_count,documents_count "
                "FROM source_runs WHERE task_id=?",
                (args.task,),
            ).fetchall()
            return OperationResponse(
                operation="task status",
                task_id=args.task,
                status=row["state"],
                data={"sources": [dict(source) for source in source_rows]},
            )
    request = None
    if args.command == "collect":
        if args.request:
            if any(
                getattr(args, key) is not None
                for key in (
                    "max_pages",
                    "max_documents",
                    "sources",
                    "date_from",
                    "date_to",
                    "date_basis",
                    "mode",
                    "idempotency_key",
                )
            ):
                raise ValueError("--request 不能与来源、日期、预算或幂等参数同时使用")
            request = TaskRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
        else:
            request = TaskRequest(
                source_ids=args.sources or ["mof", "chinatax"],
                date_from=args.date_from,
                date_to=args.date_to,
                date_basis=args.date_basis or "source_listing",
                mode=args.mode or "initial",
                max_pages=settings.collection.max_pages,
                max_documents=settings.collection.max_documents,
                idempotency_key=args.idempotency_key,
            )
    with data_lock(root):
        repo = Repository(root)
        try:
            if args.command in ("collect", "task") or (
                args.command == "repair" and args.action != "context"
            ):
                from ftr.rule_repair import recover

                recover(root, repo)
            if args.command == "collect":
                assert request is not None
                collector = Collector(root, settings, proxy=effective_proxy)
                try:
                    return collector.collect(request)
                finally:
                    collector.close()
            if args.command == "task":
                repo.task(args.task)
                if args.action == "resume":
                    repo.db.execute("UPDATE tasks SET pause_requested=0 WHERE id=?", (args.task,))
                    repo.db.commit()
                    collector = Collector(root, settings, proxy=effective_proxy)
                    try:
                        return collector.resume(args.task)
                    finally:
                        collector.close()
                raise ValueError("未知任务动作")
            if args.command == "decision":
                if args.action == "list":
                    decisions = repo.pending_decisions(args.task)
                    return OperationResponse(
                        operation="decision list",
                        task_id=args.task,
                        status="COMPLETED",
                        data={"decisions": [x.model_dump(mode="json") for x in decisions]},
                    )
                decision = SemanticDecision.model_validate_json(
                    args.file.read_text(encoding="utf-8")
                )
                record = repo.submit_decision(decision)
                return OperationResponse(
                    operation="decision submit",
                    status=record.quality_state.upper(),
                    data={"record_id": record.record_id},
                )
            if args.command == "search":
                records = repo.search(args.query, args.include_limited)
                return OperationResponse(
                    operation="search",
                    status="COMPLETED",
                    data={"records": [x.model_dump(mode="json") for x in records]},
                )
            if args.command == "document":
                record = repo.get_document(args.id)
                return OperationResponse(
                    operation="document show",
                    status="COMPLETED",
                    data={"record": record.model_dump(mode="json")},
                )
            if args.command == "evidence":
                evidence = repo.get_evidence(args.id)
                return OperationResponse(
                    operation="evidence show",
                    status="COMPLETED",
                    data={"evidence": evidence.model_dump(mode="json")},
                )
            if args.command == "repair":
                from ftr.rule_repair import (
                    activate,
                    failure_context,
                    rollback,
                    submit,
                    test_candidate,
                )

                if args.action == "context":
                    return OperationResponse(
                        operation="repair context",
                        status="COMPLETED",
                        data=failure_context(repo, args.failure),
                    )
                if args.action == "propose-rule":
                    result = submit(root, repo, args.failure, args.file)
                    return OperationResponse(
                        operation="repair propose-rule", status=result["state"], data=result
                    )
                if args.action == "test-rule":
                    result = test_candidate(root, repo, args.candidate, settings, live=args.live)
                    return OperationResponse(
                        operation="repair test-rule", status=result["state"], data=result
                    )
                if args.action == "activate-rule":
                    result = activate(root, repo, args.candidate, settings)
                    return OperationResponse(
                        operation="repair activate-rule", status=result["state"], data=result
                    )
                if args.action == "rollback-rule":
                    result = rollback(root, repo, args.source)
                    return OperationResponse(
                        operation="repair rollback-rule", status=result["state"], data=result
                    )
                if args.action == "test":
                    return OperationResponse(
                        operation="repair test",
                        status="BLOCKED",
                        errors=[
                            {
                                "code": "NO_ISOLATION",
                                "message": "未配置合格隔离执行器，禁止运行未知候选代码",
                                "retryable": False,
                            }
                        ],
                    )
                candidate = prepare_candidate(root, repo, args.failure, args.patch)
                return OperationResponse(
                    operation="repair prepare", status="PATCH_PROPOSED", data=candidate
                )
            if args.command == "candidate":
                markdown = candidate_report(root, args.candidate)
                return OperationResponse(
                    operation="candidate report", status="COMPLETED", data={"markdown": markdown}
                )
            if args.command == "research":
                if args.action == "prepare":
                    records = repo.search(args.query)
                    return OperationResponse(
                        operation="research prepare",
                        status="COMPLETED",
                        data={
                            "query": args.query,
                            "records": [x.model_dump(mode="json") for x in records],
                            "warning": "仅验证资料入选；陈述支持关系仍须语义复核",
                        },
                    )
                draft = ResearchDraft.model_validate_json(args.file.read_text(encoding="utf-8"))
                result = verify_and_render(repo, draft)
                return OperationResponse(
                    operation="research submit", status="COMPLETED", data={"markdown": result}
                )
            if args.command == "export":
                if args.output.exists():
                    raise FileExistsError("导出目标已存在")
                records = repo.search(args.query)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(
                            json.dumps(record.model_dump(mode="json"), ensure_ascii=False) + "\n"
                        )
                return OperationResponse(
                    operation="export",
                    status="COMPLETED",
                    data={"output": str(args.output), "count": len(records), "format": "jsonl"},
                )
            if args.command == "backup":
                backup_result = create_backup(root, args.output)
                return OperationResponse(
                    operation=f"backup {args.action}", status="COMPLETED", data=backup_result
                )
            if args.command == "release":
                return OperationResponse(
                    operation="release status",
                    status="BLOCKED",
                    data={"adapter": args.adapter, "mode": "local-demo"},
                    errors=[
                        {
                            "code": "NO_RELEASE_MANAGER",
                            "message": "正式发布器尚未实施",
                            "retryable": False,
                        }
                    ],
                )
            raise ValueError("未知命令")
        finally:
            repo.close()


def main() -> None:
    # 固定 JSON 传输编码，避免 Windows 重定向输出受系统代码页影响。
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        result = run(args)
        code = 0 if result.status != "BLOCKED" else 3
    except (ValueError, KeyError, FileNotFoundError, ValidationError) as exc:
        result = OperationResponse(
            operation=args.command,
            status="FAILED",
            errors=[{"code": "INPUT_ERROR", "message": str(exc), "retryable": False}],
        )
        code = 2
    except Exception as exc:  # noqa: BLE001
        result = OperationResponse(
            operation=args.command,
            status="FAILED",
            errors=[{"code": "EXECUTION_ERROR", "message": str(exc), "retryable": True}],
        )
        code = 5
    print(result.model_dump_json())
    sys.exit(code)


if __name__ == "__main__":
    main()
