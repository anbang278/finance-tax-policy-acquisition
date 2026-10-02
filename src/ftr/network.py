from __future__ import annotations

import ipaddress
import random
import socket
import time
from collections.abc import Mapping, Sequence
from functools import lru_cache
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies

import httpx
import requests
from playwright.sync_api import ProxySettings

from ftr.config import NetworkSettings, validate_proxy_url

_UNSET = object()


def resolve_proxy(settings: NetworkSettings) -> str | None:
    if settings.proxy_mode == "direct":
        return None
    if settings.proxy_mode == "explicit":
        assert settings.proxy_url is not None
        return validate_proxy_url(settings.proxy_url)
    proxy = getproxies().get("https")
    if not proxy:
        return None
    return validate_proxy_url(proxy)


def is_loopback_proxy(proxy: str | None) -> bool:
    if proxy is None:
        return False
    try:
        parsed = urlsplit(validate_proxy_url(proxy))
        return bool(parsed.hostname and ipaddress.ip_address(parsed.hostname).is_loopback)
    except ValueError:
        return False


def browser_proxy(proxy: str | None) -> ProxySettings | None:
    if proxy is None:
        return None
    parsed = urlsplit(proxy)
    result: ProxySettings = {
        "server": urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], "", "", ""))
    }
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


class AccessBlocked(RuntimeError):
    pass


class TransientFailure(RuntimeError):
    """An exhausted transient request, never an access or trust-boundary failure."""


class RetryPolicy:
    def _configure_retry(self, settings):
        self.retry_attempts = settings.retry_attempts if settings else 3
        self.retry_backoff = settings.retry_backoff_seconds if settings else 1
        self.before_request = None
        self.checkpoint = None

    def _check(self):
        if self.checkpoint is not None:
            self.checkpoint()

    def _wait(self, duration):
        until = time.monotonic() + duration
        while True:
            self._check()
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _before(self, url):
        self._check()
        if self.before_request is not None:
            self.before_request(url)

    def _retry_call(self, call):
        for attempt in range(self.retry_attempts):
            try:
                return call()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                failure = TransientFailure(f"来源请求失败（{type(exc).__name__}）")
                failure.__cause__ = exc
            except TransientFailure as exc:
                failure = exc
            except RuntimeError as exc:
                if not isinstance(
                    exc.__cause__,
                    (
                        requests.ConnectionError,
                        requests.Timeout,
                        requests.exceptions.ChunkedEncodingError,
                    ),
                ):
                    raise
                failure = TransientFailure(f"来源请求失败（{type(exc.__cause__).__name__}）")
                failure.__cause__ = exc
            if attempt + 1 == self.retry_attempts:
                raise failure
            self._wait(self.retry_backoff * (2**attempt) + random.uniform(0, 0.25))
        raise AssertionError("无效重试次数")


@lru_cache(maxsize=1)
def local_https_proxy() -> str | None:
    """读取系统已配置的本地 HTTPS 代理，供浏览器和 HTTP 客户端共同使用。"""
    proxy = getproxies().get("https")
    if not proxy:
        return None
    try:
        parsed = urlsplit(proxy)
        if (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.port
            and not parsed.username
            and not parsed.password
            and ipaddress.ip_address(parsed.hostname).is_loopback
        ):
            return proxy
    except ValueError:
        pass
    return None


def check_url(url: str, allowed_hosts: list[str], proxy: object = _UNSET) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or host not in allowed_hosts:
        raise AccessBlocked(f"目标主机或协议未获许可: {url}")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise AccessBlocked("目标 URL 包含不允许的凭据或端口")
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443)}
    except socket.gaierror as exc:
        raise AccessBlocked(f"无法解析主机: {host}") from exc
    effective_proxy = local_https_proxy() if proxy is _UNSET else proxy
    for address in addresses:
        test_network = ipaddress.ip_network("198.18.0.0/15")
        if not ipaddress.ip_address(address).is_global and not (
            isinstance(effective_proxy, str)
            and is_loopback_proxy(effective_proxy)
            and ipaddress.ip_address(address) in test_network
        ):
            raise AccessBlocked(f"主机解析到非公网地址: {host}")
    return host


class BoundedClient(RetryPolicy):
    def __init__(
        self,
        allowed_hosts: list[str],
        interval: float = 2.0,
        max_bytes: int = 50_000_000,
        *,
        settings: NetworkSettings | None = None,
        proxy: object = _UNSET,
    ):
        self._configure_retry(settings)
        self.allowed_hosts = allowed_hosts
        self.interval = settings.interval_seconds if settings else interval
        self.max_bytes = settings.max_file_bytes if settings else max_bytes
        self.timeout = settings.timeout_seconds if settings else 30
        self._last_request = 0.0
        selected = (
            (resolve_proxy(settings) if settings else local_https_proxy())
            if proxy is _UNSET
            else proxy
        )
        assert selected is None or isinstance(selected, str)
        self.proxy = selected
        self._client = httpx.Client(
            timeout=self.timeout, follow_redirects=False, proxy=self.proxy, trust_env=False
        )

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> tuple[bytes, str, str]:
        try:
            return self._retry_call(lambda: self._get(url))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"来源请求失败（{type(exc).__name__}）") from exc

    def _get(self, url: str) -> tuple[bytes, str, str]:
        for _ in range(6):
            check_url(url, self.allowed_hosts, self.proxy)
            remaining = self.interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                self._wait(remaining)
            self._before(url)
            self._last_request = time.monotonic()
            with self._client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(url, response.headers.get("location", ""))
                    check_url(target, self.allowed_hosts, self.proxy)
                    url = target
                    continue
                if response.status_code in (401, 403, 429):
                    raise AccessBlocked(f"来源限制访问，HTTP {response.status_code}: {url}")
                if response.status_code in (502, 503, 504):
                    raise TransientFailure(f"来源瞬态响应异常，HTTP {response.status_code}")
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise RuntimeError(f"来源响应异常，HTTP {response.status_code}: {url}") from exc
                claimed = response.headers.get("content-length")
                if claimed and int(claimed) > self.max_bytes:
                    raise AccessBlocked("文件超过单文件上限")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise AccessBlocked("文件超过单文件上限")
                return bytes(body), str(response.url), response.headers.get("content-type", "")
        raise AccessBlocked("重定向次数超过上限")


class BrowserSessionClient(RetryPolicy):
    """仅复用同一浏览器会话访问已登记主机，凭据不落盘。"""

    def __init__(
        self,
        allowed_hosts: list[str],
        interval: float = 2.0,
        max_bytes: int = 50_000_000,
        *,
        settings: NetworkSettings | None = None,
        proxy: object = _UNSET,
    ):
        self._configure_retry(settings)
        self.allowed_hosts = allowed_hosts
        self.interval = settings.interval_seconds if settings else interval
        self.max_bytes = settings.max_file_bytes if settings else max_bytes
        self.timeout = settings.timeout_seconds if settings else 30
        self._last_request = 0.0
        self._session = requests.Session()
        selected = (
            (resolve_proxy(settings) if settings else local_https_proxy())
            if proxy is _UNSET
            else proxy
        )
        assert selected is None or isinstance(selected, str)
        self.proxy = selected
        self._session.trust_env = False
        if self.proxy:
            self._session.proxies = {"https": self.proxy}

    def set_browser_session(
        self, headers: dict[str, str], cookies: Sequence[Mapping[str, object]]
    ) -> None:
        forwarded = {"accept", "user-agent", "referer", "origin"}
        self._session.headers.update(
            {key: value for key, value in headers.items() if key.lower() in forwarded}
        )
        for cookie in cookies:
            domain = str(cookie.get("domain", "")).lstrip(".").lower()
            if domain in self.allowed_hosts or (
                domain == "chinatax.gov.cn"
                and all(host.endswith(".chinatax.gov.cn") for host in self.allowed_hosts)
            ):
                self._session.cookies.set(
                    str(cookie["name"]),
                    str(cookie["value"]),
                    domain=str(cookie["domain"]),
                    path=str(cookie.get("path", "/")),
                )

    def close(self) -> None:
        self._session.close()

    def _request(
        self, method: str, url: str, data: dict[str, str] | None = None
    ) -> tuple[bytes, str, str]:
        for _ in range(6):
            check_url(url, self.allowed_hosts, self.proxy)
            remaining = self.interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                self._wait(remaining)
            self._before(url)
            self._last_request = time.monotonic()
            try:
                response = self._session.request(
                    method, url, data=data, timeout=self.timeout, stream=True, allow_redirects=False
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"来源请求失败: {url}") from exc
            with response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if method != "GET":
                        raise AccessBlocked("列表接口发生重定向，停止复用会话")
                    target = urljoin(url, response.headers.get("location", ""))
                    check_url(target, self.allowed_hosts, self.proxy)
                    url = target
                    continue
                if response.status_code in (401, 403, 429):
                    raise AccessBlocked(f"来源限制访问，HTTP {response.status_code}: {url}")
                if response.status_code in (502, 503, 504):
                    raise TransientFailure(f"来源瞬态响应异常，HTTP {response.status_code}")
                try:
                    response.raise_for_status()
                except requests.RequestException as exc:
                    raise RuntimeError(f"来源响应异常，HTTP {response.status_code}: {url}") from exc
                claimed = response.headers.get("content-length")
                if claimed and int(claimed) > self.max_bytes:
                    raise AccessBlocked("文件超过单文件上限")
                body = bytearray()
                try:
                    for chunk in response.iter_content(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > self.max_bytes:
                            raise AccessBlocked("文件超过单文件上限")
                except requests.RequestException as exc:
                    raise RuntimeError(f"来源下载中断: {url}") from exc
                return bytes(body), response.url, response.headers.get("content-type", "")
        raise AccessBlocked("重定向次数超过上限")

    def _retry(self, method, url, data=None):
        return self._retry_call(lambda: self._request(method, url, data))

    def get(self, url: str) -> tuple[bytes, str, str]:
        return self._retry("GET", url)

    def post_form(self, url: str, data: dict[str, str]) -> tuple[bytes, str, str]:
        return self._retry("POST", url, data)
