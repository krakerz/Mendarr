import httpx

from mendarr.proxy import parse_upstream
from mendarr.rpc import ProxySpec
from mendarr.worker.egress import Egress


def egress(handler, **kw):
    return Egress("http://x/ip", transport=httpx.MockTransport(handler), **kw)


async def test_json_body():
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}))
    assert (await e.info()).ip == "1.2.3.4"


async def test_text_body():
    e = egress(lambda r: httpx.Response(200, text=" 5.6.7.8\n"))
    assert (await e.info()).ip == "5.6.7.8"


async def test_error_is_unknown_and_retried_after_a_short_pause():
    calls = []
    now = [100.0]

    def handler(r):
        calls.append(1)
        return httpx.Response(500)

    e = egress(handler, clock=lambda: now[0])
    assert (await e.info()).ip == "unknown"
    now[0] = 105
    assert (await e.info()).ip == "unknown"
    assert len(calls) == 1  # a dead endpoint is not hit again right away
    now[0] = 116
    await e.info()
    assert len(calls) == 2


async def test_empty_url_is_default():
    assert (await Egress("").info()).ip == "default"


async def test_cache_within_ttl():
    calls = []
    now = [100.0]

    def handler(r):
        calls.append(1)
        return httpx.Response(200, json={"ip": "1.1.1.1"})

    e = egress(handler, ttl=60, clock=lambda: now[0])
    await e.info()
    now[0] = 130
    await e.info()
    assert len(calls) == 1
    now[0] = 161
    await e.info()
    assert len(calls) == 2


async def test_proxy_key_skips_lookup():
    e = egress(lambda r: (_ for _ in ()).throw(AssertionError("no lookup")))
    assert await e.current(ProxySpec(url="http://p:8080")) == "proxy:http://p:8080"


class FakeClient:
    proxies: list = []
    fail = False

    def __init__(self, *, proxy=None, **kw):
        FakeClient.proxies.append(proxy)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url):
        if FakeClient.fail:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"ip": "9.9.9.9"}, request=httpx.Request("GET", url))


async def test_lookup_through_upstream(monkeypatch):
    FakeClient.proxies, FakeClient.fail = [], False
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    e = Egress("http://x/ip", upstream=parse_upstream("http://u:p@up:3128", ""))
    info = await e.info()
    assert FakeClient.proxies == ["http://u:p@up:3128"]
    assert (info.ip, info.proxy) == ("9.9.9.9", "http://up:3128")


async def test_lookup_failure_keeps_proxy(monkeypatch):
    FakeClient.proxies, FakeClient.fail = [], True
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    info = await Egress("http://x/ip", upstream=parse_upstream("up:3128", "")).info()
    assert (info.ip, info.proxy) == ("unknown", "http://up:3128")


async def test_request_proxy_key_is_redacted():
    assert await Egress("").current(ProxySpec(url="http://u:p@p:8080")) == "proxy:http://p:8080"


async def test_no_upstream_means_direct():
    assert (await Egress("").info()).proxy is None
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}))
    assert (await e.info()).proxy is None


async def test_country_parsed_from_response():
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4", "country": "ro"}))
    info = await e.info()
    assert info.ip == "1.2.3.4"
    assert info.country == "RO"


async def test_country_secondary_lookup():
    def handler(r):
        if "api.country.is" in str(r.url):
            return httpx.Response(200, json={"country": "JP"})
        return httpx.Response(200, json={"ip": "93.118.41.92"})

    e = egress(handler, country_url="https://api.country.is/{ip}")
    info = await e.info()
    assert info.ip == "93.118.41.92"
    assert info.country == "JP"


async def test_failed_country_lookup_is_retried_but_ip_stays_cached():
    now = [100.0]
    main, country = [], []
    country_ok = [False]

    def handler(r):
        if "api.country.is" in str(r.url):
            country.append(1)
            if not country_ok[0]:
                return httpx.Response(500)
            return httpx.Response(200, json={"country": "JP"})
        main.append(1)
        return httpx.Response(200, json={"ip": "93.118.41.92"})

    e = egress(handler, ttl=60, clock=lambda: now[0], country_url="https://api.country.is/{ip}")
    assert (await e.info()).country is None
    now[0] = 105
    assert (await e.info()).country is None  # too soon to retry
    assert len(country) == 1
    now[0] = 116
    country_ok[0] = True
    info = await e.info()
    assert (info.ip, info.country) == ("93.118.41.92", "JP")
    assert len(main) == 1 and len(country) == 2
    await e.info()
    assert len(country) == 2  # resolved: no more retries


async def test_empty_country_url_disables_lookup():
    def handler(r):
        assert "country" not in str(r.url)
        return httpx.Response(200, json={"ip": "1.2.3.4"})

    info = await egress(handler, country_url=None).info()
    assert info.country is None
