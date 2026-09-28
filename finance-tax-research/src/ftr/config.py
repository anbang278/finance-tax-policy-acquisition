from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    name: str
    entry: str
    allowed_hosts: list[str] = Field(min_length=1)
    listing_date_kind: str


class SourceRegistry(BaseModel):
    sources: dict[str, SourceConfig]


def package_root() -> Path:
    return Path(str(files("ftr")))


def load_sources() -> SourceRegistry:
    path = package_root() / "data" / "sources.yaml"
    if not path.is_file():
        path = package_root().parents[1] / "config" / "sources.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"缺少来源配置: {path}")
    return SourceRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
