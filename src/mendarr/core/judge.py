"""Success judge: decides from what the executor saw, never from what a plugin reports."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from mendarr.core import markers
from mendarr.rpc import Snapshot

Reason = Literal["clear", "challenge", "blocked", "no_clearance"]


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: Reason


def is_cloudflare(headers: Mapping[str, str]) -> bool:
    return headers.get("server", "").lower() == "cloudflare" or "cf-ray" in headers


def is_challenge(headers: Mapping[str, str], title: str, html: str) -> bool:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if title.strip().lower() in markers.CHALLENGE_TITLES:
        return True
    return markers.CHALLENGE_HTML.search(html) is not None


def is_blocked(headers: Mapping[str, str], title: str, html: str) -> bool:
    t = title.strip()
    if t.startswith("Attention Required! | Cloudflare"):
        return True
    if t.startswith("Access denied") and (is_cloudflare(headers) or "cf-error-details" in html):
        return True
    return markers.BLOCKED_HTML.search(html) is not None


def has_clearance(snap: Snapshot) -> bool:
    return any(c.name == "cf_clearance" for c in snap.cookies)


def judge(snap: Snapshot, *, challenge_seen: bool) -> Verdict:
    if is_blocked(snap.headers, snap.title, snap.html):
        return Verdict(False, "blocked")
    if is_challenge(snap.headers, snap.title, snap.html):
        return Verdict(False, "challenge")
    if challenge_seen and not has_clearance(snap):
        return Verdict(False, "no_clearance")
    return Verdict(True, "clear")
