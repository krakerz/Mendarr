import asyncio

import httpx

from mendarr.proxy import parse_upstream
from mendarr.rpc import ProxySpec
from mendarr.worker.egress import Egress


def egress(handler, **kw):
    return Egress("http://x/ip", transport=httpx.MockTransport(handler), **kw)


class Stop(BaseException):
    pass


async def test_json_body():
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}))
    await e.refresh()
    assert (await e.info()).ip == "1.2.3.4"


async def test_text_body():
    e = egress(lambda r: httpx.Response(200, text=" 5.6.7.8\n"))
    await e.refresh()
    assert (await e.info()).ip == "5.6.7.8"


async def test_info_never_does_io():
    e = egress(lambda r: (_ for _ in ()).throw(AssertionError("no lookup")))
    info = await e.info()
    assert (info.ip, info.stale) == ("unknown", False)


async def test_error_before_any_success_stays_unknown():
    e = egress(lambda r: httpx.Response(500))
    assert await e.refresh() is False
    assert (await e.info()).ip == "unknown"


async def test_failure_after_success_keeps_ip_and_marks_stale():
    state = {"ok": True}

    def handler(r):
        return httpx.Response(200, json={"ip": "1.2.3.4"}) if state["ok"] else httpx.Response(500)

    e = egress(handler)
    await e.refresh()
    state["ok"] = False
    assert await e.refresh() is False
    info = await e.info()
    assert (info.ip, info.stale) == ("1.2.3.4", True)
    state["ok"] = True
    assert await e.refresh() is True
    assert (await e.info()).stale is False


async def test_concurrent_refresh_is_single_flight():
    calls = []

    async def handler(r):
        calls.append(1)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"ip": "1.2.3.4"})

    e = egress(handler)
    assert await asyncio.gather(e.refresh(), e.refresh()) == [True, True]
    assert len(calls) == 1


async def test_exit_ip_without_run_looks_up_inline_once():
    calls = []

    def handler(r):
        calls.append(1)
        return httpx.Response(200, json={"ip": "1.2.3.4"})

    e = egress(handler)
    assert await e.exit_ip() == "1.2.3.4"
    assert await e.exit_ip() == "1.2.3.4"
    assert len(calls) == 1


async def test_exit_ip_waits_for_the_running_task():
    gate = asyncio.Event()

    async def handler(r):
        await gate.wait()
        return httpx.Response(200, json={"ip": "7.7.7.7"})

    e = egress(handler)
    task = asyncio.create_task(e.run())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(e.exit_ip())
    await asyncio.sleep(0.01)
    assert not waiter.done()
    gate.set()
    assert await waiter == "7.7.7.7"
    task.cancel()


async def test_exit_ip_gives_up_waiting():
    async def handler(r):
        await asyncio.sleep(10)

    e = egress(handler)
    task = asyncio.create_task(e.run())
    await asyncio.sleep(0)
    assert await e.exit_ip(wait_s=0.01) == "unknown"
    task.cancel()


async def test_run_cadence(monkeypatch):
    sleeps = []
    ips = iter(["1.1.1.1", "2.2.2.2"])

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            raise Stop

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    e = egress(lambda r: httpx.Response(200, json={"ip": next(ips)}), ttl=60)
    try:
        await e.run()
    except Stop:
        pass
    assert sleeps == [60, 60]
    assert (await e.info()).ip == "2.2.2.2"


async def test_run_retries_sooner_after_failure(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        raise Stop

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    e = egress(lambda r: httpx.Response(500), ttl=60)
    try:
        await e.run()
    except Stop:
        pass
    assert sleeps == [15.0]


async def test_empty_url_is_default():
    e = Egress("")
    assert await e.refresh() is True
    assert (await e.info()).ip == "default"


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
    await e.refresh()
    info = await e.info()
    assert FakeClient.proxies == ["http://u:p@up:3128"]
    assert (info.ip, info.proxy) == ("9.9.9.9", "http://up:3128")


async def test_lookup_failure_keeps_proxy(monkeypatch):
    FakeClient.proxies, FakeClient.fail = [], True
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    e = Egress("http://x/ip", upstream=parse_upstream("up:3128", ""))
    await e.refresh()
    info = await e.info()
    assert (info.ip, info.proxy) == ("unknown", "http://up:3128")


async def test_request_proxy_key_is_redacted():
    assert await Egress("").current(ProxySpec(url="http://u:p@p:8080")) == "proxy:http://p:8080"


async def test_no_upstream_means_direct():
    assert (await Egress("").info()).proxy is None
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4"}))
    await e.refresh()
    assert (await e.info()).proxy is None


async def test_country_parsed_from_response():
    e = egress(lambda r: httpx.Response(200, json={"ip": "1.2.3.4", "country": "ro"}))
    await e.refresh()
    info = await e.info()
    assert info.ip == "1.2.3.4"
    assert info.country == "RO"


async def test_country_secondary_lookup():
    def handler(r):
        if "api.country.is" in str(r.url):
            return httpx.Response(200, json={"country": "JP"})
        return httpx.Response(200, json={"ip": "93.118.41.92"})

    e = egress(handler, country_url="https://api.country.is/{ip}")
    await e.refresh()
    info = await e.info()
    assert info.ip == "93.118.41.92"
    assert info.country == "JP"


async def test_country_is_cached_per_ip_and_retried_when_missing():
    main, country = [], []
    state = {"ip": "93.118.41.92", "country_ok": False}

    def handler(r):
        if "api.country.is" in str(r.url):
            country.append(1)
            if not state["country_ok"]:
                return httpx.Response(500)
            return httpx.Response(200, json={"country": "JP"})
        main.append(1)
        return httpx.Response(200, json={"ip": state["ip"]})

    e = egress(handler, country_url="https://api.country.is/{ip}")
    await e.refresh()
    assert (await e.info()).country is None
    state["country_ok"] = True
    await e.refresh()  # missing country: looked up again
    assert (await e.info()).country == "JP"
    assert len(country) == 2
    await e.refresh()  # unchanged IP: no country request
    assert len(country) == 2
    state["ip"] = "8.8.8.8"
    await e.refresh()  # changed IP: one more
    assert len(country) == 3 and len(main) == 4


async def test_country_kept_when_the_refresh_fails():
    state = {"ok": True}

    def handler(r):
        if state["ok"]:
            return httpx.Response(200, json={"ip": "1.2.3.4", "country": "ro"})
        return httpx.Response(500)

    e = egress(handler)
    await e.refresh()
    state["ok"] = False
    await e.refresh()
    assert (await e.info()).country == "RO"


async def test_empty_country_url_disables_lookup():
    def handler(r):
        assert "country" not in str(r.url)
        return httpx.Response(200, json={"ip": "1.2.3.4"})

    e = egress(handler, country_url=None)
    await e.refresh()
    assert (await e.info()).country is None
