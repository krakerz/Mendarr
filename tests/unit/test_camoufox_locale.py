import types

import pytest

pytest.importorskip("camoufox")  # the browser extra; CI runs unit tests without it

from camoufox.locales import Locale  # noqa: E402

from mendarr.worker.locale import main_locale  # noqa: E402


def fake_geo(region):
    return lambda ip, **kw: types.SimpleNamespace(locale=Locale(language="zh", region=region, script="Hant"))


@pytest.mark.parametrize(("region", "want"), [("AU", "en-AU"), ("JP", "ja-JP"), ("US", "en-US")])
def test_main_locale_is_the_main_language(monkeypatch, region, want):
    # Camoufox's own pick for the region may be any language spoken there (zh-Hant for AU here)
    monkeypatch.setattr("camoufox.geolocation.get_geolocation", fake_geo(region))
    for _ in range(5):
        assert main_locale("46.102.153.133") == want


def test_main_locale_unknown_ip(monkeypatch):
    def boom(ip, **kw):
        raise ValueError("IP not found in database")

    monkeypatch.setattr("camoufox.geolocation.get_geolocation", boom)
    assert main_locale("10.0.0.1") is None
