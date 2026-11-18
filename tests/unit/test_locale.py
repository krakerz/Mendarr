import pytest

from mendarr.config import Settings
from mendarr.worker import locale as loc
from mendarr.worker.backends import patchright
from mendarr.worker.backends.camoufox import LaunchedBrowser
from mendarr.worker.slots import SlotPool


def ip_of(value, calls=None):
    async def exit_ip():
        if calls is not None:
            calls.append(1)
        return value

    return exit_ip


@pytest.fixture(autouse=True)
def fake_main_locale(monkeypatch):
    monkeypatch.setattr(loc, "main_locale", lambda ip: "en-AU")


async def test_explicit_wins_without_lookup():
    calls: list = []
    assert await loc.resolve_locale(Settings(locale=" de-DE "), ip_of("1.2.3.4", calls)) == "de-DE"
    assert calls == []


async def test_main_language_of_known_ip():
    assert await loc.resolve_locale(Settings(locale="", geoip=True), ip_of("1.2.3.4")) == "en-AU"


@pytest.mark.parametrize("ip", ["unknown", "default"])
async def test_unknown_ip_is_no_override(ip):
    assert await loc.resolve_locale(Settings(locale="", geoip=True), ip_of(ip)) is None


async def test_geoip_off_or_no_lookup():
    calls: list = []
    assert await loc.resolve_locale(Settings(locale="", geoip=False), ip_of("1.2.3.4", calls)) is None
    assert calls == []
    assert await loc.resolve_locale(Settings(locale="", geoip=True), None) is None


def test_patchright_locale_args():
    assert patchright.locale_args("en-AU") == ["--lang=en-AU", "--accept-lang=en-AU,en"]
    assert patchright.locale_args("ja") == ["--lang=ja", "--accept-lang=ja"]


class FakeBrowser:
    def is_connected(self):
        return True


async def test_context_options_skip_camoufox():
    async def launcher(slot, locale=None):
        async def close():
            pass

        return LaunchedBrowser(FakeBrowser(), "UA", close, "en-AU")

    pool = SlotPool(1, {"camoufox": launcher, "patchright": launcher})
    await pool.browser(1, "camoufox")
    await pool.browser(1, "patchright")
    assert pool.context_options(1, "camoufox") == {}
    assert pool.context_options(1, "patchright") == {"locale": "en-AU"}
    assert pool.context_options(1, "obscura") == {}


async def test_requested_locale_beats_settings_and_skips_lookup():
    calls: list = []
    settings = Settings(locale="de-DE", geoip=True)
    assert await loc.resolve_locale(settings, ip_of("1.2.3.4", calls), "fr-FR") == "fr-FR"
    assert await loc.locale_for_ip(settings, "1.2.3.4", "fr-FR") == "fr-FR"
    assert await loc.resolve_locale(Settings(locale="", geoip=True), ip_of("1.2.3.4", calls), " ja ") == "ja"
    assert calls == []


class Browser:
    def __init__(self):
        self.contexts: list = []

    def is_connected(self):
        return True


def locale_pool():
    launches: list = []

    async def launcher(slot, locale=None):
        browser = Browser()
        closed: list = []

        async def close():
            closed.append(1)

        launches.append((browser, locale, closed))
        return LaunchedBrowser(browser, "UA", close, locale or "en-AU")

    return SlotPool(1, {"camoufox": launcher, "patchright": launcher}), launches


async def test_differing_request_locale_relaunches_idle_browser():
    pool, launches = locale_pool()
    first = await pool.browser(1)
    assert await pool.browser(1, locale="en-AU") is first  # same as the launch language
    second = await pool.browser(1, locale="de-DE")
    assert second is not first
    assert [(lang, len(closed)) for _, lang, closed in launches] == [(None, 1), ("de-DE", 0)]
    assert await pool.browser(1, locale="de-DE") is second


async def test_busy_browser_keeps_its_language():
    pool, launches = locale_pool()
    first = await pool.browser(1)
    first.contexts.append(object())  # a session or manual solve lives in it
    assert await pool.browser(1, locale="de-DE") is first
    assert len(launches) == 1 and launches[0][2] == []
    first.contexts.clear()
    assert await pool.browser(1, locale="de-DE") is not first  # free now: picks the language up


async def test_no_request_locale_never_relaunches():
    pool, launches = locale_pool()
    first = await pool.browser(1)
    assert await pool.browser(1, locale=None) is first
    assert len(launches) == 1


async def test_context_options_prefer_request_locale():
    pool, _ = locale_pool()
    await pool.browser(1, "camoufox")
    await pool.browser(1, "patchright")
    assert pool.context_options(1, "patchright", "de-DE") == {"locale": "de-DE"}
    assert pool.context_options(1, "patchright", None) == {"locale": "en-AU"}
    assert pool.context_options(1, "camoufox", "de-DE") == {}
