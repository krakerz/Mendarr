"""Failure classes. Only challenge-type failures get healed (M3); a site that's down doesn't."""

from typing import Literal

from mendarr.core import markers
from mendarr.core.judge import Verdict, has_clearance, is_blocked, is_challenge, is_cloudflare
from mendarr.rpc import Snapshot, WorkerError

FailureKind = Literal["network", "timeout", "blocked", "challenge_loop", "unknown"]


def classify(
    *, error: WorkerError | None, snapshot: Snapshot | None, verdict: Verdict | None, clicks: int = 0
) -> FailureKind:
    if verdict is not None and verdict.reason == "blocked":
        return "blocked"
    if snapshot is not None and is_blocked(snapshot.headers, snapshot.title, snapshot.html):
        return "blocked"
    if error is not None and error.type == "navigation":
        return "network"
    if snapshot is not None and snapshot.status in markers.CF_ORIGIN_ERRORS and is_cloudflare(snapshot.headers):
        return "network"
    timed_out = error is not None and error.type in ("deadline", "no_slot")
    challenged = (verdict is not None and verdict.reason in ("challenge", "no_clearance")) or (
        snapshot is not None and is_challenge(snapshot.headers, snapshot.title, snapshot.html)
    )
    if challenged:
        if clicks > 0 or (snapshot is not None and has_clearance(snapshot)):
            return "challenge_loop"
        return "timeout" if timed_out else "challenge_loop"
    if timed_out:
        return "timeout"
    return "unknown"
