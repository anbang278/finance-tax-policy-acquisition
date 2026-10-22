from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from ftr.knowledge.service import Knowledge
from ftr.models import OperationResponse, new_id
from ftr.repository import Repository
from ftr.runtime import data_lock


def add_parsers(commands):
    k = commands.add_parser("knowledge", help="知识、证据索引、加工与人工审核").add_subparsers(
        dest="entity", required=True
    )
    batch = k.add_parser("batch", help="逐项人工审核或发布")
    batch.add_argument("--file", type=Path, required=True)
    k.add_parser("seed", help="显式导入企业所得税优惠导航样例")
    for entity, actions in {
        "domain": ["list", "import"],
        "topic": ["list"],
        "job": [
            "create",
            "list",
            "show",
            "export",
            "start",
            "submit",
            "retry",
            "cancel",
            "run-api",
        ],
        "item": ["list", "show"],
        "index": ["rebuild", "refresh"],
        "impact": ["scan", "list", "resolve"],
        "review": ["submit"],
        "publish": ["submit"],
        "edit": ["submit"],
    }.items():
        subs = k.add_parser(entity).add_subparsers(dest="action", required=True)
        for action in actions:
            p = subs.add_parser(action)
            if action in ("import", "create", "submit", "resolve"):
                p.add_argument("--file", type=Path, required=True)
            if action in ("show", "export", "start", "retry", "cancel", "refresh"):
                p.add_argument("--id", required=True)
            if action == "export":
                p.add_argument("--output", type=Path, required=True)
            if action == "start":
                p.add_argument("--executor", choices=["host", "api"], default="host")
            if action == "run-api":
                p.add_argument("--id")
    s = k.add_parser("search")
    s.add_argument("--query", required=True)
    s.add_argument("--kind", choices=["all", "material", "item", "page"], default="all")
    s.add_argument("--include-limited", action="store_true")
    s.add_argument("--as-of")
    s.add_argument("--domain")
    s.add_argument("--topic")
    s.add_argument("--region")
    s.add_argument("--subject")
    w = commands.add_parser("wiki", help="专题 Wiki 候选、编辑、发布与导出").add_subparsers(
        dest="action", required=True
    )
    for action in ("list", "show", "draft", "edit", "review", "publish", "diff", "export"):
        p = w.add_parser(action)
        if action in ("draft", "edit", "review", "publish"):
            p.add_argument("--file", type=Path, required=True)
        if action in ("show", "diff", "export"):
            p.add_argument("--id", required=True)
        if action in ("diff", "export"):
            p.add_argument("--version", required=action == "diff")
        if action == "export":
            p.add_argument("--output", type=Path, required=True)


def run(args, settings):
    from ftr.knowledge.processing import execute_next, put_blob

    root = settings.data_dir
    entity = getattr(args, "entity", None)
    action = getattr(args, "action", None)
    result = {}
    op = None
    payload = {}
    if args.command == "knowledge" and entity == "job" and action == "run-api":
        result = execute_next(settings, args.id)
    elif args.command == "knowledge" and entity == "batch":
        op = "batch"
        payload = json.loads(args.file.read_text(encoding="utf-8"))
    elif args.command == "knowledge" and entity == "seed":
        op = "seed"
    elif args.command == "knowledge" and (entity, action) in (
        ("domain", "import"),
        ("job", "create"),
        ("job", "submit"),
        ("review", "submit"),
        ("publish", "submit"),
        ("edit", "submit"),
        ("impact", "resolve"),
    ):
        op = {
            ("domain", "import"): "domain.import",
            ("job", "create"): "job.create",
            ("job", "submit"): "job.submit",
            ("review", "submit"): "review",
            ("publish", "submit"): "publish",
            ("edit", "submit"): "object.edit",
            ("impact", "resolve"): "impact.resolve",
        }[(entity, action)]
        raw = args.file.read_bytes()
        payload = {"blob_id": put_blob(root, raw)} if op == "job.submit" else json.loads(raw)
    elif args.command == "knowledge" and entity == "job" and action in ("start", "retry", "cancel"):
        op = "job." + action
        payload = {"id": args.id}
        if action == "start":
            payload["executor"] = args.executor
    elif (
        args.command == "knowledge"
        and entity in ("index", "impact")
        and action in ("rebuild", "refresh", "scan")
    ):
        op = entity + "." + action
        if action == "refresh":
            payload = {"id": args.id}
    elif args.command == "wiki" and action in ("draft", "edit", "review", "publish"):
        op = {
            "draft": "job.create",
            "edit": "object.edit",
            "review": "review",
            "publish": "publish",
        }[action]
        payload = json.loads(args.file.read_text(encoding="utf-8"))
    else:
        with closing(Repository(root, readonly=True)) as repo:
            k = Knowledge(repo)
            if args.command == "knowledge" and entity == "search":
                result = k.search(
                    args.query,
                    args.kind,
                    args.include_limited,
                    args.as_of,
                    args.domain,
                    args.topic,
                    args.region,
                    args.subject,
                )
            elif args.command == "knowledge" and action == "list":
                name = {
                    "domain": "domains",
                    "topic": "topics",
                    "job": "jobs",
                    "item": "objects",
                    "impact": "impacts",
                }[entity]
                result = {"items": k.listing(name)}
            elif args.command == "knowledge" and entity == "job":
                result = k.package(args.id) if action == "export" else k.job(args.id)
                if action == "export":
                    args.output.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
            elif args.command == "knowledge" and entity == "item":
                result = k.detail(args.id)
            elif action == "list":
                result = {"items": [o for o in k.listing("objects") if o["kind"] == "page"]}
            elif action == "show":
                result = k.detail(args.id)
            elif action == "diff":
                result = k.diff(args.id, args.version)
            elif action == "export":
                text = k.export(args.id, args.version)
                args.output.write_text(text, encoding="utf-8")
                result = {"output": str(args.output.resolve())}
    if op:
        from ftr.knowledge.indexing import prepare_index, prepare_refresh
        from ftr.management import Command, apply_command

        prepared_index = (
            prepare_index(root)
            if op == "index.rebuild"
            else prepare_refresh(root, args.id)
            if op == "index.refresh"
            else None
        )

        # CLI is the explicit writer. Use the same atomic command record as Web.
        cmd = Command(
            id=new_id(), action="knowledge.apply", payload={"operation": op, "arguments": payload}
        )
        with data_lock(root), closing(Repository(root)) as repo:
            repo.db.execute("BEGIN")
            repo.db.defer_commit = True
            try:
                result = apply_command(repo, cmd, settings, prepared_index=prepared_index)
                from ftr.knowledge.service import now

                repo.db.execute(
                    "INSERT INTO management_operations VALUES(?,?,?,?,?)",
                    (
                        cmd.id,
                        cmd.model_dump_json(),
                        "APPLIED",
                        json.dumps(result, ensure_ascii=False),
                        now(),
                    ),
                )
                repo.db.defer_commit = False
                repo.db.commit()
            except Exception as exc:
                repo.db.defer_commit = False
                repo.db.rollback()
                from ftr.knowledge.commands import record_rejection

                with repo.db:
                    record_rejection(repo, cmd.payload, exc)
                    from ftr.knowledge.service import now

                    repo.db.execute(
                        "INSERT INTO management_operations VALUES(?,?,?,?,?)",
                        (
                            cmd.id,
                            cmd.model_dump_json(),
                            "FAILED",
                            json.dumps(
                                {"code": getattr(exc, "code", "INVALID"), "message": "操作校验失败"}
                            ),
                            now(),
                        ),
                    )
                raise
    return OperationResponse(operation=args.command, status="COMPLETED", data=result)
