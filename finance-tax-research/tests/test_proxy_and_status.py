from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from ftr import network
from ftr.cli import parser, run
from ftr.models import TaskRequest
from ftr.repository import Repository
from ftr.runtime import data_lock


@pytest.fixture(autouse=True)
def isolate_proxy_snapshot():
    network.local_https_proxy.cache_clear()
    yield
    network.local_https_proxy.cache_clear()


def test_proxy_configuration_is_stable_during_one_process(monkeypatch):
    monkeypatch.setattr(network, "getproxies", lambda: {"https": "http://127.0.0.1:7892"})
    assert network.local_https_proxy() == "http://127.0.0.1:7892"
    monkeypatch.setattr(network, "getproxies", dict)
    assert network.local_https_proxy() == "http://127.0.0.1:7892"


@pytest.mark.parametrize(
    "address,proxy,allowed",
    [
        ("198.18.0.125", "http://127.0.0.1:7892", True),
        ("198.19.255.254", "http://127.0.0.1:7892", True),
        ("198.18.0.125", None, False),
        ("198.18.0.125", "http://192.168.1.1:7892", False),
        ("198.18.0.125", "http://127.0.0.1:invalid", False),
        ("127.0.0.1", "http://127.0.0.1:7892", False),
        ("10.0.0.1", "http://127.0.0.1:7892", False),
        ("169.254.169.254", "http://127.0.0.1:7892", False),
        ("::1", "http://127.0.0.1:7892", False),
        ("203.0.113.1", "http://127.0.0.1:7892", False),
    ],
)
def test_proxy_dns_keeps_nonpublic_address_boundary(monkeypatch, address, proxy, allowed):
    monkeypatch.setattr(network, "getproxies", lambda: {"https": proxy} if proxy else {})
    monkeypatch.setattr(
        network.socket, "getaddrinfo", lambda *_: [(None, None, None, None, (address, 443))]
    )
    if allowed:
        assert network.check_url("https://www.mof.gov.cn/", ["www.mof.gov.cn"]) == "www.mof.gov.cn"
    else:
        with pytest.raises(network.AccessBlocked, match="非公网"):
            network.check_url("https://www.mof.gov.cn/", ["www.mof.gov.cn"])


def test_both_http_clients_explicitly_use_configured_proxy(monkeypatch):
    proxy = "http://127.0.0.1:7892"
    monkeypatch.setattr(network, "getproxies", lambda: {"https": proxy})
    factory = Mock()
    monkeypatch.setattr(network.httpx, "Client", factory)
    client = network.BoundedClient(["www.mof.gov.cn"])
    factory.assert_called_once_with(
        timeout=30, follow_redirects=False, proxy=proxy, trust_env=False
    )
    client.close()
    tax_client = network.BrowserSessionClient(["www.chinatax.gov.cn"])
    try:
        assert tax_client._session.proxies == {"https": proxy}
        assert tax_client._session.trust_env is False
    finally:
        tax_client.close()


def test_task_status_reads_source_row_names(tmp_path, monkeypatch):
    monkeypatch.setenv("FTR_DATA_DIR", str(tmp_path))
    repo = Repository(tmp_path)
    task_id = repo.create_task(
        TaskRequest(date_from=date(2026, 6, 28), date_to=date(2026, 9, 28))
    )
    repo.set_task_state(task_id, "PARTIAL")
    repo.close()
    with data_lock(tmp_path):
        result = run(parser().parse_args(["task", "status", "--task", task_id]))
    assert result.task_id == task_id
    assert result.status == "PARTIAL"
    assert {source["source_id"] for source in result.data["sources"]} == {"mof", "chinatax"}
    assert all(source["pages_count"] == 0 for source in result.data["sources"])


def test_http_timeout_becomes_recoverable_collection_error(monkeypatch):
    monkeypatch.setattr(network, "getproxies", dict)
    monkeypatch.setattr(network, "check_url", lambda *_: "www.mof.gov.cn")
    client = network.BoundedClient(["www.mof.gov.cn"], interval=0)
    client._client.close()

    def timeout(request):
        raise httpx.ReadTimeout("读取超时", request=request)

    client._client = httpx.Client(transport=httpx.MockTransport(timeout))
    try:
        with pytest.raises(RuntimeError, match="ReadTimeout"):
            client.get("https://www.mof.gov.cn/")
    finally:
        client.close()
