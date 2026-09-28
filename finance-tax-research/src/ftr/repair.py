from __future__ import annotations

import json
from pathlib import Path

from ftr.models import digest, new_id
from ftr.repository import Repository

PROTECTED = (
    "../",
    "/",
    "tests/",
    "config/",
    "contracts/",
    "skills/",
    "docs/",
    "src/ftr/network.py",
    "src/ftr/repository.py",
    "src/ftr/runtime.py",
    "src/ftr/repair.py",
    ".codex-plugin/",
    "plugin.json",
    "pyproject.toml",
    "uv.lock",
)


def allowed_path(path: str, source_id: str) -> bool:
    normalized = path.replace("\\", "/")
    if any(normalized.startswith(prefix) for prefix in PROTECTED):
        return False
    if ".." in normalized.split("/") or normalized.startswith("/"):
        return False
    return (
        normalized.startswith(f"src/ftr/adapters/{source_id}/")
        or normalized == f"src/ftr/adapters/{source_id}.py"
    )


def inspect_patch(patch: str, source_id: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/"):
                raise ValueError("补丁路径格式不合法")
            for path in (parts[2][2:], parts[3][2:]):
                if not allowed_path(path, source_id):
                    raise ValueError(f"补丁触及受保护路径: {path}")
                paths.append(path)
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].split("\t", 1)[0]
            if path == "/dev/null":
                continue
            if not path.startswith(("a/", "b/")):
                raise ValueError("补丁路径格式不合法")
            path = path[2:]
            if not allowed_path(path, source_id):
                raise ValueError(f"补丁触及受保护路径: {path}")
            paths.append(path)
    if not paths or not any(line.startswith("@@") for line in patch.splitlines()):
        raise ValueError("补丁缺少有效 diff")
    return sorted(set(paths))


def prepare_candidate(data_dir: Path, repo: Repository, failure_id: str, patch_path: Path) -> dict:
    row = repo.db.execute("SELECT * FROM failures WHERE id=?", (failure_id,)).fetchone()
    if row is None:
        raise KeyError("失败记录不存在")
    if row["category"] not in (
        "STRUCTURE_DRIFT",
        "PAGINATION_DRIFT",
        "SEMANTIC_MISMATCH",
        "UNKNOWN",
    ):
        raise ValueError("此故障类别不得自动生成代码修复候选")
    patch = patch_path.read_text(encoding="utf-8")
    paths = inspect_patch(patch, str(row["source_id"]))
    candidate_id = new_id()
    folder = data_dir / "candidates" / candidate_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "candidate.patch").write_text(patch, encoding="utf-8")
    manifest = {
        "candidate_id": candidate_id,
        "failure_id": failure_id,
        "source_id": row["source_id"],
        "patch_sha256": digest(patch.encode()),
        "changed_paths": paths,
        "state": "PATCH_PROPOSED",
        "tests": "BLOCKED_NO_ISOLATION",
        "release_allowed": False,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    repo.audit(str(row["task_id"]), "repair_candidate", manifest)
    return manifest


def candidate_report(data_dir: Path, candidate_id: str) -> str:
    folder = data_dir / "candidates" / candidate_id
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    return (
        f"# 修复候选 {candidate_id}\n\n来源：{manifest['source_id']}\n\n"
        f"故障：{manifest['failure_id']}\n\n补丁摘要：{manifest['patch_sha256']}\n\n"
        f"修改文件：{', '.join(manifest['changed_paths'])}\n\n"
        "验证：未执行候选代码与回归测试，缺少合格隔离环境。\n\n"
        "发布状态：禁止发布。审批者可审查原因与补丁，但当前候选不能转为可发布状态。\n"
    )
