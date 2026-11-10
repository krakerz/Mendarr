from mendarr.core.classify import classify
from mendarr.core.judge import Verdict
from mendarr.rpc import Cookie, Snapshot, WorkerError

CF = {"server": "cloudflare"}
CHALLENGE = Snapshot(url="https://e.com/", status=403, headers={"cf-mitigated": "challenge", "server": "cloudflare"},
                     title="Just a moment...", html='<div id="challenge-stage"></div>')
CHALLENGE_CLEARED = CHALLENGE.model_copy(update={"cookies": [Cookie(name="cf_clearance", value="x")]})
BLOCK = Snapshot(url="https://e.com/", status=403, headers=CF, title="Attention Required! | Cloudflare",
                 html="Sorry, you have been blocked")
CF_521 = Snapshot(url="https://e.com/", status=521, headers=CF, title="e.com | 521: Web server is down",
                  html='<div id="cf-error-details"><span class="cf-error-code">521</span></div>')
PLAIN = Snapshot(url="https://e.com/", title="Hello", html="<p>hi</p>")


def err(t):
    return WorkerError(type=t)


def test_navigation_error_is_network():
    assert classify(error=err("navigation"), snapshot=None, verdict=None) == "network"


def test_521_is_network():
    assert classify(error=None, snapshot=CF_521, verdict=None) == "network"


def test_block_wins_over_deadline():
    v = Verdict(False, "blocked")
    assert classify(error=err("deadline"), snapshot=BLOCK, verdict=v) == "blocked"


def test_challenge_with_clicks_is_loop():
    v = Verdict(False, "challenge")
    assert classify(error=None, snapshot=CHALLENGE, verdict=v, clicks=2) == "challenge_loop"


def test_challenge_with_clearance_is_loop():
    v = Verdict(False, "challenge")
    assert classify(error=None, snapshot=CHALLENGE_CLEARED, verdict=v) == "challenge_loop"


def test_challenge_deadline_no_clicks_is_timeout():
    assert classify(error=err("deadline"), snapshot=CHALLENGE, verdict=None) == "timeout"


def test_deadline_plain_is_timeout():
    assert classify(error=err("deadline"), snapshot=PLAIN, verdict=None) == "timeout"


def test_no_slot_is_timeout():
    assert classify(error=err("no_slot"), snapshot=None, verdict=None) == "timeout"


def test_plugin_error_plain_is_unknown():
    assert classify(error=err("plugin"), snapshot=PLAIN, verdict=None) == "unknown"
