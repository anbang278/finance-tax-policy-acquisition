"""Registered identities and frozen channel scopes, independent of runtime settings."""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, ValidationInfo

from ftr.config import SourceConfig, load_sources

LEGACY_SOURCE_IDS = ("mof", "chinatax")


def registered_source(value: str, info: ValidationInfo) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value):
        raise ValueError("来源ID格式无效")
    if not (info.context or {}).get("saved") and value not in load_sources().sources:
        raise ValueError(f"未登记来源: {value}")
    return value


RegisteredSourceId = Annotated[str, AfterValidator(registered_source)]


def validate_source_ids(ids: list[str]) -> list[str]:
    registry = load_sources().sources
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("来源范围必须非空且不重复")
    for source_id in ids:
        if source_id not in registry:
            raise ValueError(f"未登记来源: {source_id}")
    return list(ids)


def _digest(config: dict) -> str:
    return sha256(
        json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot_digest(snapshot: dict) -> str:
    return _digest(snapshot)


def source_snapshot(ids: list[str]) -> dict:
    registry = load_sources().sources
    return {
        source_id: {
            "config": registry[source_id].model_dump(mode="json"),
            "digest": _digest(registry[source_id].model_dump(mode="json")),
        }
        for source_id in validate_source_ids(ids)
    }


def resolve_source(source_id: str, snapshot: dict) -> SourceConfig:
    """Frozen extraction semantics; the current trust registry can only narrow hosts."""
    current = load_sources().sources.get(source_id)
    if current is None:
        raise ValueError(f"来源已撤销或未登记: {source_id}")
    if not snapshot:  # Historical requests retain the original legacy channels.
        return current
    item = snapshot.get(source_id)
    if not isinstance(item, dict) or item.get("digest") != _digest(item.get("config", {})):
        raise ValueError(f"来源快照损坏: {source_id}")
    frozen = SourceConfig.model_validate(item["config"])
    hosts = [host for host in frozen.allowed_hosts if host in current.allowed_hosts]
    parsed = urlsplit(frozen.entry)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"冻结入口不再符合当前来源信任规则: {source_id}")
    return frozen.model_copy(update={"allowed_hosts": hosts})


def freeze_scope(ids: list[str], snapshot: dict, *, trusted_snapshot: bool = False) -> dict:
    validate_source_ids(ids)
    if not snapshot:
        return source_snapshot(ids)
    if set(snapshot) != set(ids):
        raise ValueError("来源快照与所选范围不一致")
    if not trusted_snapshot and snapshot != source_snapshot(ids):
        raise ValueError("来源快照已过期或被修改，请重新预览")
    for source_id in ids:
        resolve_source(source_id, snapshot)
    return snapshot


def preview_scope(ids: list[str], **window) -> dict:
    snapshot = source_snapshot(ids)
    return {
        "source_ids": ids,
        "source_snapshot": snapshot,
        "sources": [{"source_id": key, **item["config"]} for key, item in snapshot.items()],
        **window,
    }


def catalog_entries() -> list[dict]:
    """Discovery gaps are visible catalog entries, never trusted source identities."""
    from pathlib import Path

    import yaml  # type: ignore[import-untyped]

    from ftr.config import package_root

    entries = []
    for key, config in load_sources().sources.items():
        selectable = config.entry.startswith("https://")
        if config.adapter.startswith("public_"):
            from ftr.adapters.public import PublicProfile

            try:
                PublicProfile.model_validate(config.profile)
            except ValueError:
                selectable = False
        entries.append(
            {
                "source_id": key,
                **config.model_dump(mode="json"),
                "selectable": selectable,
                "collectable": config.verification_status.lower() in {"pass", "passed"},
            }
        )
    pending_path = package_root() / "data" / "pending-sources.yaml"
    if not pending_path.is_file():
        pending_path = package_root().parents[1] / "config" / "pending-sources.yaml"
    if pending_path.is_file():
        payload = yaml.safe_load(Path(pending_path).read_text(encoding="utf-8")) or {}
        known = {item["source_id"] for item in entries}
        for item in payload.get("pending", []):
            if item["source_id"] in known:
                continue
            entries.append(
                {
                    "name": item["publisher"] + "｜" + item["channel"],
                    "categories": ["tax", "practice"]
                    if item["channel"] in ("qa", "interpretation")
                    else ["tax"],
                    "listing_date_kind": "unknown",
                    "allowed_hosts": [],
                    "adapter": "pending",
                    **item,
                    "selectable": False,
                    "collectable": False,
                }
            )
    return entries
