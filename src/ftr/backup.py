from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

import portalocker

from ftr.models import digest, utc_now


def _files(root: Path) -> dict[str, str]:
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("备份目录包含符号链接")
    return {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    }


def _checked_file(root: Path, relative: str) -> Path:
    normalized = _relative_path(relative)
    path = Path(normalized)
    if path.is_absolute():
        raise ValueError("备份路径越界")
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
        raise ValueError("备份路径越界")
    return target


def _relative_path(relative: str) -> str:
    if not isinstance(relative, str) or "\x00" in relative:
        raise ValueError("备份路径无效")
    path = PurePosixPath(relative.replace("\\", "/"))
    if (
        path.is_absolute()
        or PureWindowsPath(relative).drive
        or ".." in path.parts
        or not path.parts
    ):
        raise ValueError("备份路径越界")
    return path.as_posix()


def _expected(backup_dir: Path) -> dict[str, str]:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest["files"]
    if not isinstance(expected, dict) or not all(
        isinstance(relative, str) and isinstance(value, str) for relative, value in expected.items()
    ):
        raise ValueError("备份清单格式无效")
    normalized = {}
    for relative, value in expected.items():
        name = _relative_path(relative)
        if name in normalized:
            raise ValueError("备份清单包含重复规范路径")
        normalized[name] = value
    return normalized


def create_backup(data_dir: Path, destination: Path) -> dict:
    if destination.exists():
        raise FileExistsError("备份目标已存在")
    if destination.resolve().is_relative_to(data_dir.resolve()):
        raise ValueError("备份目标不得位于运行数据目录内")
    destination.mkdir(parents=True)
    try:
        source = sqlite3.connect(data_dir / "database.sqlite3")
        try:
            target = sqlite3.connect(destination / "database.sqlite3")
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        if (data_dir / "evidence").exists():
            shutil.copytree(data_dir / "evidence", destination / "evidence")
        if (data_dir / "rules").exists():
            shutil.copytree(data_dir / "rules", destination / "rules", symlinks=True)
        if (data_dir / "commands").exists():
            if (data_dir / "commands").is_symlink():
                raise ValueError("命令目录不得为符号链接")
            # Web enqueues outside the business write lock; freeze its spool separately.
            with portalocker.Lock(str(data_dir / ".commands.lock"), timeout=1):
                shutil.copytree(data_dir / "commands", destination / "commands", symlinks=True)
        manifest = {"created_at": utc_now().isoformat(), "files": _files(destination)}
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        verify_backup(destination)
        return manifest
    except Exception:
        shutil.rmtree(destination)
        raise


def verify_backup(backup_dir: Path) -> dict:
    expected = _expected(backup_dir)
    for relative in expected:
        _checked_file(backup_dir, relative)
    if _files(backup_dir) != expected:
        raise ValueError("备份文件列表或哈希不匹配")
    db = sqlite3.connect(
        (backup_dir / "database.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查失败")
        for (metadata_json,) in db.execute("SELECT metadata_json FROM evidence"):
            evidence = json.loads(metadata_json)
            file = _checked_file(backup_dir, f"evidence/{evidence['relative_path']}")
            if not file.is_file() or digest(file.read_bytes()) != evidence["sha256"]:
                raise ValueError("备份证据引用或哈希不匹配")
    finally:
        db.close()
    return {"file_count": len(expected), "verified": True}


def restore_backup(backup_dir: Path, destination: Path) -> dict:
    result = verify_backup(backup_dir)
    if destination.exists():
        raise FileExistsError("恢复目标已存在，禁止覆盖运行数据")
    destination.mkdir(parents=True)
    try:
        expected = _expected(backup_dir)
        for relative in expected:
            source = _checked_file(backup_dir, relative)
            target = _checked_file(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        if _files(destination) != expected:
            raise ValueError("恢复后哈希不匹配")
    except Exception:
        shutil.rmtree(destination)
        raise
    return result
