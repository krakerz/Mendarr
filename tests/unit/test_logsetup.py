import logging

import pytest

from mendarr import logsetup

NAMES = ("", "mendarr", *logsetup.NOISY)


@pytest.fixture(autouse=True)
def restore_levels():
    saved = {n: logging.getLogger(n).level for n in NAMES}
    yield
    for n, lvl in saved.items():
        logging.getLogger(n).setLevel(lvl)


def level(name):
    return logging.getLogger(name).getEffectiveLevel()


def test_debug_is_mendarr_only():
    assert logsetup.configure("debug") == "info"
    assert logging.getLogger("mendarr.x").isEnabledFor(logging.DEBUG)
    assert level("websockets") == logging.INFO
    assert level("httpx") == logging.WARNING
    assert level("httpcore") == logging.WARNING


def test_info_pins_httpx_to_warning():
    assert logsetup.configure("INFO") == "info"
    assert level("httpx") == logging.WARNING
    assert level("httpcore") == logging.WARNING
    assert not logging.getLogger("mendarr.x").isEnabledFor(logging.DEBUG)


def test_warning_level():
    assert logsetup.configure("warning") == "warning"
    assert level("httpx") == logging.WARNING
    assert level("mendarr") == logging.WARNING


def test_unknown_level_falls_back_to_info(caplog):
    assert logsetup.configure("loud") == "info"
    assert "unknown log level" in caplog.text
