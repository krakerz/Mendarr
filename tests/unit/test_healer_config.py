import os

import pytest
from pydantic import ValidationError

from mendarr.config import Settings
from mendarr.healer.config import (
    ConfigFile,
    HealerConfig,
    parse,
    parse_duration,
    refresh_policy,
)


@pytest.mark.parametrize(
    ("raw", "secs"), [(90, 90), ("90", 90), ("90s", 90), ("5m", 300), ("2h", 7200), ("1d", 86400), ("1.5m", 90)]
)
def test_durations(raw, secs):
    assert parse_duration(raw) == secs


def test_bad_duration():
    with pytest.raises(ValueError):
        parse_duration("x")


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("HOOK", "https://example.invalid/h")
    monkeypatch.delenv("NOPE", raising=False)
    cfg = parse("notify:\n  discord_webhook: ${HOOK}\n  webhook_url: ${NOPE}\n")
    assert cfg.notify.discord_webhook == "https://example.invalid/h"
    assert cfg.notify.webhook_url == ""


def test_agent_tier_off_and_durations():
    cfg = parse("escalation:\n  agent_tier: off\n  heal_windows: [5m, 1h]\n")
    assert cfg.escalation.agent_tier is False
    assert cfg.escalation.heal_windows == [300, 3600]
    assert parse("escalation:\n  agent_tier: on\n").escalation.agent_tier is True


def test_unknown_order_profile():
    with pytest.raises(ValidationError):
        parse("llm:\n  order: [ghost]\n")


def test_typo_key_fails():
    with pytest.raises(ValidationError):
        parse("healer:\n  enabeld: true\n")
    with pytest.raises(ValidationError):
        parse("domains:\n  a.org:\n    escalation:\n      heal_roundz: 1\n")


def test_for_domain_overrides():
    cfg = parse(
        "llm:\n  order: [a]\n  profiles:\n    a: {base_url: 'http://x/v1'}\n    b: {base_url: 'http://y/v1'}\n"
        "escalation:\n  heal_rounds: 3\n  agent_tier: on\n"
        "domains:\n  a.org:\n    enabled: false\n    llm_order: [b]\n    daily_rounds: 2\n"
        "    escalation: {heal_rounds: 1}\n"
    )
    pol = cfg.for_domain("a.org")
    assert not pol.enabled and pol.llm_order == ["b"] and pol.daily_rounds == 2
    assert pol.escalation.heal_rounds == 1 and pol.escalation.agent_tier is True
    other = cfg.for_domain("b.org")
    assert other.enabled and other.llm_order == ["a"] and other.escalation.heal_rounds == 3


def test_config_file_reload(tmp_path):
    path = tmp_path / "mendarr.yml"
    cf = ConfigFile(path)
    assert cf.get() == HealerConfig()
    path.write_text("healer:\n  poll_s: 3\n")
    assert cf.get().healer.poll_s == 3
    path.write_text("healer: [garbage")
    os.utime(path, (path.stat().st_mtime + 5, path.stat().st_mtime + 5))
    assert cf.get().healer.poll_s == 3


def test_refresh_policy(monkeypatch):
    monkeypatch.delenv("MENDARR_COOKIE_REFRESH_INTERVAL", raising=False)
    monkeypatch.delenv("MENDARR_COOKIE_REFRESH_IDLE_DAYS", raising=False)
    assert refresh_policy(Settings(), HealerConfig()) == (0.0, 7.0)
    env = Settings(cookie_refresh_interval="1h", cookie_refresh_idle_days=3)
    assert refresh_policy(env, HealerConfig()) == (3600.0, 3.0)
    cfg = parse("cookies: {refresh_interval: 90m, refresh_idle_days: 1}")
    assert cfg.cookies.refresh_interval == 5400
    assert refresh_policy(env, cfg) == (5400.0, 1.0)
    assert refresh_policy(env, parse("cookies: {refresh_interval: 0}"))[0] == 0.0
    assert refresh_policy(Settings(cookie_refresh_interval="1s"), HealerConfig())[0] == 10.0
    assert refresh_policy(Settings(cookie_refresh_interval="soon"), HealerConfig())[0] == 0.0
