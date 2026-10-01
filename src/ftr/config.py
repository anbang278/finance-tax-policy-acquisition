from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import yaml  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    ValidationError,
    model_validator,
)


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class NetworkSettings(SettingsModel):
    proxy_mode: Literal["system", "direct", "explicit"] = "system"
    proxy_url: str | None = None
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False, strict=True)
    interval_seconds: float = Field(default=2, ge=0, allow_inf_nan=False, strict=True)
    max_file_bytes: int = Field(default=50_000_000, gt=0, strict=True)

    @model_validator(mode="after")
    def proxy_configuration(self) -> NetworkSettings:
        if self.proxy_mode == "explicit":
            if not self.proxy_url:
                raise ValueError("explicit 代理模式必须提供 proxy_url")
            validate_proxy_url(self.proxy_url)
        elif self.proxy_url is not None:
            raise ValueError("proxy_url 仅用于 explicit 代理模式")
        return self


def validate_proxy_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or port == 0
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
        ):
            raise ValueError
    except ValueError as exc:
        raise ValueError("代理地址须为有效的 HTTP/HTTPS URL（可含凭据），不能含路径或查询") from exc
    return value


def redact_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if parsed.username is None and parsed.password is None:
        return value
    authority = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, "***:***@" + authority, parsed.path, "", ""))


class BrowserSettings(SettingsModel):
    headless: StrictBool = False
    executable_path: Path | None = None
    timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False, strict=True)


class CollectionSettings(SettingsModel):
    max_pages: int = Field(default=1000, ge=1, le=1000, strict=True)
    max_documents: int = Field(default=1000, ge=1, le=1000, strict=True)
    max_duration_seconds: float = Field(default=1800, gt=0, allow_inf_nan=False, strict=True)
    max_bytes_per_source: int = Field(default=500_000_000, gt=0, strict=True)


class WebSettings(SettingsModel):
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8765, ge=1, le=65535, strict=True)
    poll_interval_ms: int = Field(default=5000, ge=100, strict=True)
    request_timeout_ms: int = Field(default=10000, ge=100, strict=True)

    @model_validator(mode="after")
    def bind_address(self) -> WebSettings:
        import ipaddress

        if self.host != "localhost":
            try:
                ipaddress.ip_address(self.host)
            except ValueError as exc:
                raise ValueError("host 须为 IPv4/IPv6 地址或 localhost") from exc
        return self


class RuntimeSettings(SettingsModel):
    data_dir: Path = Field(default_factory=lambda: Path.home() / ".local" / "share" / "ftr")
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    _origins: dict[str, str] = PrivateAttr(default_factory=dict)
    _config_path: Path | None = PrivateAttr(default=None)

    @property
    def origins(self) -> dict[str, str]:
        return dict(self._origins)

    def public_dict(self) -> dict[str, Any]:
        value = self.model_dump(mode="json")
        value["network"]["proxy_url"] = redact_proxy(self.network.proxy_url)
        return value

    def describe(self) -> dict[str, Any]:
        return {
            "settings": self.public_dict(),
            "origins": self.origins,
            "config_path": str(self._config_path) if self._config_path else None,
        }


class UniqueLoader(yaml.SafeLoader):
    """避免重复键在 YAML 加载时静默覆盖。"""


def _unique_mapping(loader: UniqueLoader, node: Any, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValueError("配置键必须为字符串且不能重复")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_settings(
    config_path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> RuntimeSettings:
    env = dict(os.environ) if environ is None else environ
    cwd = Path.cwd()
    path = config_path
    if path is None and "FTR_CONFIG" in env:
        if not env["FTR_CONFIG"].strip():
            raise ValueError("FTR_CONFIG 不能为空")
        path = Path(env["FTR_CONFIG"])
    if path is not None:
        path = path.expanduser().resolve()
    values: dict[str, Any] = {}
    origins: dict[str, str] = {}
    defaults = RuntimeSettings().model_dump()
    for key, value in defaults.items():
        if isinstance(value, dict):
            for field in value:
                origins[f"{key}.{field}"] = "default"
        else:
            origins[key] = "default"

    def merge(layer: dict[str, Any], origin: str, base: Path) -> None:
        for key, value in layer.items():
            if key == "data_dir" or (key == "browser" and isinstance(value, dict)):
                if key == "browser":
                    value = dict(value)
                    raw = value.get("executable_path")
                    if raw is not None:
                        value["executable_path"] = normalize_path(raw, base)
                else:
                    value = normalize_path(value, base)
            if isinstance(value, dict) and isinstance(defaults.get(key), dict):
                existing = values.setdefault(key, {})
                if not isinstance(existing, dict):
                    raise ValueError(f"{key} 必须是映射")  # noqa: TRY004
                existing.update(value)
                for field in value:
                    origins[f"{key}.{field}"] = origin
            else:
                values[key] = value
                origins[key] = origin

    if path is not None:
        # 不将 YAML 解析异常中的原文（可能含代理密码）带入 CLI 输出。
        try:
            content = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
        except yaml.YAMLError as exc:
            raise ValueError("配置 YAML 语法错误") from exc
        if not isinstance(content, dict):
            raise ValueError("配置 YAML 顶层必须是映射")
        merge(content, "yaml", path.parent)
        # 在覆盖前验证各层，防止错误 YAML 被高优先级参数掩盖。
        _validate_settings(values)
    environment: dict[str, Any] = {}
    supported = {"FTR_CONFIG", "FTR_DATA_DIR", "FTR_WEB_BROWSER"}
    for group, fields in defaults.items():
        if not isinstance(fields, dict):
            continue
        for field in fields:
            name = f"FTR_{group.upper()}__{field.upper()}"
            supported.add(name)
            if name in env:
                raw: Any = env[name]
                if field in ("proxy_url", "executable_path") and not raw.strip():
                    raw = None
                if field not in ("host", "proxy_mode", "proxy_url", "executable_path"):
                    try:
                        raw = yaml.safe_load(raw)
                    except yaml.YAMLError as exc:
                        raise ValueError(f"环境变量格式无效: {name}") from exc
                environment.setdefault(group, {})[field] = raw
    unknown = sorted(name for name in env if name.startswith("FTR_") and name not in supported)
    if unknown:
        raise ValueError("未知环境变量: " + ", ".join(unknown))
    if "FTR_DATA_DIR" in env:
        environment["data_dir"] = env["FTR_DATA_DIR"]
    merge(environment, "env", cwd)
    _validate_settings(values)
    merge(overrides or {}, "cli", cwd)
    settings = _validate_settings(values)
    settings.data_dir = settings.data_dir.expanduser().resolve()
    settings._origins = origins
    settings._config_path = path
    return settings


def normalize_path(value: Any, base: Path) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError("路径必须是非空字符串")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _validate_settings(values: dict[str, Any]) -> RuntimeSettings:
    try:
        return RuntimeSettings.model_validate(values)
    except ValidationError as exc:
        messages = [
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in exc.errors(include_input=False, include_context=False)
        ]
        raise ValueError("配置无效: " + "; ".join(messages)) from exc


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
