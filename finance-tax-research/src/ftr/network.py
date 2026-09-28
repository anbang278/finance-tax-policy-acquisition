from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Mapping, Sequence
from functools import lru_cache
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies

import httpx
import requests


class AccessBlocked(RuntimeError):
    pass


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


def check_url(url: str, allowed_hosts: list[str]) -> str:
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
    proxy = local_https_proxy()
    for address in addresses:
        test_network = ipaddress.ip_network("198.18.0.0/15")
        if not ipaddress.ip_address(address).is_global and not (
            proxy is not None
            and ipaddress.ip_address(address) in test_network
        ):
            raise AccessBlocked(f"主机解析到非公网地址: {host}")
    return host


class BoundedClient:
    def __init__(
        self, allowed_hosts: list[str], interval: float = 2.0, max_bytes: int = 50_000_000
    ):
        self.allowed_hosts = allowed_hosts
        self.interval = interval
        self.max_bytes = max_bytes
        self._last_request = 0.0
        proxy = local_https_proxy()
        self._client = httpx.Client(
            timeout=30, follow_redirects=False, proxy=proxy, trust_env=proxy is None
        )

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> tuple[bytes, str, str]:
        try:
            return self._get(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"来源请求失败（{type(exc).__name__}）: {url}") from exc

    def _get(self, url: str) -> tuple[bytes, str, str]:
        for _ in range(6):
            check_url(url, self.allowed_hosts)
            remaining = self.interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
            with self._client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = urljoin(url, response.headers.get("location", ""))
                    check_url(target, self.allowed_hosts)
                    url = target
                    continue
                if response.status_code in (401, 403, 429):
                    raise AccessBlocked(f"来源限制访问，HTTP {response.status_code}: {url}")
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


class BrowserSessionClient:
    """仅复用同一浏览器会话访问已登记主机，凭据不落盘。"""

    def __init__(
        self, allowed_hosts: list[str], interval: float = 2.0, max_bytes: int = 50_000_000
    ):
        self.allowed_hosts = allowed_hosts
        self.interval = interval
        self.max_bytes = max_bytes
        self._last_request = 0.0
        self._session = requests.Session()
        proxy = local_https_proxy()
        if proxy:
            self._session.trust_env = False
            self._session.proxies = {"https": proxy}

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
            check_url(url, self.allowed_hosts)
            remaining = self.interval - (time.monotonic() - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()
            try:
                response = self._session.request(
                    method, url, data=data, timeout=30, stream=True, allow_redirects=False
                )
            except requests.RequestException as exc:
                raise RuntimeError(f"来源请求失败: {url}") from exc
            with response:
                if response.status_code in (301, 302, 303, 307, 308):
                    if method != "GET":
                        raise AccessBlocked("列表接口发生重定向，停止复用会话")
                    target = urljoin(url, response.headers.get("location", ""))
                    check_url(target, self.allowed_hosts)
                    url = target
                    continue
                if response.status_code in (401, 403, 429):
                    raise AccessBlocked(f"来源限制访问，HTTP {response.status_code}: {url}")
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

    def get(self, url: str) -> tuple[bytes, str, str]:
        return self._request("GET", url)

    def post_form(self, url: str, data: dict[str, str]) -> tuple[bytes, str, str]:
        return self._request("POST", url, data)
