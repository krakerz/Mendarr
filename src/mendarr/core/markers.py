"""Cloudflare page markers shared by the judge (API) and the SDK (worker).

Titles and ids adapted from FlareSolverr (MIT, github.com/FlareSolverr/FlareSolverr,
src/flaresolverr_service.py). Class selectors like .ray_id/.lds-ring were left out: they
also appear on ordinary and 5xx pages.
"""

import re

CHALLENGE_TITLES = frozenset({"just a moment...", "just a moment…", "ddos-guard"})
ACCESS_DENIED_TITLES = ("Access denied", "Attention Required! | Cloudflare")

CHALLENGE_IDS = (
    "cf-challenge-running", "challenge-form", "challenge-stage", "challenge-running",
    "challenge-body-text", "cf-please-wait", "challenge-spinner", "trk_jschal_js", "turnstile-wrapper",
)
CHALLENGE_CSS = ", ".join(f"#{i}" for i in CHALLENGE_IDS)
CHALLENGE_HTML = re.compile(
    r"""id=["']?(?:%s)["'\s>]|window\._cf_chl_opt""" % "|".join(map(re.escape, CHALLENGE_IDS))
)
# 10xx = Cloudflare firewall/access errors; 52x origin errors are not blocks
BLOCKED_HTML = re.compile(r"Sorry, you have been blocked|cf-error-code[^>]*>\s*10\d\d\b", re.I)
CF_ORIGIN_ERRORS = range(520, 531)

TURNSTILE_FRAME_HINTS = ("challenges.cloudflare.com", "/cdn-cgi/challenge-platform/")
TURNSTILE_RESPONSE_INPUT = 'input[name="cf-turnstile-response"]'
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def extract_title(html: str) -> str:
    m = TITLE_RE.search(html)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
