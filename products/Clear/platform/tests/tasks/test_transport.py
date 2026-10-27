import asyncio
from argparse import Namespace

import httpx
import pytest

from workbench_platform.tasks.transport import (
    private_worker_headers, private_worker_url, public_endpoint,
)


@pytest.fixture(autouse=True)
def explicit_network(monkeypatch):
    for name in ('CLEAR_PUBLIC_ORIGIN', 'CLEAR_ALLOW_LAN_HTTP',
                 'CLEAR_ALLOW_HTTP_LOOPBACK', 'CLEAR_WORKER_URL'):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize('origin', [
    'http://192.168.1.8:8889', 'http://10.2.3.4:9021', 'http://[fd12::9]:8889',
])
def test_lan_http_requires_explicit_selection(monkeypatch, origin):
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN', origin)
    monkeypatch.setenv('CLEAR_ALLOW_HTTP_LOOPBACK', '1')
    with pytest.raises(RuntimeError, match='HTTPS'):
        public_endpoint()
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP', '1')
    endpoint = public_endpoint()
    assert endpoint.origin == origin and endpoint.insecure_http


@pytest.mark.parametrize('origin', [
    'http://0.0.0.0:8889', 'http://8.8.8.8:8889', 'http://myserver:8889',
    'http://localhost:8889', 'http://169.254.1.2:8889',
    'https://admin:password@server:8889', 'https://server:8889/path',
    'https://server:8889?query=1', 'https://server:8889#fragment',
    'https://*.server:8889', 'https://server:0', 'https://server:99999',
    'https://server:8889 ', 'ftp://server:8889',
])
def test_lan_mode_does_not_broaden_origin_allowlist(monkeypatch, origin):
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN', origin)
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP', '1')
    with pytest.raises(RuntimeError):
        public_endpoint()


def test_http_flag_does_not_remove_https_cookie_security(monkeypatch):
    monkeypatch.setenv('CLEAR_ALLOW_HTTP_LOOPBACK', '1')
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP', '1')
    assert not public_endpoint().insecure_http


def test_browser_default_port_origin_is_canonical(monkeypatch):
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN', 'https://Example.COM:443/')
    assert public_endpoint().origin == 'https://example.com'
    assert public_endpoint().authority == 'example.com'


@pytest.mark.parametrize('url', [
    'http://localhost:8890', 'https://127.0.0.1:8890',
    'http://192.168.1.8:8890', 'http://127.0.0.1',
    'http://127.0.0.1:8890/worker', 'http://admin@127.0.0.1:8890',
    'http://127.0.0.1:8890?x=1', 'http://127.0.0.1:99999',
])
def test_worker_stays_loopback_only(monkeypatch, url):
    monkeypatch.setenv('CLEAR_WORKER_URL', url)
    with pytest.raises(RuntimeError, match='loopback'):
        private_worker_url()


def test_worker_and_native_headers_use_configured_port(monkeypatch):
    from workbench_platform.tasks.worker import ExecutionHost
    monkeypatch.setenv('CLEAR_WORKER_URL', 'http://127.0.0.1:19091')
    expected = {'Host': '127.0.0.1:19091', 'Origin': 'http://127.0.0.1:19091'}
    assert private_worker_headers() == expected

    async def exercise():
        def inspect(request):
            assert request.headers['Host'] == expected['Host']
            assert request.headers['Origin'] == expected['Origin']
            assert request.headers['X-Clear-Step'] == 'step'
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(inspect),
                                     base_url=private_worker_url()) as client:
            host = ExecutionHost.__new__(ExecutionHost)
            host.client = client
            assert (await host.call('POST', '/api/platform/v1/example', {}, 'step')).status_code == 200
    asyncio.run(exercise())


def test_lan_launcher_requires_same_explicit_listen_address(monkeypatch):
    from scripts.serve_task_stack import portal_listen_address
    monkeypatch.setenv('CLEAR_ALLOW_LAN_HTTP', '1')
    monkeypatch.setenv('CLEAR_PUBLIC_ORIGIN', 'http://192.168.1.8:9021')
    args = Namespace(port=9021, worker_port=19091, lan_http=True,
                     development_http=False, listen_address='192.168.1.8',
                     certificate=None, private_key=None)
    assert portal_listen_address(args) == '192.168.1.8'
    for address in (None, '0.0.0.0', '192.168.1.9'):
        args.listen_address = address
        with pytest.raises(ValueError, match='LAN HTTP'):
            portal_listen_address(args)
    args.listen_address = '192.168.1.8'
    args.port = 8889
    with pytest.raises(ValueError, match='port must match'):
        portal_listen_address(args)
